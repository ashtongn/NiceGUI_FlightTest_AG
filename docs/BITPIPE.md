# Bitpipe: the bit level communications container

## What the bitpipe is

The DPMG link normally carries separate messages. The bitpipe, in
`dpmg/bitpipe/`, turns it into a continuous stream. On transmit, application
bytes go in and a bit stream comes out, cut into fixed length frames, coded
with FEC and, if asked, encrypted. Receive runs the same steps in reverse.
The same code runs at both ends, on a radio host or on the laptop.

In this document "container" means the `BitpipeContainer` class, not a Docker
container. A container is one Python process with three sockets, called
faces. Programs send and receive bytes on the application face. A modem pulls
frames to transmit from the bit face and pushes back the bits it received.
The control face answers JSON commands.

The package has three parts: the container, which needs no radio; the stream
modem, which turns bits into IQ samples and back, with its wiring into the
radio service; and the laptop peer with the link panel.

With real radios the container runs as a plain process on the laptop
(`bench/bitpipe_hw.py` or `python3 -m dpmg.bitpipe.peer`) and the stream
modem inside the radio service, in the radio's Docker container: `dpmg_svc`
on the G40, `dpmg-b200` on the Super Nano. The offline loopback is one
process on the laptop, with no radio and no Docker. The modem offers four
waveforms (2-FSK, 4FSK, BPSK, QPSK), on a one radio loopback or a cabled two
radio link (see Two radio links).

## Quick start

Run every command from the repo root. The offline loopback:

```bash
python3 -m dpmg.bitpipe.loopback
python3 -m dpmg.bitpipe.loopback --secure --ber 0.005 --drop 0.03 --payload 128
python3 -m dpmg.bitpipe.loopback --fec rs --payload 100 --file-kb 32 --transport unix
```

It runs two containers back to back. A relay plays both modems: it holds the
clock (`--bit-rate`, 48000 bit/s by default), pulls one frame per period from
each container, flips bits (`--ber`) and loses or cuts frames (`--drop`), and
hands each frame to the other container. Text goes both ways on the chat
ports, then a file each way on the file ports. The run passes when every
byte arrives intact. `--secure` uses AES-256-GCM-SIV with throw away keys.

One container on its own, with default settings (64 byte payload, conv 1/2,
clear), stopped with Ctrl-C:

```bash
python3 -m dpmg.bitpipe.peer
```

It listens on 127.0.0.1: control on 47898, the bit face on 47899, and stream
n on 47900 + n (1 chat, 2 files, 3 reserved for audio). From another shell,
the first line is a chat terminal and the second opens the control face:

```bash
nc 127.0.0.1 47901
nc 127.0.0.1 47898
```

The control face takes one JSON object per line, such as
`{"cmd": "status"}` or `{"cmd": "send", "stream": 1, "text": "hi"}`. The
commands are `status`, `send` (data as `text` or `hex`), `ping` (its round
trip shows in the status) and `stop`. With `--transport unix` the faces are
socket files under `/tmp/dpmg-bitpipe` named `<name>-app1.sock`,
`<name>-bit.sock` and `<name>-ctl.sock`. The peer's default name is
`ground`.

## How it fits together

### The bit face

Every face binds through one `Endpoint` type: TCP on 127.0.0.1 by default,
or a Unix socket. One setting, `transport`, picks for all faces. TCP sockets
use TCP_NODELAY and 16 kB buffers, so the ARQ never sees seconds of queued
frames.

The bit face carries records: the magic "BP", a type byte, a 4 byte length,
then the payload. A reader that loses its place skips forward to the next
magic.

| Record | Direction | Meaning |
|---|---|---|
| `REC_PULL` | modem to container | Bits wanted. The container answers with whole frames until at least that many bits are sent, idle frames when it has no data. |
| `REC_BITS` | both | Bit count, then packed bits. From the container, one whole frame. From the modem, any chunk of the received stream. |
| `REC_SOFT8` | modem to container | One signed byte per bit (soft decision): +127 a certain 1, -127 a certain 0. |
| `REC_HELLO` | both, on connect | JSON. The container gives block bits, payload bytes, FEC and crypto. The modem gives its format and expected frame size. |
| `REC_STATUS` | modem to container | Empty request. The container answers with its status JSON. |
| `REC_MODEM` | modem to container | The modem's status JSON (waveform, carrier offset, eye, underruns), about once a second, kept in the container's status. |

