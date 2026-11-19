# DSSS builder: LFSR codes, carrier families, multi-user overlays

The DSSS builder is an engineering view feature for building DSSS (direct
sequence spread spectrum) messages from an LFSR (linear feedback shift
register) definition. It takes any carrier family the modem offers and many
simultaneous users with different spreading codes. With it you can emulate a
wide multi-user CDMA system and decode each user separately.

The pieces:

| Piece | Where |
|---|---|
| LFSR, m-sequence and Gold code library, with analysis | `dpmg/modulation/lfsr_codes.py` |
| code resolver (families seed, m, gold, custom, lfsr) | `dpmg/modulation/spreading_codes.py` (`resolve_code`, `describe_code`) |
| DSSS modulator and demodulator over BPSK, QPSK, OQPSK, pi/4-QPSK, dual code | `dpmg/modulation/spread_spectrum.py` |
| scenario JSON keys `lfsr`, `carrier`, `chip_rate_hz`, `user`, `code_set`, `users`, `dsss` | `dpmg/scenario/gen_scenario_json.py` (`expand_users`, `overlay_report`, `dsss_preset_specs`) |
| plan and channelizer: one channel per user at one centre | `dpmg/scenario/plan_adapter.py`, `dpmg/dsp/channelizer_worker.py` (CDMA_OVERLAY) |
| engineering view panel | `dpmg/web/dsss_builder_tab.py`, mounted from `link_tab.build_remote_sdr` (SDR tab, Live PSD column) |
| presets | `scenarios/json/dsss_4user_bpsk.json`, `dsss_mixed_carriers.json`, `dsss_16user.json` |
| input checks | `dpmg/scenario/dsss_builder_validate.py` (`validate_builder`, see Input checks under section 4) |
| tests | `tests/test_lfsr_codes.py`, `tests/test_dsss_multiuser.py`, `tests/test_dsss_builder_validate.py`, and the older `test_dsss_codes.py` and `test_dsss_scenario_roundtrip.py` |
| bench and checkout | `bench/dsss_multiuser.py` (stage `dsss_multiuser` of `bench/first_checkout.py`) |

## 1. Codes

A spreading code is a dict (`code_spec`). Both ends resolve it to the same
sequence of +1 and -1 chips. The families:

```
{"family": "lfsr", "poly": [5,2], "state": 1, "form": "fibonacci", "length": 31, "offset": 0}
{"family": "gold", "polys": [[6,1],[6,5,2,1]], "offset": 3, "state": 1, "state2": 1, "length": 63}
{"family": "m", "n": 7, "seed": 1}            # legacy preset (kept, chips unchanged)
{"family": "gold", "n": 6, "index": 3}        # legacy preset (kept, see below)
{"family": "seed", "length": 15, "seed": 1}   # legacy numpy PN
{"family": "custom", "chips": "+-+--++-"}
```

- `poly` is the feedback polynomial x^n + ... + 1. It can be an exponent list
  `[5,2]`, an int or hex mask where bit k-1 stands for x^k (`0x12` is
  x^5+x^2+1), a string `"x^5+x^2+1"`, or `"5,2"`. The 0 term is implied.
  `degree` overrides n.
- `state` is the n bit register start; 0 is replaced by 1. The first chip is
  the state's LSB. `form` is `fibonacci` (the default, the same as the legacy
  generator) or `galois` (the same sequence at another phase).
- `length` cuts a period short (for example 48 of 63). `offset` discards that
  many chips first, which shifts the phase. A Gold `offset` of -1 or -2
  returns one of the two base m-sequences.
- `lfsr_codes.PRIMITIVE[n]` (n from 2 to 16) and `PRIMITIVE_ALT` hold known
  primitive polynomials. `GOLD_PAIRS[n]` (n = 5, 6, 7, 9, 10, 11) holds
  preferred pairs, whose cross-correlation takes only the three values -1, -t
  and t-2, with t = 2^((n+1)/2)+1 for odd n or 2^((n+2)/2)+1 for even n. Gold
  codes do not exist for n divisible by 4. The n=10 pair is the GPS C/A pair;
  `tests/test_lfsr_codes.py` checks it against the published first chips of
  PRN 1, 2 and 3.
- `lfsr_codes.legacy_to_lfsr(spec)` rewrites any legacy preset in the explicit
  form, chip for chip. In the legacy gold table the second polynomial is not
  primitive for n = 6, 7, 9 and 10; only the n = 5 pair is a true preferred
  pair. Those legacy "gold" codes stay unchanged so deployed plans still work,
  but they have no cross-correlation bound. The builder and the presets always
  write the `lfsr` form or the explicit `polys` form.

