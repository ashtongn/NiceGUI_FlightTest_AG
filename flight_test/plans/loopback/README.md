# Loopback mission files for one G40

Four example mission files. Each makes one G40 send a signal out of one jack and decode it on another jack through a cable. They are for a bare G40 on the bench with no antenna and no amplifier. They are not flight cards and are not for the payload as wired.

Settings in these files: center 1325 MHz, 1.92 MS/s, TX attenuation 30 dB, radio row G40-LOOP. make_loopback_plans.py in the folder above writes them.

## What you need

- One G40 running the DPMG radio service, reachable from the ground station laptop.
- One SMA cable with a pad (attenuator) in line. The bench scripts use 20 dB at this TX attenuation; later bench runs used 50 dB.
- The ground station GUI open in a browser.

## The files

| File | Cable, TX jack to RX jack | Keyed steps | Messages to look for | Length (min:s) |
|---|---|---|---|---|
| `g40_loop_fsk.dpmg-mission.json` | TRx1B to TRx1A | 1 | LOOP FSK | 01:18 |
| `g40_loop_comm_set.dpmg-mission.json` | TRx1B to TRx1A | 9 | one after the other: LOOP FSK, LOOP 4FSK, LOOP BPSK, LOOP QPSK, LOOP OQPSK, LOOP 8PSK, LOOP 16QAM, LOOP QPSK CONV 1/2, LOOP FSK CONV 1/2 | 04:06 |
| `g40_loop_eight_fsk.dpmg-mission.json` | TRx1B to TRx1A | 1 | LOOP FSK 1 to LOOP FSK 8, all together | 01:31 |
| `g40_loop_port_walk.dpmg-mission.json` | moved six times, see Port walk | 6 | one per move, see Port walk | 09:53 |

Start with `g40_loop_fsk`. It is the shortest and sends one message.

## What to change for your G40

The files as written name no real radio. Before a file can run, its radio row has to be your G40. Nothing else has to change for a bare G40 with a cable and pad.

| What | In the files now | Change it when | How |
|---|---|---|---|
| Radio name | G40-LOOP (a placeholder) | always, unless this is already your G40's serial name | any one of the three ways below |
| Radio address | none | only if you want the plan to connect to the G40 by itself | write the files again with `--address IP:47810` |
| RF center | 1325 MHz | your bench uses another frequency | `--center`, in Hz |
| TX attenuation | 30 dB | your pad is smaller or larger than 20 to 50 dB | `--tx-atten`, in dB |
| Jacks | TRx1B to TRx1A | your cable is on other jacks, or a jack is locked to TX or RX on your G40 | `--tx` and `--rx` |

The serial name is the name the G40 announces, for example G40-AX35. It is on the radio's card in the SDR tab once the radio is connected.

Three ways to put your G40's name in, pick one:

1. Edit the .json. The name G40-LOOP is in the file once under "radios" as "id" and once in every step as "radio". Replace every G40-LOOP with your G40's serial name in a text editor, and save. All of them must change, or Check plan reports steps on a radio the plan does not have. From a terminal in this folder, for all four files:

```
sed -i 's/G40-LOOP/G40-AX35/g' *.dpmg-mission.json
```

2. Bind it in the mission view, step 5 below. This changes only the plan that is open, not the file. Save plan in the mission view downloads the changed plan if you want to keep it.

3. Write the files again with the name in them. Run this from the folder above this one. It writes all four files and this page again:

```
python3 make_loopback_plans.py --radio G40-AX35
python3 make_loopback_plans.py --radio G40-AX35 --address 192.168.0.22:47810
```

The other settings are in the .json too, but in many places: the center is in every step as "center_hz" and inside each step's "spec" as "center_freq_hz", the TX attenuation is "tx_atten_db" in every keyed step, and the jacks are "ports" in every step. Change those with the script or in the mission view's step editor, not by hand.

## How to run one

1. Fit the cable with the pad between the two jacks in the table and tighten both ends.
2. In the GUI open the engineering view, SDR tab, and connect the G40. It shows up on its own when the laptop hears it; otherwise use Connect by IP.
3. Open the mission view: http://localhost:8080/mission, or "mission view" in the header.
4. Press Open file and pick one of the files in this folder.
5. Under Mission radios the plan has one radio, named G40-LOOP. If that is not your G40's serial name it is shown as not online: pick your G40 in its "bind to a connected radio" box. The row takes your G40's name and its steps go with it. The box is there only while a G40 is connected.
6. Press Check plan. There should be no errors. One warning for each Decode step is expected: "Decode step has no Comm waveform step on air during it".
7. Press Run mission and confirm. Nothing is keyed in the first minute: the radio is being set to the two jacks.
8. Go back to the SDR tab and watch the Decodes table of the radio. Each message in the table above appears while its step is keyed, 13 s per step.
9. The plan stops keying by itself at the end. STOP ALL TX in the mission view stops it at any time. RX stays on afterwards: stop RX or close the radio on the SDR tab when done.

## If no message appears

- Look at the radio's spectrum while a step is keyed. The one message file puts its signal 400 kHz below the center. No signal there means the cable is on the wrong jacks or the step did not key: read the audit log in the mission view.
- A signal that fills the display or an RX overload warning means too much level: add pad, or write the files again with a higher --tx-atten.
- Check that the radio row is bound to the G40 that has the cable on it.

## How a loopback is laid out

The planner does not let two steps overlap on one radio, so each loopback is two steps in a row on the same pair of jacks. The Decode step builds the scenario without arming TX, loads its decode plan and starts RX. The Comm step after it keys the same scenario. RX is still running on that plan, so the radio decodes its own signal.

- The warning in step 6 comes from this layout: the Decode step ends when the Comm step starts.
- The first Decode step is 60 s long because a G40 takes up to 50 s to change jacks.
- Both steps name both jacks. If the Comm step named only its TX jack, the radio would be set up again when it keys and RX would stop.
- RX on a "2" jack with TX on the "1" jack of the same card is refused by the radio. TX on a "2" jack looped to its own card shows on-card coupling only. These files always go from one card to the other.

## Port walk

`g40_loop_port_walk` uses one cable and moves it six times. Each move sends a message that names its two jacks.

| Move | TX jack | RX jack | Message |
|---|---|---|---|
| 1 | TRx1B | TRx1A | LOOP 1B TO 1A |
| 2 | TRx2B | TRx1A | LOOP 2B TO 1A |
| 3 | TRx1A | TRx1B | LOOP 1A TO 1B |
| 4 | TRx2A | TRx1B | LOOP 2A TO 1B |
| 5 | TRx1B | TRx2A | LOOP 1B TO 2A |
| 6 | TRx1A | TRx2B | LOOP 1A TO 2B |

Fit the cable for move 1 before Run mission. After each keyed step ends nothing is keyed for 90 s: move the cable to the next row's jacks in that time. The radio changes jacks in the same gap. The mission view shows which step is running.

## Changing a setting

The files are written by `make_loopback_plans.py`, one folder up. Run it again to change them all together:

```
python3 make_loopback_plans.py --center 915e6 --tx-atten 40
python3 make_loopback_plans.py --rx TRx1B --tx TRx1A
```

--center is the RF center in Hz, --tx-atten the TX attenuation in dB, --rx and --tx the jacks of the three fixed cable files, --radio the name of the radio row and --address the radio's address. A single step can also be changed in the mission view's step editor after the file is opened.

## Test status

The files pass the planner's check. They have not been run on a G40 yet, so no decode has been seen from them. The first run on a radio is the test.