The modem is the only clock. It pulls at its symbol rate, and the container
always answers, with an idle frame if nothing is queued, so the RF stream
never has a gap. The bitpipe has no pacing timer; back pressure is the
socket. It hands over bits, never symbols, so the modulation is the modem's
choice. `bit_framing="raw"` drops the records and pushes packed bits as fast
as the socket takes them. That is for a file or `nc`, not a modem.

### What is in the stream

```
sync(32)  interleaved FEC block of one fixed length frame
frame = flags(1) stream(1) seq(2) ack(2) sack(2) len(1) payload(P) crc(2|4)
```

- Flags: version in the top nibble; bit 0 idle, bit 1 data, bit 2
  retransmit; 0x08 for a frame from station 1 (see Two radio links).
- Sync. Frames have a fixed length, so the receiver finds the sync once and
  then flywheels, expecting each frame one block after the last. The sync is
  the CCSDS 32 bit marker 1ACFFC1D, found with up to 3 bit errors. A frame
  whose CRC passes keeps the lock, whatever its sync looked like.
- Acknowledgements. Every frame, idle frames included, carries `ack` (the
  next sequence expected from the peer) and `sack` (16 bits for the frames
  after it), so the reverse direction is the acknowledgement channel. Idle
  frames go out when nothing is queued, so the far end keeps lock.
- Payload. With `crypto="secure"` it is the sealed body from
  `SecureContext.seal` (AES-256-GCM-SIV, `dpmg/secure/secure_frame.py`); with
  `crypto="none"` it is the plain inner record, stream(1) seq(2) data. After
  the authentication tag verifies, the receiver checks the inner stream and
  sequence against the clear header, so the AEAD (authenticated encryption)
  catches a swapped header.
- Order on transmit: segment, mux, seal, header and CRC, FEC, interleave,
  sync. Receive is the reverse. The ARQ removes duplicates and reorders
  before the open, so each sealed body reaches the replay table once and in
  order. A retransmission re-sends the original sealed bytes and spends no
  new nonce.
- FEC. One frame is one block of `dpmg/fec`: conv 1/2 to 7/8, rs, rs+conv,
  ldpc or tpc, with the fec dicts `dpmg/modem/modem.py` uses. LDPC and TPC
  bound the frame (qc-r12 fits 41 bytes, eH(32,26)^2 84 bytes), and the
  config check says so.
- Interleaver. A bit level block interleaver over the coded frame, with the
  row and column convention of `rs.block_interleave`.
- Whitening. The coded block is XORed with a fixed PN sequence, sync left
  clear, so idle frames still give the modem's timing and carrier loops
  balanced transitions.
- ARQ (automatic repeat request): selective repeat, timed in frame slots. A
  pending frame goes again 2 slots after a later frame is acknowledged past
  it, or on timeout. A slow link stretches the timeout to 1.5 times the
  measured round trip plus 2 slots. After `max_retries` tries the engine
  reports `link_down` and keeps retrying.

With the defaults a frame is 75 bytes (9 header, 64 payload, 2 CRC) and 1248
bits on the stream, sync included. It carries 61 data bytes clear or 38
encrypted. At 2400 bit/s that is about two frames a second.

## Configuration

Settings are fields of `BitpipeConfig` in `dpmg/bitpipe/config.py`;
`validate()` checks them and returns the frame sizes.

