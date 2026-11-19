# DPMG walkthrough

This is a guided tour of the main features on a G40 in Setup A. It takes
about half an hour and suits someone new to the system. Words in capitals
below are GUI labels.

## Setup

1. Cable Setup A: TX TRx1B to RX TRx1A through the 50 dB pad. No antenna on
   TX.
2. Open http://localhost:8080/ and check that the G40's card shows LINK OK.
3. The GPS antenna bias is off after every reboot of the radio. Set Timing /
   ref clock to gpsdo, tick "GPS antenna bias 3.3 V" and confirm the prompt.
4. Set the starting state: 30.72 MSps, 425.025 MHz, RX TRx1A, TX TRx1B, TX
   atten 20 dB, clock gpsdo. Press START. The status line should show
   STREAMING and ref LOCKED.
5. Check that the radio card shows the chip temperature, the card
   temperatures and a tilt line reading "still". If the radio was moved since
   it started, press set level so the tilt reads near 0.
6. Open a second tab on http://localhost:8080/engineering (SDR tab) for steps
   5 to 8.

## 1. Operator console

The radio card shows the link, GPS, the chip temperature, the two NV100 card
temperatures and the accelerometer line. The status line shows streaming,
frame rate, overflow counts and GPSDO lock. RX is on one card and TX on the
other.

## 2. Tone and safety

In Jammer, pick Spot / CW, then BUILD + LOAD, then TRANSMIT and confirm. A
single carrier shows on the spectrum and in the detections table. Stop it.
Every transmit asks for confirmation, and the STOP ALL TX button in the top
bar stops everything.

## 3. Modulation and coding card

Set the sample rate to 1.92 MSps and press START straight away. A rate that
is not applied goes back to the radio's value after a few seconds. Then set
TX atten to 5 dB. RX gain stays at +34 dB.

In IQ playback, set Source to "Scenario JSON: on the radio", click BROWSE
RADIO and pick scenarios/json/mod_coding_card.json. Click PLAY and confirm.

13 carriers appear between -650 and +650 kHz. Within a minute the Decodes
table fills with C01 OK through C13 OK. Stop the card when done.

## 3b. Symbol jammer

Stay at 1.92 MSps. In Jammer, pick Modulated (symbol jammer), QPSK, symbol
rate 100000, symbol duty 1.00, offset 300000, TX power 0.5. Press BUILD +
LOAD, then TRANSMIT, and confirm. A smooth signal about 115 kHz wide appears
300 kHz above centre. Set the symbol duty to 0.25 and build again: the same
signal appears, now pulsed. Stop it.

Then pick Partial-band noise, bandwidth 115000, offset 300000. On the
spectrum it looks much the same. Stop it.

## 4. DSSS, four users on one frequency

Keep the same settings. Play scenarios/json/dsss_mixed_carriers.json the same
way. One signal shows on the spectrum and four users decode.

dsss_4user_bpsk.json (four BPSK users) and dsss_16user.json (sixteen) play
the same way.

## 5. DSSS builder

In the engineering view, SDR tab, open "DSSS builder: LFSR codes & multi-user
overlay". Pick the preset "4-user BPSK overlay" and click Build codes. Look
at the correlation table, then click Preview spectrum.

To see the input checks, set codes (N users) to 16 and degree n to 3. The
Input checks box reports an error (no Gold preferred pair for n=3; use n = 5,
6, 7, 9, 10 or 11), and the three build buttons are disabled. Set degree 5
and 4 users again.

## 6. Views in sync

Change TX atten in the engineering view. Within a few seconds the operator
console shows the same value. Change it back. The radio card at the top shows
the same temperatures and tilt as the operator card, with GPSDO lock in the
status line. RX centre, RX gain and TX atten take effect as soon as they
change, as on the operator console.

## 7. Secure messaging

The "Secure messaging (AES-256-GCM-SIV, 128-bit tag)" panel is in the
engineering view.
Secure messages need the radio service started with `DPMG_SECURE_MSG` set.
Without it, the service log shows `secure-msg: off` at start.

## 8. Record & replay

In the engineering view, SDR tab, open Record & replay below the DSSS
builder. With the test card from step 3 playing, record 1 s of the full
band, then stop the card.

Replay with the gain at its cap, then again 6 dB lower. The decodes come back
from the recording, and the level on the spectrum follows the replay gain.

Do not replay at 0 dB on this cable. The recording already went through the
50 dB pad once, so a 0 dB replay arrives about 38 dB too low and nothing
decodes.

To record one signal only, drag a box on the Live PSD with the select band
tool (top right of the plot) and set the source to the selected band.

## When you finish

Press STOP ALL TX, then STOP RX. RX left streaming keeps the NV100 cards hot.

## If something goes wrong

| Problem | Fix |
|---|---|
| Radio card shows LINK LOST | Engineering view, Connect by IP, 192.168.0.22 |
| No GPSDO lock | Tick GPS antenna bias 3.3 V and wait about a minute |
| Decodes table stays empty | Check the rate is 1.92 MSps, then play again |
| Spectrum frozen or RX STALE | STOP RX, then START |
| Service not answering | Run `./deploy.sh --restart` on the ground station, then set the starting state |
