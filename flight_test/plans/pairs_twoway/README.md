# G40 pair plan, both ways in turn

Written by make_twoway_pair_plan.py. `g40_twoway.dpmg-mission.json`: comm waveforms sent down from the
payload and up from the ground, one after the other, with the ground G40 on two jacks. Center
1325 MHz, 1.92 MS/s on both radios. One pass. The last step ends at 05:54.

## Cabling

| Radio | Jack | Goes to |
|---|---|---|
| G40-AX35 | TRx1A | the ground RX antenna |
| G40-AX35 | TRx1B | the ground HPA, then the ground TX antenna |
| G40-AX3G | TRx2B | 20 dB pad, 1189 HPA, T/R switch, payload antenna |
| G40-AX3G | TRx1A | payload RX chain, from the same antenna through the T/R switch |

The payload has one antenna, so it cannot send and receive at the same moment. The two
directions take turns, 21 s apart.

## Settings to check before keying

| Setting | Value in this file | What sets it |
|---|---|---|
| Payload TX attenuation, downlink | 10 dB | the T/R switch rating and the range |
| Ground G40 TX attenuation, uplink | 30 dB | the ground HPA: its input limit and its gain. Not known to this script |
| Payload RX attenuator, uplink | 0 dB | how strong the uplink is at the payload |

The ground G40 gives about +20 dBm at full scale and 0 dB TX attenuation, and these
waveforms average about 7 dB under that. At 30 dB it puts about -17 dBm into the
ground HPA. Work out what that HPA then puts into its antenna before the first key.
With the two radios close together on the ground the uplink is strong: raise the payload
RX attenuator with `--air-rx-atten 30` for a ground check and take it back down to fly.
The ground G40 does not switch its HPA on or off; that is done at the HPA.

## Run it

1. Both radios connected. Ground RX and TX antennas apart from each other.
2. Mission view, Open file. Check plan: no errors.
3. Run mission, read the confirm, key it. Nothing keys in the first minute.
4. Watch Decodes on G40-AX35 for the downlink messages and on G40-AX3G for the uplink
   messages, in the order below.
5. STOP ALL TX to end early.

| Turn | Down keys at | Down message (G40-AX35 decodes) | Up keys at | Up message (G40-AX3G decodes) |
|---|---|---|---|---|
| 1 | 01:08 | COMM BPSK | 01:29 | UPLINK BPSK |
| 2 | 01:50 | COMM QPSK | 02:11 | UPLINK QPSK |
| 3 | 02:32 | COMM OQPSK | 02:53 | UPLINK OQPSK |
| 4 | 03:14 | COMM 8PSK | 03:35 | UPLINK 8PSK |
| 5 | 03:56 | DSSS BPSK | 04:17 | UPLINK DSSS BPSK |
| 6 | 04:38 | DSSS QPSK | 04:59 | UPLINK DSSS QPSK |
| 7 | 05:20 | DSSS OQPSK | 05:41 | UPLINK DSSS OQPSK |

## Change it

    python3 make_twoway_pair_plan.py --ground-atten 20            more drive into the ground HPA
    python3 make_twoway_pair_plan.py --air-rx-atten 30            radios close together
    python3 make_twoway_pair_plan.py --ground-rx TRx2A --ground-tx TRx1B
    python3 make_twoway_pair_plan.py --repeats 3                  three passes, for a longer leg

## Status

Checked in the planner's plan check only. Not run on the radios.