| Setting | Default | Meaning |
|---|---|---|
| `payload_bytes` | 64 | payload field, 1 to 232 |
| `fec` | conv 1/2 | FEC per frame, `None` for none |
| `interleave_depth` | 8 | 1 is off |
| `crc` | CRC-16-CCITT | or CRC-32 |
| `sync_max_errors`, `lock_miss_limit` | 3, 4 | sync search errors; bad frames in a row before a new search |
| `window` | 16 | frames in flight, 16 at most |
| `rto_frames`, `rto_max_frames` | 6, 64 | retransmit timeout in frame slots, and its ceiling |
| `max_retries` | 50 | tries before `link_down` |
| `station` | None | 0 or 1 on the two ends of a two radio link |
| `crypto`, `key_provider`, `key_file` | none, file, None | see Keys |
| `transport`, `unix_dir` | tcp, /tmp/dpmg-bitpipe | tcp or unix sockets for every face |
| `app_base_port`, `bit_port`, `ctl_port` | 47900, 47899, 47898 | TCP ports |
| `bit_connect` | None | dial out to the modem ("tcp:host:port" or "unix:/path") instead of listening |
| `bit_framing` | records | or raw |

## Keys

### Key source

With `crypto="secure"` the keys come from a key provider:

- `FileKeyProvider` reads the operator key file named by `DPMG_KEY_FILE` (or the
  `key_file` setting). The file stays on the laptop, never on a radio.
- `StaticKeyProvider` takes the keys as a Python dict instead of a file. The
  offline loopback and the tests use it.
- `DeviceKeyProvider` is for keys unlocked by the Jetson's secure boot and
  fuses. It is not implemented, so it refuses every request.

The bitpipe itself only calls `seal` and `open`; it never handles the keys.

Key custody: keys stay on the x86 and the radios never open frames. A Jetson
device key provider would plug in through the same `KeyProvider` interface.
A process that seals or opens frames needs `DPMG_SECURE_MSG=1`; the peer, the
loopback and `bench/bitpipe_hw.py` set it themselves.

## The radio side

### The stream modem

The stream modem (`stream_modem.py`, `modem_link.py`, `sim_link.py`) lives in
the bitpipe package, so the radio service, the offline harness and a laptop
peer share it. `ModemLink` is the pull client of a container's bit face. The
radio code calls `next_tx_block()` for the next block of gapless TX IQ, and
`on_rx(iq, fs)` to turn RX IQ into soft bits for the container. With no
container connected, TX blocks are silence and count as underruns, so do not
key the radio then.

Waveforms, in order of maturity: 2-FSK, 4FSK, BPSK, QPSK. Tone and
constellation conventions follow `dpmg/modulation/modulator.py`. BPSK and
QPSK are differentially encoded, so the carrier loop's 180 or 90 degree
ambiguity never reaches the bits.

The demodulator carries its state between calls. Each call it:

1. mixes down with an NCO (phase carried);
2. decimates with a FIR to about 16 samples a symbol (filter tail and
   decimation phase carried);
3. takes symbol timing from the spectral line of the transition energy
   (accumulator carried), so chunks of any size and alignment work;
4. detects. FSK compares the instantaneous frequency, averaged over the
   symbol, with a tracked threshold (4FSK with four tracked levels). PSK
   takes a coarse carrier estimate from the M-th power over at least 128
   symbols, then runs a decision directed loop and differential detection.

The message demodulators in `dpmg/modulation` re-acquire timing on every
call, so a stream cut into chunks comes out of them as random bits.

### Service commands

In the radio service (`dpmg/radio/dpmg_service.py`, `dpmg/radio/sdr_core.py`):

- `FullDuplexScheduler.tx_source` is a callable that returns the next TX
  block. When set, the TX loop sends it instead of repeating the cached
  waveform. `set_tx_source` refuses to swap it while TX runs.
- `bitpipe_start` attaches a stream modem. With `container` (default
  tcp:127.0.0.1:47899) the modem dials the container. With `listen` (such as
  tcp:0.0.0.0:47899) it listens instead, and `allow_from` limits it to one
  address. Waveform parameters: `modulation` (FSK, which is 2-FSK and the
  default, 4FSK, BPSK, QPSK), `symbol_rate` (2400), `deviation_hz` (1200) or
  `tones`, `dibit_mapping`, `offset_hz`, `amplitude` (0.5), `rx_sps` (16),
  `block_s` (0.05) and `block_multiple`.
