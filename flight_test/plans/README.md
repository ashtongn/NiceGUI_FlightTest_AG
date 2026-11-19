# Flight test mission files

Written by make_flight_plans.py: center 1325 MHz, payload G40-AX3G at 192.168.0.23:47810 TX TRx2B, ground G40 G40-AX35, ground X40 X40-B00S, TX attenuation 30 dB, ground 30 dB, flight drive 30 dB, time plan on 2026-11-17 from 17:00 UTC. Rates stop at 30.72 MS/s.

Load a plan in the mission view with Open file; nothing is run on the day. `plans/` are Payload 2 alone. `pairs/` is one plan per pairing with both radios on one timeline from Run mission (downlink, uplink, then the location tasks) and the open air go or no-go. `bench/` are the same two radio plans plus a tone each way check. `cabled/` are the pair plans for the cabled ground layer (GROUND-02): uplink at ground TX attenuation 0 dB and the payload RX attenuator at 0 dB, since the coupled port and the 50 dB pad take about 80 dB off the uplink. Each radio row is named by the radio's serial name and binds to that radio when it is online; any row that stays offline is bound with "bind to a connected radio" in Mission radios. The location areas are points with a radius, placed along the default route inside the flight box and retyped in the step editor once the route is known. The time plan keys at UTC clock times: set its date and move its steps in the mission view.

| File | Plan name | Steps | Location tasks | Radios | Check | Least margin to a band edge (MHz) |
|---|---|---|---|---|---|---|
| `file_playback.dpmg-mission.json` | Flight: example files through the planner build | 7 | 0 | G40-AX3G | valid | 22.50 |
| `jam_1m92.dpmg-mission.json` | Flight: seven jammers at 1.92 MS/s | 7 | 0 | G40-AX3G | valid | 24.04 |
| `jam_7m68.dpmg-mission.json` | Flight: seven jammers at 7.68 MS/s | 7 | 0 | G40-AX3G | valid | 21.16 |
| `jam_30m72.dpmg-mission.json` | Flight: 25 jammers at 30.72 MS/s, then 25 at once | 26 | 0 | G40-AX3G | valid | 9.64 |
| `jam_power_ladder.dpmg-mission.json` | Flight: power ladder through the 1189 | 10 | 0 | G40-AX3G | valid | 9.64 |
| `trig_time_utc.dpmg-mission.json` | Flight: steps at UTC clock times | 5 | 0 | G40-AX3G | valid | 24.70 |
| `trig_location.dpmg-mission.json` | Flight: range gates and the far end of the flight box | 9 | 7 | G40-AX3G | valid | 24.70 |
| `ground_cabled_fieldfox.dpmg-mission.json` | Flight: one step per waveform for the FieldFox | 9 | 0 | G40-AX3G | valid | 9.64 |
| `pairs/g40_pair.dpmg-mission.json` | Pair G40 ground: G40-AX3G and G40-AX35, one timeline | 46 | 7 | G40-AX3G, G40-AX35 (by name) | valid | 24.25 |
| `pairs/g40_air_go_nogo.dpmg-mission.json` | Flight: open air go or no-go with the G40 ground radio | 7 | 0 | G40-AX3G, G40-AX35 (by name) | valid | 24.04 |
| `bench/g40_pair.dpmg-mission.json` | Pair G40 ground: G40-AX3G and G40-AX35, one timeline | 46 | 7 | G40-AX3G, G40-AX35 | valid | 24.25 |
| `bench/g40_tone_each_way.dpmg-mission.json` | Bench G40 and G40: tone each way, no HPA | 2 | 0 | G40-AX3G, G40-AX35 | valid | 24.00 |
| `cabled/g40_pair.dpmg-mission.json` | Cabled pair G40 ground: G40-AX3G and G40-AX35, one timeline | 46 | 7 | G40-AX3G, G40-AX35 (by name) | valid | 24.25 |
| `pairs/x40_pair.dpmg-mission.json` | Pair X40 ground: G40-AX3G and X40-B00S, one timeline | 38 | 7 | G40-AX3G, X40-B00S (by name) | valid | 24.25 |
| `pairs/x40_air_go_nogo.dpmg-mission.json` | Flight: open air go or no-go with the X40 ground radio | 7 | 0 | G40-AX3G, X40-B00S (by name) | valid | 24.04 |
| `bench/x40_pair.dpmg-mission.json` | Pair X40 ground: G40-AX3G and X40-B00S, one timeline | 38 | 7 | G40-AX3G, X40-B00S | valid | 24.25 |
| `bench/x40_tone_each_way.dpmg-mission.json` | Bench X40 and G40: tone each way, no HPA | 2 | 0 | G40-AX3G, X40-B00S | valid | 24.00 |
| `cabled/x40_pair.dpmg-mission.json` | Cabled pair X40 ground: G40-AX3G and X40-B00S, one timeline | 38 | 7 | G40-AX3G, X40-B00S (by name) | valid | 24.25 |

