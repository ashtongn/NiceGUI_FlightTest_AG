# Near flight mission files

Written by make_near_plans.py for a flight that goes no further than 5 statute miles
(8.05 km) from the ground radio site at 43.598621, -112.905566.
Center 1325 MHz, 1.92 MS/s, payload G40-AX3G keying on TRx2B at 10 dB.

| File | Plan name | Radios | TX attenuation (dB) | Location tasks | Check |
|---|---|---|---|---|---|
| `near_gates.dpmg-mission.json` | Near flight: range gates inside 5 mi | G40-AX3G | 10 | 5 | valid |
| `near_gates_g40_pair.dpmg-mission.json` | Near flight with the ground G40: range gates inside 5 mi | G40-AX3G, G40-AX35 | 10 | 5 | valid |
| `near_gates_full_power.dpmg-mission.json` | Near flight at full power: a jammer on each range gate inside 5 mi | G40-AX3G, G40-AX35 | 4.5 | 5 | valid |

`near_gates_full_power` keys a jammer on each gate in place of the range message, at half
scale and 4.5 dB TX attenuation. By the payload model the HPA puts out about 42 W on them, 0.8 dB into compression: 46.1 dBm into the T/R switch against its 47 dBm rating, about 29 W at the antenna and 24 W EIRP.
The comm messages are not expected to decode at this drive, which is why it keys jammers. The
20 dB pad in front of the HPA keeps its input under the damage limit at any TX attenuation.
The T/R switch, rated 47 dBm at its input, is what sets this file's drive. G40-AX35 receives so its spectrum and detections
log show each one. Its outer band keys for 30 s a visit at most. The
engineering view's Payload tab shows the expected EIRP of each step of the loaded file.

| Step | Jammer | From (mi) | To (mi) | Keys |
|---|---|---|---|---|
| near_r01 | spot tone | 1.00 | 1.25 | 15 s on entry |
| near_r02 | swept | 2.00 | 2.25 | 15 s on entry |
| near_r03 | LFM chirp | 3.00 | 3.25 | 15 s on entry |
| near_r04 | comb, 8 tones | 4.00 | 4.25 | 15 s on entry |
| near_outer | barrage noise | 4.50 | 5.00 | while inside, max 30 s |

## The gates

Each one is the part of the flight box at its range from the ground radio site, a band from one side of the box to the other. The times are for the route drawn
on the map: straight out on bearing 29.9 degrees to 4.75 mi and
straight back, at 30 m/s, 8 min 29 s in all.

| Step | Message | From (mi) | To (mi) | From (km) | To (km) | Keys | Confirm | Entered at (mm:ss) |
|---|---|---|---|---|---|---|---|---|
| near_r01 | RANGE 01.0 MI | 1.00 | 1.25 | 1.61 | 2.01 | 15 s on entry | auto | 00:54, 07:22 |
| near_r02 | RANGE 02.0 MI | 2.00 | 2.25 | 3.22 | 3.62 | 15 s on entry | auto | 01:47, 06:29 |
| near_r03 | RANGE 03.0 MI | 3.00 | 3.25 | 4.83 | 5.23 | 15 s on entry | auto | 02:41, 05:35 |
| near_r04 | RANGE 04.0 MI | 4.00 | 4.25 | 6.44 | 6.84 | 15 s on entry | auto | 03:34, 04:42 |
| near_outer | OUTER 05.0 MI | 4.50 | 5.00 | 7.24 | 8.05 | while inside, max 120 s | auto | 04:01 |

A gate keys once when the aircraft enters it and is ready again when the aircraft has
left it. An aircraft that circles inside a gate keys it once, not over and over.

## Run it

1. Radios connected. Aircraft feed listening and showing the aircraft's position.
2. Mission view, Open file. Check plan: no errors.
3. Run mission, then switch the location triggers on. Do this before the aircraft moves.
4. Watch the Live feed row and the audit log: each gate logs when it keys.
5. With the pair file, watch Decodes on G40-AX35: one message for each gate crossed.
6. STOP ALL TX to end.

On the bench, with no aircraft, play the route:

    python3 tools/sim_position.py --plan --speed 30

## Change it

    python3 make_near_plans.py --unit nm                       nautical miles
    python3 make_near_plans.py --rings full                    full rings, for a flight not held to the box
    python3 make_near_plans.py --limit 3 --gates 0.5,1,1.5,2   a shorter flight
    python3 make_near_plans.py --tx-atten 20                   more drive
    python3 make_near_plans.py --confirm-first once            a confirm on the nearest gate

## Status

Checked in the planner's plan check and against the route. Not run on the radios.
