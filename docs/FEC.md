# FEC suite: CDM-625A class coding

`dpmg/fec/` is an FEC suite in plain numpy. It covers the coding options of
the Comtech CDM-625A modem. `dpmg/framing/frame_codec.py` uses it for the frame
FEC: `conv`, `rs`, `rs+conv`, `ldpc` and `tpc`.

The codes do the same job as the Comtech ones but are not bit compatible.
Nothing here interoperates with a real Comtech modem on the air:

- RS uses the conventional (Berlekamp) basis, not the CCSDS dual basis.
- LDPC uses its own QC array codes, not the proprietary VersaFEC matrices.
- The ACM Es/N0 thresholds are nominal reference values by default. A measured
  table is available as an option (see ACM Es/N0 thresholds).

Each module states these limits in its header. In the whole package an LLR
(log likelihood ratio) above 0 favours bit 1, as in `higher_order.soft_llr` and
`HigherOrderDemodulator`. `dpmg/fec/channel.py` holds the shared AWGN and LLR
test harness.

## Tiers

| Tier | Module | Test | What it is |
|---|---|---|---|
| 1 Convolutional, soft Viterbi | `conv.py` | `test_fec_conv.py` | K=7 (X=0o171, Y=0o133), punctured 1/2 to 7/8 with the DVB-S patterns, soft decision Viterbi |
| 2 Reed-Solomon, concatenated | `rs.py` | `test_fec_rs.py` | RS(255,223) over GF(2^8), Berlekamp-Massey, Chien and Forney, block interleaver, RS plus Viterbi concatenation; corrects up to t=16 symbol errors per block |
| 3 LDPC min-sum | `ldpc.py` | `test_fec_ldpc.py` | Regular QC-LDPC array codes (girth at least 6), normalized min-sum, `qc-r12`, `qc-r23`, `qc-r34` |
| 4 TPC (Chase-Pyndiah) | `tpc.py` | `test_fec_tpc.py` | 2D product of extended Hamming components, Chase-II with Pyndiah SISO iteration |
| 5 ACM controller | `acm.py` | `test_fec_acm.py` | 9 rung MODCOD ladder, selector with hysteresis against flapping, export to the plan config |
| Integration | none | `test_fec_integration.py` | coded bits through a real 8PSK or 16QAM modulator and `soft_llr` into tier 1 and ACM |

Each test runs its tier through the AWGN harness and prints the BER it
reaches, so the coding gain of a tier can be read from its own run.

## Running the tests

```bash
python3 tests/test_fec_conv.py
python3 tests/test_fec_rs.py
python3 tests/test_fec_ldpc.py
python3 tests/test_fec_tpc.py
python3 tests/test_fec_acm.py
python3 tests/test_fec_integration.py
python3 tests/test_acm_thresholds.py
```

All are seeded, so they repeat exactly, and they print BER figures as well as
pass or fail.

## ACM GUI tab (disabled)

`build_acm_tab` (marker `ACM_GUI` in `dpmg/web/nicegui_app.py`) shows the 9
rung MODCOD ladder: BPSK, QPSK, 8PSK, 16APSK and 32APSK with Viterbi rates 1/2
to 7/8, increasing in both Es/N0 and spectral efficiency. Below it is a live
readout of SNR, margin and the selected MODCOD. The tab is a read only planning
aid. It is commented out of the tab bar (marker `ACM_MOTHBALL`), but
`build_acm_tab` and the engine stay in the tree and work if the tab is wired
back in. `test_fec_acm.py` covers `acm.py`. Its hysteresis turns a noisy SNR sequence
into 1 switch instead of about 100 flaps, and `to_plan_config` writes the
channel plan JSON.

## ACM Es/N0 thresholds

`acm.py` holds two threshold tables. `ACM_TABLE` carries the nominal
reference values and is the default. `acm.MEASURED_ESN0_DB` is the second
table: the Es/N0 at which each conv rung is taken to decode through DPMG's
own decode path (scenario build, GPU style channelizer,
`decode_pool._default_decode` with hard decision Viterbi), with 1.0 dB of
margin on top of the margin and hysteresis inside `AcmController`. It comes
from `bench/acm_threshold_sweep.py --replay`, whose result file is
`tests/data/acm_thresholds_replay.json`. These are the values the code uses:

| MODCOD | `ACM_TABLE` dB | `MEASURED_ESN0_DB` dB |
|---|---|---|
| BPSK-conv12 | 1 | none (the rung is dropped) |
| QPSK-conv12 | 3 | 33.0 |
| QPSK-conv34 | 5 | 27.0 |
| 8PSK-conv23 | 7 | 17.0 |
| 8PSK-conv34 | 8.5 | 17.0 |
| 16APSK-conv34 | 10.5 | 22.0 |
| 16APSK-conv56 | 11.5 | 21.0 |
| 32APSK-conv34 | 12.5 | 23.0 |
| 32APSK-conv78 | 14 | 22.5 |

`acm.acm_table("measured")` builds a ladder from the second table. It drops
BPSK 1/2 (no value) and every rung that a faster rung beats at no more Es/N0,
and re-indexes what is left so the ladder stays strictly increasing in both
Es/N0 and spectral efficiency: 8PSK 3/4 at 17.0, 16APSK 5/6 at 21.0 and
32APSK 7/8 at 22.5 dB.

Pick that ladder per call with
`select_modcod(..., table=acm.acm_table("measured"))` or
`AcmController(table=...)`, or for the whole process with
`DPMG_ACM_THRESHOLDS=measured`. `tests/test_acm_thresholds.py` checks the
default, the pruning and ordering, the environment switch, the fallback, and
that the values in `acm.py` match the result file.

## Known limitations

- The second table sits far above the nominal one. The decode path sets the
  edge (hard decision Viterbi for PSK and QAM, and acquisition), not the code,
  so the nominal table says what the codes can do and the second what this
  decode path does with them.
- On RF the stock 12 dB decode gate clips any edge below about 19 dB Es/N0.
  The RF ladder is scripted in `bench/first_checkout.py --only acm_ladder`
  (or `--acm-ladder`).
- RS is not CCSDS dual basis. The ACM ladder has no LDPC or TPC rungs, though
  the dispatch in `acm.py` handles LDPC.
