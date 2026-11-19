# Ground G40 into the payload RX chain

Written by make_rx_chain_plan.py. `ax35_to_payload_rx.dpmg-mission.json`: G40-AX35 keys 9 comm
waveforms in turn and G40-AX3G decodes each one through the payload RX chain. Center
1325 MHz, 1.92 MS/s. Times are from Run mission.

## Cable

G40-AX35 jack TRx1B to the payload antenna port (T/R switch J0). Nothing else on J0.

    G40-AX35 TRx1B, cable, switch J0, switch J1, RX attenuator, LNA, splitter, G40-AX3G TRx1A

The payload does not key in this plan, so the switch stays in RX and the HPA stays off.
The payload must not key while G40-AX35 is on the antenna port: the HPA output would go
into G40-AX35. Take the cable off J0 before loading any plan that keys the payload.

## Levels

| Where | Estimate (dBm) | Limit (dBm) |
|---|---|---|
| J0, from G40-AX35 at 30 dB TX attenuation | -20 | |
| LNA input, RX attenuator at 60 dB | -87 | 10 |
| G40-AX3G TRx1A | -64 | 20 |

Keep G40-AX35 at 10 dB TX attenuation or more on this cable; the script refuses less.
Too weak or too strong at G40-AX3G: change the RX attenuator, 10 dB at a time, with
`python3 make_rx_chain_plan.py --rx-atten 50` and load the file again. If G40-AX35 comes in
through a pad or a coupler, take that many dB off the RX attenuator setting.

## Run it

1. Both radios connected in the ground station. G40-AX3G shows the RX attenuator as connected.
2. Mission view, Open file, `ax35_to_payload_rx.dpmg-mission.json`. Check plan: no errors.
3. Run mission. In the first minute each radio is set to its jack; nothing keys.
4. Watch Decodes on G40-AX3G: one message per waveform, in the order below.

| No | Keys at | Waveform | Offset (kHz) | Message |
|---|---|---|---|---|
| 1 | 00:01:08 | FSK | -400 | RX CHAIN FSK |
| 2 | 00:01:30 | 4FSK | -300 | RX CHAIN 4FSK |
| 3 | 00:01:52 | BPSK | -200 | RX CHAIN BPSK |
| 4 | 00:02:14 | QPSK | -100 | RX CHAIN QPSK |
| 5 | 00:02:36 | OQPSK | +500 | RX CHAIN OQPSK |
| 6 | 00:02:58 | 8PSK | +100 | RX CHAIN 8PSK |
| 7 | 00:03:20 | 16QAM | +200 | RX CHAIN 16QAM |
| 8 | 00:03:42 | QPSK | +300 | RX CHAIN QPSK CONV 1/2 |
| 9 | 00:04:04 | FSK | +400 | RX CHAIN FSK CONV 1/2 |

Each keys for 13 s. The last one ends at 00:04:18.

## Other set-ups

    python3 make_rx_chain_plan.py --utc 20:30                 start at a UTC clock time today
    python3 make_rx_chain_plan.py --utc 20:30 --date 2026-11-18
    python3 make_rx_chain_plan.py --ground G40-AX3B --ground-address 192.168.0.24:47810

## Status

Checked in the planner's plan check only. Not run on the radios yet.