- Nothing radiates on `bitpipe_start`. The stream keys on the usual
  `start_tx`, behind every existing TX guard. `bitpipe_start` is refused
  before `init`, while TX runs and when a modem is attached. While one is
  attached the radio role can only change to `any` or `bitpipe`.
- `bitpipe_stop` stops TX, clears the TX source and restores the RX hook.
  `close` also tears the link down.
- `bitpipe_status` gives the modem stats, and the container's status with
  `container: true`. The acks carry summaries of about 300 to 550 bytes, so
  each fits one datagram; the full status is on the container's control face.
- The mock backend's TX to RX loopback (`extras.tx_loopback` with
  `loopback_cfo_hz` and `loopback_snr_db`, or `DPMG_MOCK_TX_LOOPBACK=1`) lets
  a simulated service hear its own stream.

### What a live stream needs from the radio

Two things the G40 TX path needs from a stream:

- Block length. The G40 bridge zero pads every send to a whole card TX block
  (8188 samples in single mode below 20 MS/s). Any other modem block length
  leaves a hole on every block and the receiver slips. `bitpipe_start` sizes
  modem blocks to a whole number of the backend's `tx_block_samples`
  (`block_multiple` overrides). The mock pads the same way with
  `extras.mock_tx_block`.
- Buffer lifetime. With async TX, `skiq_transmit` queues pointers into the
  prepared buffer and returns. A stream replaces that buffer every send, so
  the bridge keeps replaced buffers until newer sends cover twice the 50
  packet queue. Otherwise the queue could read freed memory mid-stream. A
  repeating cached waveform was never affected.

Three more for the B200:

- Fractional symbol timing. Whole samples per symbol is exact at 1.92 MS/s
  (800) but not at 2 MS/s (833.33), where it gives 2400.96 Bd, 400 ppm off
  the receiver. Symbol edges fall on a carried fractional sample clock,
  so the air rate is exact on any radio and a G40 at 1.92 MS/s and a B200 at
  2 MS/s run at the same rate.
- UHD bursts. Each block as its own burst puts a seam in a continuous
  stream. While a live TX source feeds it, the
  scheduler sets `tx_stream_mode` and the backend keeps one burst open until
  `end_tx_burst()` at TX stop (`tests/test_uhd_stream_tx.py`).
- Rate guard. The `start_tx` rate guard compares the radio rate with the last
  cached waveform, which the stream never plays. The service skips the check
  while a live TX source is set. For services without that change the
  harness passes `allow_rate_mismatch`.

A TX underflow on the B200 loses frames. Once a UHD send returns only a few
ms are left in the transport, so a block built after the send can starve the
radio. The G40 hides this behind the bridge's 50 packet async queue.
`UhdBackend` in `dpmg/radio/sdr_core.py` (here and in the Super Nano repo)
guards against it:

- TX queue. In stream mode `send()` puts the block on a queue of
  `TX_QUEUE_BLOCKS` (3, extras `tx_queue_blocks`) and returns; a sender
  thread feeds UHD. The scheduler builds ahead until the queue is full,
  which paces the source to the radio: 150 ms of lead at 50 ms blocks.
  `end_tx_burst()` sends what is queued, then the end of burst. A repeated
  waveform still goes out as whole bursts.
- Underflow counting. After every send the backend reads UHD's async TX
  reports with a zero timeout. Underflows add to `health.underruns` (the
  STATUS count the harness guards on) and `tx_underflows`; sequence and time
  errors are kept apart. `get_config` carries `tx_async` {underflows,
  seq_errors, time_errors, off}.
- `tests/test_uhd_tx_queue.py`: a fake radio that plays in real time. Without
  the queue a slow block leaves it starved for 5 ms after a send, and no
  underflow report is counted; with the queue it is never starved.

### Offline check

```bash
python3 -m dpmg.bitpipe.sim_link --all --snr 10 --cfo 200 --ppm 10
```