A code set (`lfsr_codes.make_code_set`) expands to N explicit specs:

```
{"kind": "gold", "n": 6, "count": 4}                     distinct offsets of one Gold generator
{"kind": "mseq_phases", "poly": [6,1], "count": 4}       distinct seeds (phases) of one m-sequence
{"kind": "polys", "n": 7, "count": 3}                    distinct primitive polynomials
{"kind": "list", "codes": [{...}, {...}]}                explicit list
```

### Reading the correlation table

`lfsr_codes.set_report(codes)` fills the panel's table and the plan's
`dsss_overlay` block. For each code it gives:

- the balance, the sum of the +1 and -1 chips (-1 for an m-sequence);
- the peak auto-correlation away from zero lag, which says how well the
  despreader finds its own timing (1 for an m-sequence, at most t for a Gold
  code);
- the N by N table of peak cross-correlation magnitude over all lags, which
  says how much of another user's chips leaks into this user's despread
  symbol (the diagonal is L, the code length);
- the worst pair, in dB of the peak;
- the predicted MUI (multi-user interference) SNR, 10 log10(L^2 / sum_j
  rms^2(xcorr_ij)): the despread signal to interference ratio that an equal
  power, chip asynchronous overlay of the whole set leaves this user, before
  thermal noise.

Rules of thumb: a BPSK user decodes cleanly above about 8 dB, and a QPSK
family user needs about 3 dB more per rail. Random-like codes give L / (N-1):
9 dB for 16 users at 127 chips, 10 dB for 4 users at 31. Gold sets bound the
worst lag at t / L: -10.7 dB at n=5, -11.4 dB at n=6, -17.5 dB at n=7. The
equal power prediction uses the rms over all lags.

## 2. Carrier families

`dsss_modem` (or `carrier`) is `bpsk` (1 bit a symbol, real chips), `qpsk`,
`oqpsk` (the Q rail staggered by half a chip, so `samples_per_chip` must be
even) or `pi4qpsk` (alternating constellation grids). The same code multiplies every chip of the symbol. The plan modulation names
`DSSS` (with `dsss_modem`), `BPSK-DSSS`, `QPSK-DSSS`, `OQPSK-DSSS` and
`PI4QPSK-DSSS` all resolve. MSK or GMSK shaped chips are not offered, because
the modem has no MSK chip shaper. GFSK is a frequency modulation, not a chip
filter.

Dual code spreading (`code_spec_q` or `lfsr_q`, qpsk and oqpsk only) puts a
second code on the Q rail, as in IS-95 complex spreading. The I rail is
despread with the first code and the Q rail with the second. The two codes
must have the same length. Without a second code (the default) one real code
multiplies the complex symbol.

The receiver (`DSSSDemodulator`) acquires chip and symbol timing by itself,
trying every offset within one symbol. It removes the remaining carrier offset
from the per-symbol correlations with an M-th power FFT peak, over plus or
minus Rs/2M: 600 Hz for BPSK and 300 Hz for QPSK, OQPSK and pi/4 at 2400 Bd.
It takes the common phase from the 2nd or 4th power (8th for pi/4). For OQPSK
it searches timing and quadrant together, scored on the real part of the I
rail at the chip timing and the imaginary part of the Q rail half a chip
later, then makes one decision directed phase refinement.

The frame layer resolves what ambiguity is left. `Modem.receive` and
`demod_helper.demod_with_recovery` try each rotation from
`dsss_rotations(carrier)`: 1 for BPSK, 4 steps of 90 degrees for the QPSK
family, and 8 steps of 45 degrees for pi/4-QPSK, whose grid parity cannot be
seen blind.

### Overlay interference cancellation (DSSS_MUI)

Users that share a centre, symbol rate and code length form one overlay.
`plan_adapter` gives each user the other users' codes (`overlay_codes` in its
demod parameters), and `decode_pool` passes them to the despreader.

For a BPSK single code user, `DSSSDemodulator._mui_cancel` estimates every
BPSK user of the overlay: timing over every offset by FFT correlation, carrier
offset slope, phase, amplitude, burst mask and hard symbols. It rebuilds their
chip streams, fits the common chip pulse (channel filter and resampler, 7
taps) by least squares, and runs three passes of parallel cancellation. Each
pass acquires every user again, timing included, on x minus the others.