## Band check, 1300 to 1350 MHz

Every radiating step's occupied band at its radio's rate, from the step's parameters: a barrage is taken as the whole sample band, a noise patch, comb, sweep, chirp or modulated jammer by its own width, a comm signal by its offset and bandwidth (FSK by its tone excursion plus a symbol rate each side, PSK and QAM by 1.35 times the symbol rate), a playback file by its own rate. The radio's analog filter and the HPA are not modeled. A ground X40 runs at 50 MS/s, so its sample band is 1300 to 1350 MHz at a 1325 MHz center: its TX steps carry only the comm signals listed, but its noise floor and any image fill that band edge to edge.

No step goes outside the band.

Steps within 10 MHz of an edge:

| Plan | Step | Low (MHz) | High (MHz) | Margin (MHz) | What sets it |
|---|---|---|---|---|---|
| jam_30m72 | j3072_01_barrage | 1309.640 | 1340.360 | 9.64 | barrage, the whole sample band |
| jam_30m72 | j3072_24_barrage | 1309.640 | 1340.360 | 9.64 | barrage, the whole sample band |
| jam_power_ladder | lad_40_barrage | 1309.640 | 1340.360 | 9.64 | barrage, the whole sample band |
| jam_power_ladder | lad_30_barrage | 1309.640 | 1340.360 | 9.64 | barrage, the whole sample band |
| jam_power_ladder | lad_25_barrage | 1309.640 | 1340.360 | 9.64 | barrage, the whole sample band |
| jam_power_ladder | lad_20_barrage | 1309.640 | 1340.360 | 9.64 | barrage, the whole sample band |
| jam_power_ladder | lad_15_barrage | 1309.640 | 1340.360 | 9.64 | barrage, the whole sample band |
| ground_cabled_fieldfox | ff_01_barrage | 1309.640 | 1340.360 | 9.64 | barrage, the whole sample band |

## Location tasks

Ground radio site 43.598621, -112.905566. Flight box corners: 43.916667, -112.750000; 43.916667, -112.650000; 43.550000, -112.833333; 43.583333, -112.933333. Route on the map (the bench sender plays it): 43.566667, -112.883333; 43.916667, -112.700000. The same tasks are in trig_location and every pair plan. Each is the part of the flight box between two ranges from the ground radio site, so it works for any track through the box. A gate keys for a set time once entered and re-arms when the aircraft has left it, so it keys going out and again coming back; the far end keys while the aircraft is in it.

| Step | Message | From (km) | To (km) | Keys | Confirm | Area (km2) |
|---|---|---|---|---|---|---|
| loc_r05 | RANGE 05 KM | 5 | 6.0 | 15 s on entry | once | 13.5 |
| loc_r10 | RANGE 10 KM | 10 | 11.0 | 15 s on entry | auto | 9.8 |
| loc_r15 | RANGE 15 KM | 15 | 16.0 | 15 s on entry | auto | 9.0 |
| loc_r20 | RANGE 20 KM | 20 | 21.0 | 15 s on entry | auto | 8.6 |
| loc_r25 | RANGE 25 KM | 25 | 26.0 | 15 s on entry | auto | 8.4 |
| loc_r30 | RANGE 30 KM | 30 | 31.0 | 15 s on entry | auto | 8.1 |
| loc_far | FAR END BOX | 34 | 40.9 | while inside, max 300 s | auto | 69.3 |

The farthest corner of the flight box is 40.9 km from the site.