It runs every waveform through two containers, two modem links and a
simulated channel (noise, carrier offset and clock offset as given), with
text and a file each way, sealed, at 2400 Bd, and reports the underruns and
the carrier offset each demodulator tracks. On the simulated service
(`--simulate` with the mock TX loopback) a stream makes the full round trip:
scheduler TX source, mock send, loopback, RX frame hook, demodulator.

## Laptop peer and link panel

`python3 -m dpmg.bitpipe.peer` runs the ground station's container. `--panel
PORT` serves the link panel. `--sim` adds an offline far end: a second
container, two modem links and a simulated channel (QPSK, 10 dB SNR, 200 Hz
and 10 ppm offsets by default).

In the GUI the panel is its own view, `/bitpipe` (marker BITPIPE_VIEW in
`dpmg/web/nicegui_app.py`). `DPMG_BITPIPE_CTL`, `DPMG_BITPIPE_CHAT` and
`DPMG_BITPIPE_FILE` point it at the laptop container; the defaults are the
peer's TCP ports on 127.0.0.1.

The panel polls the container's status once a second and shows:

- Link state: LINKED (modem on the bit face, locked, peer heard), SEARCHING,
  NO MODEM (nothing on the bit face), LINK DOWN (retries exhausted), NO
  CONTAINER.
- Waveform, from the `REC_MODEM` status: modulation, symbol rate, carrier
  offset (PSK only), eye opening, TX underruns.
- Flow: payload rate each way, frames sent (data, idle, retransmitted),
  frames heard and bad, bytes queued.
- Quality: BER before FEC (each frame that passes the CRC is re-encoded and
  compared with the hard decisions that arrived, last 64 frames), frame error
  rate over 30 s (frames the bit count says arrived against frames that
  passed the CRC), share of frames sent again, round trip from the
  acknowledgements, and a ping on the control stream (stream 0) that
  includes queueing, both modems and the ARQ.
- A two minute chart of BER, frame error rate and round trip.
- Chat on stream 1 and files on stream 2. Received files go to
  `~/.dpmg/bitpipe/inbox` after the sha256 check.

The offline demo shows both ends side by side at http://127.0.0.1:8095.
Without a key file, `--sim` uses throw away keys.

```bash
python3 -m dpmg.bitpipe.peer --sim --panel 8095 --crypto secure --snr -3
```

With it running, both panels show LINKED and the carrier offset each end
tracks, a chat line typed in one panel appears in the other, and a file sent
from one side arrives in the other's inbox with a progress line and the
sha256 check.

### Connecting to a radio

The laptop runs ufw with inbound denied by default, so the radio's modem
listens and the laptop container dials out:

- radio: `bitpipe_start listen=tcp:0.0.0.0:47899 allow_from=<laptop ip>`;
- laptop: a container with `bit_connect=tcp:<radio ip>:47899`.

Replies ride the laptop's outbound connection, and `allow_from` makes the
modem refuse any other peer. `bench/bitpipe_hw.py` sets up its containers
this way; `dpmg.bitpipe.peer` has no `bit_connect` option. The bit face
carries only ciphertext when the link is secure, and plain data when it is
not.

The other direction needs an inbound ufw rule for the bit port. The laptop
runs the line below and the radio gets
`bitpipe_start container=tcp:<laptop ip>:47899`:

```bash
python3 -m dpmg.bitpipe.peer --bit-host 0.0.0.0
```

### Hardware harness

`bench/bitpipe_hw.py` runs the bitpipe on real radios (FSK in `--mods` is
2-FSK):

```bash
python3 bench/bitpipe_hw.py --host <G40 address> probe
python3 bench/bitpipe_hw.py --host <G40 address> path
python3 bench/bitpipe_hw.py --host <G40 address> --arm run --mods FSK 4FSK BPSK QPSK
python3 bench/bitpipe_hw.py --kind b200 --host 192.168.55.1 --port 47812 --arm run --mods FSK 4FSK BPSK QPSK
python3 bench/bitpipe_hw.py pair
python3 bench/bitpipe_hw.py --arm pair --mods FSK QPSK --soak-s 60 --crypto secure --key-file <key file>
python3 bench/bitpipe_hw.py --arm pair --mods FSK --soak-s 10 --fault-s 10 --fault-radio B200
```

`probe` sends hello, get_config and bitpipe_status. `path` runs init,
start_rx and bitpipe_start and checks that the modem reaches the container.
`pair` without `--arm` checks both bit faces. `restore` re-inits a radio and
starts RX. Only `--arm` keys TX. The container dials out unless `--dial-in`
is given. The harness stops TX at once on an underrun burst (modem or card),
a front end refusal, TX dropping, or lost lock. It always ends with stop_tx,
bitpipe_stop and stop_rx, and writes JSON and Markdown reports to
`bench/results/bitpipe/` (or `--out`).

## Two radio links

### The station flag

On a two radio link each radio's RX hears some of its own TX, inside the
radio and through the cabling. While the far radio is silent that leak
decodes, and a container would take its own frames for the peer's: it would
deliver its own data to itself, and its acknowledgements, now counting its
own sequence space, would tell the other end that frames lost at a TX stop
had arrived, so they were never resent. On a one radio loopback the container
is meant to hear itself, so the engine cannot refuse its own frames outright.

The `station` setting, 0 or 1 for the two ends of a two radio link (the
harness uses 0 for the G40 and 1 for the B200), settles it. Frames from
station 1 carry flag bit 0x08, and an end drops frames with its own bit. They
keep the deframer locked but carry nothing for it, and count as `own_rx`. The
status also gives `peer_age_s`, the time since the last frame from the peer.
That is the true link down signal when an end can lock on its own leak. With
no station set nothing changes, so the one radio loopback still works.
`tests/test_bitpipe_link.py` covers it: its outage test stalls without
stations (0 retransmits, the file never completes) and completes with them.

The harness runs one container per radio on the laptop, each dialling out to
its own radio's modem, so the G40's container talks to the B200's through
both radios. Chat, a file and the soak run both ways at once, timed per
direction. `--fault-s N --fault-radio B200` (or `G40`) stops that radio's TX
for N seconds in the middle of the file transfer, then keys it again, to show
the link going down and recovering. The link counts as down after 2 s with no
frame from the peer. At the end TX and RX stop on both radios, the B200 is
closed and the G40 is left initialised and idle.

The radios need no common sample rate: the fractional symbol clock gives
2400 Bd from a G40 at 1.92 MS/s and a B200 at 2 MS/s alike. The stream modem
is meant for low rates; at 30.72 MS/s modulation and demodulation of one
second of stream take longer than a second on the laptop, and the radio hosts
are slower, so run the stream at 1.92 MS/s.

### Carrier offset reporting

Every waveform's demodulator fills in `cfo_hz` in the `REC_MODEM` status. The
PSK demodulator reports its M-th power coarse estimate plus what the decision
directed loop still tracks. After the offset mixer the 2-FSK threshold sits
midway between the tones, so it is the carrier offset, and the 4FSK centre
likewise. The 4FSK figure is good to a few tens of hertz, not to the hertz:
the four tone levels are not used equally, which moves the percentile
midpoint. `test_cfo_reported_each_waveform` and
`test_cfo_reported_with_offset_mixer` in `tests/test_bitpipe_modem.py` cover
2-FSK and 4FSK. An older modem leaves `cfo_hz` at 0 for the FSK waveforms,
which reads as a real zero.

### Encrypted links

Each laptop container gets a `SecureContext` from the same key file, with its
own sender id (the harness uses 1 for the G40 end and 2 for the B200 end).
The keys stay on the laptop; the radios only carry sealed frames. The harness
refuses `--crypto secure` without `--key-file`; use a test key file made for
it, not the operator key file. A sealed file takes about 1.5 times as long as
one in the clear, because the seal leaves 38 data bytes in each 64 byte
payload instead of 61.

### Dry run without radios

`bench/bitpipe_pair_sim.py` runs one simulated service and sends its mock TX
to a second one over local TCP, resampled to the peer's rate and offset in
frequency. Run each line in its own shell:

```bash
python3 bench/bitpipe_pair_sim.py --serial SIM-G40 --cmd-port 48210 --rate-hint 1.92e6 --air-in 48401 --air-out 48402 --peer-rate 2e6 --cfo 224
python3 bench/bitpipe_pair_sim.py --serial SIM-B200 --kind b200 --cmd-port 48212 --rate-hint 2e6 --air-in 48402 --air-out 48401 --peer-rate 1.92e6 --cfo -223
```

## Troubleshooting

- G40 receiver slips once per block: modem blocks are not a whole number of
  card TX blocks. Check for a `block_multiple` override.
- `bitpipe_start` refused: radio not initialised, TX running, or a modem
  already attached.
- Radio keys but sends silence and counts underruns: no container connected.
- B200 frame errors with 0 underruns: look for U marks in the service log. A
  backend without the TX queue fix does not count them.
- Two radio link, a file never finishes, every frame shows as acknowledged:
  set `station` 0 on one end and 1 on the other. Judge the peer by
  `peer_age_s`, not by lock.
- Container and modem never connect: the laptop's ufw drops inbound, so use
  `listen` on the radio and `bit_connect` on the laptop. With `allow_from`
  the modem logs "refused a container from" for any other address.
- Config check refuses the frame: it does not fit one LDPC or TPC block.
  Shrink `payload_bytes`.
- Sealing fails in your own script: set `DPMG_SECURE_MSG=1`.

## Files

In `dpmg/bitpipe/` unless a path is given:

| File | What |
|---|---|
| `config.py` | `BitpipeConfig`, `FrameSizes`, the config check, endpoints per face |
| `transport.py` | `Endpoint` (tcp or unix), the bit face records |
| `frame.py` | frame pack and unpack, CRC (crc_util), inner record, sequence arithmetic |
| `coding.py` | one frame as one FEC block via `dpmg.fec`, bit interleaver |
| `crypto.py` | `NullCrypto`, `SecureContextCrypto`, key providers, throw away loopback keys |
| `deframer.py` | sync search and flywheel over soft bits |
| `arq.py` | `TxWindow`, `RxReorder` |
| `engine.py` | `LinkEngine`: the whole pipe, no sockets or threads |
| `streams.py` | stream ids, the file record (name, size, sha256) |
| `container.py` | `BitpipeContainer`: application, bit and control faces |
| `loopback.py` | two containers back to back, a relay as the clocked modem, a software channel |
| `stream_modem.py` | `StreamModulator` (bits to IQ, FSK phase and PSK reference carried), `StreamDemodulator` (IQ to soft bits, state carried), `IqChannel` (AWGN, carrier offset, clock offset, delay) |
| `modem_link.py` | `ModemLink`, the pull client of a bit face |
| `sim_link.py` | two containers, two modem links, an IQ channel each way: the whole link in software |
| `peer.py` | the ground station's container from the command line |
| `link_panel.py` | the NiceGUI panel for one container |
| `ctl_client.py` | `CtlClient` (control face JSON lines) and `StreamClient` (one application stream) |
| `bench/bitpipe_hw.py` | hardware harness |
| `bench/bitpipe_pair_sim.py` | simulated radio service for pair dry runs |

The tests in `tests/` each run as a script: `test_bitpipe_frames.py`,
`test_bitpipe_link.py`, `test_bitpipe_transport.py`,
`test_bitpipe_loopback.py` (plan rows BP-08 and BP-09),
`test_bitpipe_modem.py` and `test_bitpipe_service.py` (stream modem),
`test_bitpipe_panel.py` (link panel), and for the B200 backend
`test_uhd_stream_tx.py` and `test_uhd_tx_queue.py`. All bitpipe tests:

```bash
for t in tests/test_bitpipe_*.py; do python3 "$t" || echo "failed: $t"; done
```