The user is then despread by the normal path from x minus the other users.
The estimate is cached per buffer, so the K decodes of one window in a pool
worker estimate the set once. That takes about 0.5 to 2 s a window for 16
users on the x86 under load, then milliseconds. `demod_with_recovery` falls
back to the plain despread when the cancelled one does not validate. A code
that is in the plan but not on the air fails the BPSK quality gate (mean over
std of the symbol magnitude at least 2) and is not subtracted. Other carriers
(QPSK, OQPSK, pi/4, dual code) are not cancelled and do not cancel others.

Cancellation is needed because the overlay comes from one transmitter, so every user has
the same carrier phase, frame layout and nearly the same bits. The despread
interference is then a fixed real bias, not noise, and it is the same on
every repeat of a frame, so combining repeats does not help. Without
cancellation some users of the 16 user preset acquire the wrong chip timing
even at high SNR.

## 3. Scenario JSON

Per signal (any DSSS modulation):

```
{"modulation": "DSSS", "carrier": "oqpsk", "symbol_rate": 2400, "samples_per_chip": 2,
 "offset_hz": 300000, "message": "U03 HELLO", "user": "U03",
 "lfsr": {"poly": [6,1], "state": 1, "form": "galois", "length": 63},
 "lfsr_q": {...} | "code_spec_q": {...},        # optional dual code
 "chip_rate_hz": 151200}                        # instead of symbol_rate (= chips/s / L)
```

At spec level, `"peak_fs": 0.45` sets the peak of the whole built waveform as
a fraction of full scale. The range is 0.05 to 1.0 and the default is 0.45.
A build stops at 0.999. Each user's `amplitude` only
sets its level against the other users. The "peak (FS)" box in the Build row
writes this key.

The multi-user block is expanded by `gen_scenario_json.expand_users`, and also
by `validate_spec` and the service's `gen_scenario`:

```
{"name": "dsss_4user_bpsk", "sample_rate_hz": 1920000, "center_freq_hz": 425025000, "loop_s": 0.25,
 "code_set": {"kind": "gold", "n": 5, "count": 4, "length": 31},        # or "code_sets": {name: {...}}
 "dsss": {"symbol_rate": 2400, "samples_per_chip": 2, "offset_hz": 300000, "carrier": "bpsk",
          "amplitude": 0.8, "stagger_chips": 7, "spacing_hz": 0},
 "users": [{"id": "U01", "code": 0, "message": "U01 PING"},
           {"id": "U02", "code": 1, "carrier": "qpsk", "code_q": 2, "message": "U02 PING"},
           {"id": "U03", "set": "other", "code": 0, "offset_hz": 500000, "lfsr": {...}}],
 "signals": []}
```

Users share one centre (`spacing_hz` 0, the CDMA overlay) or are spread
apart by `spacing_hz`. `stagger_chips` starts user i `stagger_chips * i` chips
late, which breaks up the pile-up of chip synchronous sync words and lowers the
PAPR of the sum. Per user, `delay_s`, `amplitude`, `symbol_rate` or
`chip_rate_hz`, `frame`, `fec` and `decode_gate_db` override the defaults.

The plan gets one entry per user, named by the user id. Each entry has an
explicit `code_spec`, never a default (a DSSS entry without one does not
decode on the live G40), `dsss_modem`, `spread_factor` (the true code length),
a `band_hz` as wide as the chips, and `meta.user`, `meta.carrier` and
`meta.code`.

The plan's `dsss_overlay` block carries the set report and
`expected_mui_snr_db`. The channelizer holds every user as its own channel at
the shared slot (log line `[PLAN] CDMA overlay: N extra user(s) share a
slot`), and the decode pool despreads each with its own code from the same
baseband.

Presets (JSON in `scenarios/json/`):

```bash
python3 -m dpmg.scenario.gen_scenario_json --dsss-presets
```

| Preset | Users | Code set | Rate | Predicted equal-power MUI SNR |
|---|---|---|---|---|
| `dsss_4user_bpsk` | 4 x BPSK, one centre +300 kHz | Gold n=5, 31 chips | 2400 Bd (74.4 kchip/s) | >= 9.7 dB (peak xcorr 9/31) |
| `dsss_mixed_carriers` | BPSK, QPSK, OQPSK and pi4 users | Gold n=6, 63 chips | 2400 Bd (151.2 kchip/s) | >= 13.6 dB (peak 17/63) |
| `dsss_16user` | 16 x BPSK, short messages | Gold n=7, 127 chips | 1200 Bd (152.4 kchip/s) | >= 9.2 dB (peak 17/127) |

