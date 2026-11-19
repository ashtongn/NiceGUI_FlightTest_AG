# Example files for the file playback card (FILE-07)

Built by make_example_files.py, center 1325 MHz, loop 0.25 s. Copy the whole folder to the radio as data/waveforms/flight/ (the card says how).

| File | Rate (MS/s) | Bytes | sha256 (first 16) | What it is |
|---|---|---|---|---|
| `fsk_pair_2m4.json` | 2.4 | 602 | c58f02a14b858c0b | scenario spec (A); the planner builds it on the radio |
| `fsk_pair_2m4.iq.bin` | 2.4 | 4800000 | 93b3f06e59262aa2 | A built: rate in fsk_pair_2m4.plan.json |
| `fsk_pair_2m4.plan.json` | 2.4 | 1140 | ee7af5d6bab82f0b | decode plan for A |
| `fsk_pair_2m4.cdif` | 2.4 | 4800648 | e105fec64c11f595 | B: BLUE header carries 2.4 MS/s and the center |
| `fsk_pair_2400k.iq.bin` | 2.4 | 4800000 | 93b3f06e59262aa2 | E: raw, the rate only in the file name (_2400k) |
| `fsk_pair_nomark.iq.bin` | 2.4 | 4800000 | 93b3f06e59262aa2 | F: raw, no rate marker; the operator must supply 2.4 MS/s |
| `qpsk_msg_5m.c64` | 5 | 10000000 | 50db70b849658c6d | C: raw complex64 recording, rate in the .c64.json sidecar |
| `qpsk_msg_5m.c64.json` | 5 | 197 | efe5fc800a3b1108 | sidecar for C |
| `bpsk_msg_1m92.iq.bin` | 1.92 | 3840000 | 9e64adc2f21d1ea1 | D: raw at a G40 rate, rate in bpsk_msg_1m92.plan.json |
| `bpsk_msg_1m92.plan.json` | 1.92 | 647 | d2115336df7a72c4 | decode plan for D |

Rates the G40 runs natively, from dpmg/radio/sdr_core.py: 1.92, 2.4576, 2.8, 3.84, 4.0, 4.9152, 5.6, 7.68 ... 61.44 MS/s. 2.4 and 5.0 MS/s are not in that list, so B, C, E and F must be resampled before they play.
