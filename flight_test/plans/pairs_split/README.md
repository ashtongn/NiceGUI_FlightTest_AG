# G40 pair plans, split by direction

Written by make_split_pair_plans.py. The downlink and the uplink of `pairs/g40_pair` as two
files, so each runs on its own. Center 1325 MHz, 1.92 MS/s on both radios, payload
G40-AX3G, ground radio G40-AX35. One pass.

| File | Plan name | Keys | Keyed steps | Ends | Check |
|---|---|---|---|---|---|
| `g40_payload_tx.dpmg-mission.json` | Pair G40, payload transmits: G40-AX3G keys, G40-AX35 decodes | G40-AX3G | 12 | 05:24 | valid |
| `g40_payload_rx.dpmg-mission.json` | Pair G40, payload receives: G40-AX35 keys, G40-AX3G decodes | G40-AX35 | 7 | 03:34 | valid |

## Which radio does what

| | g40_payload_tx | g40_payload_rx |
|---|---|---|
| G40-AX3G | keys on TRx2B through the HPA, 30 dB | receives on TRx1A, RX attenuator 0 dB |
| G40-AX35 | receives on TRx1A | keys on TRx1B, 0 dB |
| HPA | keyed on every step | stays off |

In g40_payload_rx the ground G40 has no amplifier: at 0 dB TX attenuation it gives about
+10 dBm into its antenna cable. If the payload's decodes are weak, that is the first limit.
If they are too strong with the radios close together on the ground, raise the RX
attenuator with `--air-rx-atten 20` and load the file again.

## Run it

1. Both radios connected. The payload on its antenna, the ground G40 on its own antenna.
2. Mission view, Open file. Check plan: no errors.
3. Run mission, read the confirm, key it. Nothing keys in the first minute.
4. Watch Decodes on the receiving radio: one message per waveform, in the order below.
5. STOP ALL TX to end early.

Run one file, let it finish, then open the other. Going from payload transmits to payload
receives, the payload G40 changes jacks in the first minute of the second file.

## The waveforms

### g40_payload_tx

| No | Keys at | Waveform | Rate | Offset (kHz) | Message |
|---|---|---|---|---|---|
| 1 | 01:08 | FSK | 4800 Bd | -400 | COMM FSK |
| 2 | 01:30 | 4FSK | 4800 Bd | -300 | COMM 4FSK |
| 3 | 01:52 | BPSK | 9600 Bd | -200 | COMM BPSK |
| 4 | 02:14 | QPSK | 9600 Bd | -100 | COMM QPSK |
| 5 | 02:36 | OQPSK | 9600 Bd | +500 | COMM OQPSK |
| 6 | 02:58 | 8PSK | 9600 Bd | +100 | COMM 8PSK |
| 7 | 03:20 | 16QAM | 9600 Bd | +200 | COMM 16QAM |
| 8 | 03:42 | QPSK | 9600 Bd | +300 | COMM QPSK CONV 1/2 |
| 9 | 04:04 | FSK | 4800 Bd | +400 | COMM FSK CONV 1/2 |
| 10 | 04:26 | DSSS | 300 kchip/s | -450 | DSSS BPSK |
| 11 | 04:48 | QPSK-DSSS | 300 kchip/s | +450 | DSSS QPSK |
| 12 | 05:10 | OQPSK-DSSS | 300 kchip/s | -450 | DSSS OQPSK |

### g40_payload_rx

| No | Keys at | Waveform | Rate | Offset (kHz) | Message |
|---|---|---|---|---|---|
| 1 | 01:08 | BPSK | 9600 Bd | -200 | UPLINK BPSK |
| 2 | 01:30 | QPSK | 9600 Bd | -100 | UPLINK QPSK |
| 3 | 01:52 | OQPSK | 9600 Bd | +500 | UPLINK OQPSK |
| 4 | 02:14 | 8PSK | 9600 Bd | +100 | UPLINK 8PSK |
| 5 | 02:36 | DSSS | 300 kchip/s | -450 | UPLINK DSSS BPSK |
| 6 | 02:58 | QPSK-DSSS | 300 kchip/s | +450 | UPLINK DSSS QPSK |
| 7 | 03:20 | OQPSK-DSSS | 300 kchip/s | -450 | UPLINK DSSS OQPSK |

## Change it

    python3 make_split_pair_plans.py --repeats 3              three passes, for a longer flight leg
    python3 make_split_pair_plans.py --tx-atten 20            more payload drive
    python3 make_split_pair_plans.py --ground-atten 10 --air-rx-atten 20
    python3 make_split_pair_plans.py --dsss ""                without the DSSS waveforms

## Status

Checked in the planner's plan check only. Not run on the radios.