`tests/test_dsss_multiuser.py` builds each case, runs the worker's gate rate
and the channelizer, and calls `decode_pool._default_decode` per user, with
random arrival and phase, 25 dB AWGN, and a 250 Hz carrier offset (either
sign) drifting 5 Hz/s. It covers every carrier family, the dual code and alias
cases, `chip_rate_hz` and truncation, 4 users and mixed 4 of 4, and 16 users
per live sized window (2 x 131072 samples at 1.92 MSps, `HW_WINDOW_S`). The
16 user case must reach at least 13 of 16 in every window and every user over
the run. The test also checks the overlay with only some of the planned codes
on the air: every user on the air must decode and no user off the air may
give a false decode.

## 4. Engineering view panel

In the engineering view, open the SDR tab. The panel sits in the Live PSD
column below Tools, titled "DSSS builder: LFSR codes & multi-user overlay".
The operator console does not have it.

1. Code set: kind (Gold, m-sequence phases, distinct polynomials, custom
   list), degree, count, length, form, polynomials and seed. "Build codes"
   fills the code table (balance, auto peak, MUI SNR) and the N by N table of
   peak cross-correlation, with the worst pair and the predicted MUI. "Add
   custom LFSR" adds one explicit LFSR (poly, state, form, length, offset) to
   a custom set.
2. Users: common symbol rate, samples per chip, offset, spacing (0 for an
   overlay), default carrier, amplitude and stagger. Add users one by one (id,
   code number, carrier, message, amplitude) or with "Auto-fill N users". The
   chip rate and main lobe width update as you type.
3. Build: name, sample rate, centre and loop (or "rate/center from radio"),
   then:
   - "Preview spectrum" (Welch PSD of the sum built in the GUI process, PAPR,
     predicted MUI), "Scenario JSON" (the users form JSON), presets, and "Save
     JSON on this PC" (`scenarios/json/<name>.json`, which the playback panels
     and "Load scenario JSON" can load);
   - "Build on radio + load TX + push plan": runs `gen_scenario` on the
     radio, inline, or by a file push and `spec_inbox` when the spec is too
     big for one command datagram. The IQ becomes the TX waveform, then
     `set_plan` runs. Nothing keys until START TX is pressed.
4. Per-user decodes: one row per user (carrier, code, expected message). Each
   row shows the live state, last message and decode count from the
   WATCH_UPDATE rows, with a tick when the message matches. "Replay decode
   (offline check)" runs the same replay chain as the test on the built
   scenario (SNR and carrier offset adjustable) and fills ok, bit errors and
   despread SINR per user. Live rows need a running channelizer (RX
   streaming). The simulator receives nothing, so there only the replay
   column fills.

### Input checks (DSSS_VALIDATE)

`dsss_builder_validate.validate_builder(spec, radio_rate=None)` checks the
users form spec that the panel describes and returns `(level, field,
message)` items. The panel runs it again on every input change (0.3 s
debounce) and shows the items in the "Input checks" box above section 3:
errors red, warnings amber, passes grey, with a red outline on the input at
fault. While any error exists, Preview spectrum, Scenario JSON, Save JSON, Build on
radio and Replay are disabled. Build codes and Auto-fill are disabled only for
code set errors. Clicking a disabled button shows the first error. "Add
custom LFSR" refuses a bad code before adding it. Each message says what to
change.

| Level | Rule |
|---|---|
| error | degree outside the library's 2..16; empty degree or count |
| error | more users than the set offers: Gold n gives 2^n+1 codes, 2^n-1 as offsets 0..N-1; m-sequence phases 2^n-1; distinct polynomials 1 + `PRIMITIVE_ALT[n]` (suggests the degree that fits) |
| error | Gold n with no preferred pair (n divisible by 4, or not in `GOLD_PAIRS` = 5, 6, 7, 9, 10, 11) |
| error | explicit polynomial unreadable, not primitive (period shown), tap beyond degree n, wrong degree, Gold A = B; warning: pair not preferred |
| error | state or seed 0 (all-zero register) or wider than n bits |
| error | truncated length > period, or < 7 chips |
| error | code index outside the set; two users on one code on overlapping spectrum; dual code equal to the I code, on a bpsk or pi4 carrier, or of another length |
| error | symbol rate or chip rate <= 0; chip rate needs > fs / 2 (2 samples per chip); main lobe (offset plus or minus chip rate) beyond the usable 0.475 fs either side; samples per chip < 2, or odd for OQPSK |
| error | empty message, > 255 bytes, or more than one FEC block carries (for example TPC eH(16,11)^2 = 121 bits); unknown FEC type |
| error | burst longer than the TX loop (the loop would cut the frame) |
| error | amplitude <= 0 or > 1; empty or invalid name, sample rate, centre, loop |
| warning | chip rate x samples per chip above the sample rate (resampled down) |
| warning | users at different centres with overlapping main lobes |
| warning | live channelizer 2 frame window too short for a frame at this rate (the worker's `[NEED]` rule) |
| warning | predicted despread SINR (amplitude weighted MUI from `set_report`) below about 8 dB BPSK or 11 dB QPSK family |
| warning | equal power rule of thumb L/(N-1) under the decode level + 2 dB (16 users on 127 chips: 9.3 dB, about 13 users fit) |
| warning | codes that are shifts of each other (peak cross-correlation = L, for example m-sequence phases) |
| warning | amplitudes summing above 1 (the generator scales the sum to its peak setting, 0.45 by default, so nothing clips but every user is lower) and > 6 dB spread (near-far); radio rate differs from the scenario rate |

The three presets give no errors; the 4 user and mixed presets give the
amplitude sum warning, and the 16 user preset also the capacity warning
(`tests/test_dsss_builder_validate.py`).

## 5. Limits

- Code length, frame and decode window. The channelizer decodes a 2 frame
  window, and a frame must fit: `(frame_bits + 32) * samples_per_symbol <=
  window samples`. If not, the service log shows a `[NEED]` line saying the
  channel "can NEVER decode". With 2 samples per chip the window holds about
  `0.05 * symbol_rate` bits at 1.92 MSps, whatever the code length. Keep
  messages short at low symbol rates (the 16 user preset uses 1200 Bd and 3
  character messages) or raise the symbol rate. The 1.92 MSps input gives the
  longest window.
- Chip rate and sample rate. The main lobe spans plus or minus the chip rate,
  which is symbol_rate x L. The plan's band is 2 x chip rate and must sit
  inside the usable 95 % of plus or minus fs/2; `validate_spec` refuses it
  otherwise. The gate rate is raised to 2 samples per chip (log tag
  `SPREAD_FSOUT`). 16 users at 127 chips and 1200 Bd are 305 kHz wide; at 7.68
  MSps and above prefer 2400 Bd.
- Multi-user interference. Equal power users leave about L/(N-1) of despread
  SNR. Below about 8 dB (BPSK) or 11 dB (QPSK family) frames start to miss.
  Use longer codes, fewer users, FEC (`fec` per user) or unequal amplitudes.
- OQPSK needs an even `samples_per_chip` for the half chip stagger, and its
  dual code must have the same length. The pi/4-QPSK carrier offset range is
  plus or minus Rs/8 (the differential 4th power). Dual code OQPSK at 0 dB
  SNR per sample shows a few % BER where single code is clean.
- The simulator does not loop TX into RX, so live per-user decodes can only be
  checked on hardware or in replay.

## 6. Hardware recipe (Setup A, GPSDO)

Through the first checkout:

```bash
python3 bench/first_checkout.py --host <G40 address> --arm --clock gpsdo --pad-db 50 --only dsss_multiuser --dsss-multiuser
```

Or the DSSS script alone, replay first and then on the radio:

```bash
python3 bench/dsss_multiuser.py --replay --out output/bench/dsss_multiuser_replay.json
python3 bench/dsss_multiuser.py --host <G40 address> --arm --clock gpsdo --pad-db 50 --replay-json output/bench/dsss_multiuser_replay.json --out output/bench/dsss_multiuser.json
```

Setup A: TX TRx1B through the 50 dB pad to RX TRx1A, 1.92 MSps, +300 kHz,
clock gpsdo. The 3.3 V GPS antenna bias must be on and the GPSDO locked
before keying (the `checkout_common` gate). If the bias is off the harness
refuses to run, unless `--ant-bias` is added to let it switch the bias on.
Pass bars: MOD-DSSS-C 5 of 5 carriers, MOD-DSSS-4 4 of 4 users, MOD-DSSS-M
4 of 4 users, MOD-DSSS-16 recorded (at least 13 of 16 per window, all 16 over
the run).

In the GUI, open the DSSS builder panel in the engineering view's SDR tab and
work through section 4. The panel keys nothing; press START TX by hand to see
the live per-user rows.
