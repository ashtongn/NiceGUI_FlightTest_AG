# G40 GPIO front end: PA enable, TX/RX switch, PA fault, USB attenuator

The G40 can drive an external HPA enable and a TX/RX switch, and read an HPA alarm,
through its 27-pin GPIO socket. It can also set a Mini-Circuits RCDAT-6G-120H attenuator
over USB. All of it is opt in. With no `rf_frontend.yaml`, or with `frontend: none`, the
service touches no GPIO and the radio behaves as it does without a front end.

Files: `dpmg/payload/rf_frontend.py`, `dpmg/payload/frontend_setup.py`,
`dpmg/payload/mc_atten.py`, `rf_frontend.example.yaml`, `deploy.sh`,
`bench/rf_frontend_bench.py`, `deploy/g40/60-minicircuits.rules`, and `deploy/wheels/`
with `deploy/g40/Dockerfile.frontend-overlay` (gpiod, hidapi, smbus2). The tests are
`tests/test_rf_frontend.py`, `test_mc_atten.py`, `test_deploy_frontend_option.py` and
`test_hpa_monitor.py`. The port map (see "Port map" below) is `dpmg/radio/port_map.py`,
`dpmg/web/port_pick_ui.py` and `tests/test_port_map.py`.

## Socket pins and Linux lines

The pin table comes from the Epiq G-Series Hardware User Manual v1.5, Table 12. All
signals are 3.3 V after the carrier's level translators. The mating connector is an ODU
GK1WAM-P27WBC0-000L.

| socket pin | signal | Linux line (chip, pad, logical name, R36 offset) | dir | role |
|---|---|---|---|---|
| 17 | CPU_GP_OUT_1 | gpiochip0 `PG.06` `cpu-gp-out1` 41 | out | HPA enable (default) or switch |
| 15 | CPU_GP_OUT_2 | gpiochip0 `PN.01` `cpu-gp-out2` 85 | out | switch (default) or HPA enable |
| 16 | CPU_GPIO | gpiochip0 `PP.06` `cpu-gpio` 98, plus direction line gpiochip1 `PCC.03` `cpu-gpio-direction` 15 held high (output) | in/out | HPA enable or switch |
| 18 | CPU_GP_IN | gpiochip1 `PCC.01` `cpu-gp-in` 13 | in | HPA alarm or fault input |
| 9 / 7, 8 / 10 | NVA_TR_N_1/2, NVB_TR_N_1/2 | none (NV100 card A / card B FPGA) | out | switch, proxy mode only |

Offsets 41, 85 and 13 were read with `gpioinfo` on the G40 (BSP R36.5). Offsets 98 and 15
follow the tegra234 port layout but were not measured. The R36 device tree has no logical
line names, so lines resolve through the pad name fallback.

`./deploy.sh --frontend` refuses: pin 18 as an output; pins 7 to 10 as the HPA enable;
power pins 1, 2 and 26 (+3.3 V, 33 mA each, not a PA supply); ground pins 3, 4, 13, 14,
19 and 20; SPI0 and I2C-1 pins 21 to 25 and 27; NV100 GPIO pins 5, 6, 11 and 12 (no
software path); and one pin in two roles.

Boot and kill state. The flashed MB1 BCT drives PG.06 and PN.01 low (pull-down) until the
service claims them. PCC.01 has no pad pull, but an open pin 18 reads 1 because the
carrier pulls it up. So with `active_low: false` a disconnected alarm wire reads as a
fault and blocks keying. A hard kill of the service (SIGKILL, OOM, `docker kill`) leaves
the PA pad driven at its last level, since the pad stays a GPIO output after release.
Only the interface hardware can make a killed or unpowered G40 safe.

## Hardware mapping

| part | control input | polarity | alarm | config |
|---|---|---|---|---|
| Empower 1189 / BBM3K5KKO (500-2500 MHz, 100 W; Payload 2) | DB-9 pin 5 Shutdown, 5 V TTL | high = off, low = on; open = on | none | `pa_enable.active_high: false`, no fault input |
| Empower 1208 (Payload 1), 1211 (Payload 3) | not on file | entered from the datasheet | entered | entered at setup |
| Empower 1193 / BBM2E4AKO (20-1000 MHz, 100 W), 1212 (2-6 GHz, 50 W) | D-sub pin 6 PA Enable, 0/3.3 V | high = on; open = on (pulled high) | pin 7, high = fault | `active_high: true`; fault on pin 18, `active_low: false` |
| Pasternack PE71S2041 switch (payload, 15 V supply) | control pin | TX level not on file, entered | | `tr_switch.mode: gpio`, `tx_level` |
| dBwave PASR0200500600A or B SP2T (+5 V bias) | TTL pin, high threshold 2.8 V | TTL 1 = J0-J1, TTL 0 = J0-J2 | | `tx_level` = level that selects the HPA port (0 on Payload 2, where the HPA is on J2) |

The payload numbers follow `payload_profiles.yaml`: P1 is the 1208, P2 the 1189 and P3
the 1211.

Wiring rules:

- Every Empower HPA is on when its control input is open, so never wire socket pins
  straight to an HPA. For an active high enable (1193, 1212), put a buffer with an input
  pull-down (10 to 47 kOhm) at the HPA end. For the 1189 the interface must pull the
  shutdown input high (off); a pull-down would turn it on. The 1189 input is 5 V TTL, so
  it needs a level shifter or driver.
- Strap the switch control so an open control selects RX.
- A pull-up level shifter (BOB-12009 / BSS138, 3.3 to 5 V, as in the payload block
  diagram) reads high whenever the G40 is off, booting or not driving the pad. Behind it
  the HPA must be off at high (the 1189 is), and `tx_level: 1` selects TX while the pad
  is undriven. The setup tool prints a DANGER line for both cases. The carrier's own
  translator and the shifter are two translators in series, so check levels and edges on
  a scope.
- The dBwave switch is rated 47 dBm (50 W) CW, below the 100 W HPAs, and is cold
  switching only. The sequencer keeps the PA off while the switch moves.

## Setup

Run `./deploy.sh --frontend` on the x86. It shows the socket table (also printed by
`python3 -m dpmg.payload.frontend_setup pins`) and asks, by socket pin, for the HPA
enable, the switch and the alarm. It takes the polarity from an HPA or switch preset or
from the datasheet, and asks about a pull-up level shifter and `watchdog_ms` (default
500). The chip, line, pad fallback and the pin 16 direction line follow from the pin. A
polarity that is not on file has no default: an empty answer is asked again, and
`--batch` without it is refused.

The tool validates the file on the x86 with `dpmg/payload/rf_frontend.py` against the G40
R36 line table, shows it and asks to confirm. It refuses if the service does not answer
`frontend_status` or the HPA is keyed. Otherwise it backs up the radio's file as
`rf_frontend.yaml.bak_<time>`, installs the new one at `$CODE_DIR/rf_frontend.yaml`
(`/app_svc/rf_frontend.yaml` in `dpmg_svc`) and applies it with `frontend_reload`. The
report checks chip:offset per role against the file, that the HPA reads off, and that the
switch reads its RX level. A failed reload restores the backup. There is no push,
rebuild, restart or RF. A pin 16 config is refused unless the radio's
`dpmg/payload/rf_frontend.py` matches this tree.

To run without questions, give the answers as flags (`./deploy.sh --help` lists them)
with `--batch --yes`, or install a ready-made file with `./deploy.sh --frontend-config
FILE`:

```bash
./deploy.sh --frontend --hpa empower-1189 --switch pe71s2041 --pa-pin 17 --switch-pin 15 --tr-tx-level 1 --fault-pin none --batch
```

`--tr-tx-level 1` there is the example file's default. Payload 2 as built has the dBwave
switch with the HPA on its J2 port, which TTL 0 selects, so it runs
`--switch dbwave-pasr --tr-tx-level 0`.

`rf_frontend.example.yaml` documents the keys: `frontend` (none, gpio or mock),
`pa_enable`, `tr_switch` (`mode: proxy`, the example default, or `gpio`), `pa_fault_in`,
`watchdog_ms`, `atten`, and two optional sections, commented out. `tr_readback` reads the
switch control line through an I2C ADC (an ADS1015 or ADS1115) and calls a half rail level
a fault. It needs the `smbus2` module, which the image gets from the wheel in
`deploy/wheels/` (the overlay build above, or a fresh image build). `payload` runs
the RX protection check on every key (`dpmg/payload/payload_guard.py`).

The setup tool does not ask about `payload`, `tr_readback` or `hpa_monitor`. Add them by
hand; a later `./deploy.sh --frontend` run keeps them (an enabled ADC section is carried
over as it is). With `payload` set and `atten.transport: none`, every key is
refused, and a TX run keys the PA line too, so the radio cannot transmit at all. Add the
section only once the RX attenuator is plugged in and its transport set. Leave it out for
bench loopback work with no HPA.

Unknown keys are rejected. A line may be a logical name, a pad name or an offset, and
`fallback_line` is tried second. The file path comes from `--frontend-config PATH` or
`DPMG_FRONTEND_CONFIG`. The service flags `--frontend`, `--pa-line`, `--tr-line`,
`--tr-mode`, `--pa-fault-line` and `--atten` only override the file.

## How the software sequences it

- TX on happens at the first send of a run, before the RF port is pointed at TX and
  streaming starts. The service sets the switch to TX, waits `tr_switch.settle_ms` (1),
  turns the PA on and waits `pa_enable.settle_ms` (2).
- TX off happens at `stop_tx`, before streaming stops and the ports return to RX.
  SINGLE_SHOT waits `hang_ms` (20), turns the PA off, waits `settle_ms` and sets the
  switch to RX. CONTINUOUS holds the PA keyed between sends and skips the hang.
- Interlocks (`gpio` mode): the PA is never keyed with the switch in RX, and the switch
  never returns to RX with the PA keyed. In `proxy` mode the software only tracks the
  switch, so the interlock is not enforced.
- Watchdog: each send feeds it with the send's duration plus `watchdog_ms`, before and
  after the blocking transmit. A stalled TX loop drops the PA (50 ms poll). A manual PA
  ON from the GUI is an operator hold and is not timed by the watchdog.
- Fault input: when it is asserted, or cannot be read, keying is refused and a keyed PA
  is dropped, a manual hold included.
- Fail safe: the PA line is driven to its inactive level (high for the 1189, whose
  shutdown input is active low) at open, close, reload, `atexit`, SAFE and any error
  inside a sequence. A front end refusal stops the TX loop instead of retrying.
- Heartbeat mode (`pa_enable.mode: heartbeat`, for a permit board between the G40 and
  the HPA): while the PA is keyed the service toggles the PA line at
  `pa_enable.heartbeat_hz` (default 200). The board turns the toggling into an enable and
  drops it a set time after the edges stop. A steady line at either level means off, so
  the bootloader level at power-on, a service that never starts and a crash or hang while
  keyed all leave the HPA off. Every way out of keying (TX off, SAFE, watchdog, fault,
  close, reload, a failed line write) stops the edges and leaves the line steady at the
  inactive level. `frontend_status` shows `mode`, `heartbeat_hz`, `hb_running`, `hb_edges`
  and `hb_max_gap_ms`, the longest gap seen between edges, which sets how long the board
  may wait before dropping the enable. Leave the mode at `level` (the default) unless the
  permit board is fitted: wired straight to the 1189, a toggling line would key and unkey
  the HPA at the heartbeat rate.

## HPA safety at power-up

The Empower 1189 is on when its Shutdown line (DB9 pin 5) is low or open. PG.06 is
the HPA enable, active low for the 1189. PN.01 is the switch line, and which level
means TX is `tr_switch.tx_level`: 1 in the example file, 0 on Payload 2 as built, where
the HPA is on the switch's J2 port and TTL 0 selects it. The table gives the switch
position for both:

| Event | PG.06 | 1189 | PN.01 | Switch, tx_level 1 | Switch, tx_level 0 (Payload 2) |
|---|---|---|---|---|---|
| G40 power-on until `dpmg_svc` claims the line (tens of seconds) | LOW (bootloader BCT) | ON | LOW | RX | TX (HPA port) |
| Service running, idle | HIGH | off | the RX level | RX | RX |
| Service stopped cleanly | stays HIGH | off | stays at the RX level | RX | RX |
| Service killed while keyed | stays LOW | ON | stays at the TX level | TX | TX |
| Control cable unplugged at the G40 side | pulled HIGH by the shifter | off | pulled HIGH | TX | RX |

With `tx_level: 0` the boot window puts the switch on the HPA port while the 1189 is
on; with `tx_level: 1` an unplugged cable does, with the HPA off. Either way the HPA is
on during the boot window, which is what the power order below covers.

On the bench this is handled by the power order: the HPA DC supply is off while
the G40 boots, on only after `frontend_status` shows the PA off and the lines
held, and off again before any reboot, service restart or deploy. Never kill
`dpmg_svc` while keyed.

When the G40 and the HPA get power together (the payload), the power order cannot
be followed, and nothing in software covers the boot window, a service that never
starts, or a crash while keyed. The protection planned for that case is hardware:

- a permit board between PG.06 and the 1189 Shutdown pin, powered from the HPA
  rail, that enables the HPA only while PG.06 toggles (a steady level at either
  value, as at boot, after a crash or with the cable off, means off);
- the HPA DC supply behind a normally open relay closed by the same permit;
- the switch control resting on RX when there is no permit.

The software side exists: `pa_enable.mode: heartbeat` (see "How the software
sequences it"). Leave the mode at `level` until the permit board is fitted.

## Port map: what each SMA jack may do

The `ports:` section of a radio's `rf_frontend.yaml` says how each jack is connected and
which functions it may be used for. It is per radio, like the rest of the file. With no
section nothing is restricted.

```yaml
ports:
  TRx2B: {role: tx, chain: "20 dB pad, Empower 1189 HPA, T/R switch J2 and J0, antenna"}
  TRx1A: {role: rx, chain: "ZB3PD-63-S+ splitter, dBWave PALNA200101000A LNA, RCDAT-6G-120H RX attenuator, T/R switch J1 and J0, antenna"}
```

`role` is `tx`, `rx`, `txrx` or `none`, and a jack the section does not list counts as
`none`. `chain` is plain text for the operator, at most 140 characters. At least one jack
must allow RX, because the service always opens an RX channel. A map with no TX jack makes a
receive only radio. A bad section stops the file loading, like any other bad key.

The service enforces it in one place (`_port_refusal`, called from `_apply` next to the
radio role check), so it holds for the GUIs, the planner and bench scripts alike:

- `init` is refused if its RX jack or the TX jack it names is not allowed. An init that
  names no TX antenna (RX only) passes.
- Anything that keys or listens later is checked against the jacks the radio is on:
  `set_tx_waveform`, `start_tx`, `replay_recording`, `bitpipe_start`, armed `transceive`,
  armed `transponder` and armed `ground_checkout` for TX; `start_rx`, `record_iq`,
  `capture_iq`, `survey_classify`, `channelizer_test` and `selftest_rx_wedge` for RX.
  After an RX only init TX
  would use the card's default jack, TXA1, so that is the jack checked.
- Stops, status and tuning are never refused. `payload_key` and `frontend_set` move the HPA
  and the switch rather than choosing a jack, so the map does not cover them.
- `frontend_reload` re-reads the map under an open radio and its ack carries
  `ports_conflict` if the open jacks are now locked. If the file does not load, the map is
  unknown, so init and every command above are refused until it does.

`get_config` reports `ports` (jack to role) and `ports_v` (changes with the map) in the
snapshot polled every second. `get_config {"ports": true}` returns `ports_full` with the
chain text, which is too big for the snapshot's one datagram. An older service reports
neither and a GUI then restricts no jack.

The operator console and the engineering SDR tab grey out and label locked jacks
("TRx1A (RX only)"), step off a locked default, and show the chain as a tooltip on each row
and on the closed dropdown. The engineering tab also greys out modes no card can run under
the map. The mission step editor does the same, keeps the stored choice and leaves it to
Check plan, which reports a locked jack as an error for a radio that is connected. RX none
never moves RX onto a locked jack.

### Changing it from the GUI

The engineering view has a "Ports lockout" panel above the SDR controls. It follows the
radio selected with the radio cards, whatever its kind, and its title names that radio's
serial and kind. The jack rows are that radio's own: TRx1A to TRx2B on a G40, RXA1, RXA2,
RXB1, RXB2 and TXA1 on an X40 (a receive jack there is rx or none, the transmitter tx or
none), the capability list's antennas on any other kind; `caps` carries the list as
`port_jacks`. A kind with no jack list shows "this radio reports no port map support". The
rows show each jack's role and chain as that radio reports them. Apply asks for
confirmation, says what changes and warns when a change opens a jack up, then sends
`set_ports` to that radio only. Remove lockout sends the same command with `clear`. An edit
not yet applied is kept for its radio when another radio is selected, and the panel says
which radios hold one; Revert drops it.

- `set_ports {ports: {jack: {role, chain}}, by}` or `set_ports {clear: true, by}` checks the
  map the same way the loader does, and refuses names that are not jacks of that radio's
  kind (a G40 refuses RXB1, an X40 refuses TRx1A). It is refused while TX runs. Sending the
  map the radio already has does nothing.
- It writes the `ports:` section of the radio's own `rf_frontend.yaml`, the same file
  `frontend_reload` reads, so nothing else holds a copy. Only that section changes: the new
  text must load, and everything else in it must read the same, or the file is not touched.
  Comments elsewhere stay, but a comment inside the section is lost when it is rewritten. The
  owner and mode are kept, since the service runs as root and the file belongs to the login
  user.
- It applies at once with no front-end reload, so the PA and switch lines are left alone.
  The operator, engineering and mission views follow on the next `get_config`.
- If the open radio is on a jack the new map locks, the change still goes through and the ack
  carries `ports_conflict`. Later commands on that jack are refused until a re-init.

Who changed it: the section carries a `# last set by` line, `get_config` reports `ports_by`,
the service logs each change as a warning, the panel shows it and every connected GUI puts it
in its activity log. A hand edit picked up by `frontend_reload` shows as a config file reload.

`./deploy.sh --frontend` regenerates the file and keeps the radio's current `ports:`
section. `--port JACK=ROLE[:CHAIN]` (repeatable; roles tx, rx, txrx, none) sets it and
`--ports-clear` removes it. The section needs a service with port map support: an older
`rf_frontend.py` rejects `ports` as an unknown key, so deploy the code first.

### Same-card TX on a 2 jack

RX on a 1 jack with TX on the 2 jack of the same card (TRx1B + TRx2B, TRx1A + TRx2A) opens and
streams, and the map and `G40_SAME_CARD` both allow it, but it does not put real RF out of
the jack: what RX sees is on-card coupling, the same with the RX cable off. The same TX
jack works cross-card. So the init ack carries `same_card_tx` and a `warning`, the GUIs show it
next to the pickers, and Check plan warns. With TX on TRx2B, receive on TRx1A or TRx2A.
On the engineering tab that is the "Transceive cross-card" mode; "TX only: TRx2" and
"Transceive FDD" run the TX card's own RX and hit this case.

## Service commands and GUI

- `frontend_status`: chip:offset, level and `by` (name or fallback) per line; keyed, hold
  and fault state; watchdog time left and trip counts; attenuator readback.
- `frontend_set {pa: bool, tr: "tx"|"rx", safe: true}`: manual control.
- `frontend_reload {path}`: PA off, lines released, the file read and mapped again.
  `frontend_set` and `frontend_reload` are refused during a TX run, but `frontend_set`
  still accepts `safe`.
- `set_ports {ports, by}` or `{clear: true, by}`: change the port map (see "Port map").
- `atten_set {db, startup}` and `atten_get {ident}`; `payload_status`, `payload_key` and
  `payload_restore` key at the payload level with the RX protection check.

Engineering view, Tools panel: PA ON / OFF, Switch TX / RX, SAFE, Status, Reload config,
attenuator Set / Read with "also power-on default", and a chip:offset readout. The
Payload tab has KEY HPA, SAFE, Switch TX / RX, Reload front-end config and the RX
attenuator.

## Checking it without the PA or switch

- `frontend: mock` (or `--frontend mock`) runs the same sequencing over in memory GPIO.
- `bench/rf_frontend_bench.py` drives the live service the way the GUI does, with the
  modes `status`, `manual [hold_s]` and `reload`. Its `init`, `burst` and `cont` modes
  transmit a tone, so use a pad.
- On the G40 host, `gpioinfo gpiochip0 | grep -E 'PG.06|PN.01'` shows both lines `[used]`
  by consumer `dpmg-rf-frontend`, as outputs. The levels read back are the output latch,
  not the pad voltage; metering the socket needs a breakout cable.

```bash
python3 bench/rf_frontend_bench.py status
```

## Attenuator (RCDAT-6G-120H)

The attenuator covers 200 to 6000 MHz, 0 to 120 dB in 0.05 dB steps (0.10 dB above
105 dB), with +23 dBm maximum input. It is a USB HID device, VID 0x20CE PID 0x0023, and
is powered from USB even when controlled over Ethernet.

- `atten.transport: hidraw` with `path: auto` finds `/dev/hidrawN` by VID and PID (hidraw
  is built into the G40 kernel). Install `deploy/g40/60-minicircuits.rules` in
  `/etc/udev/rules.d/` on the host and run `udevadm control --reload` and `udevadm
  trigger`, so group `periph` can open the node. `transport: http` with `url:
  http://<ip>` uses Ethernet instead.
- Every command goes as SCPI (`:SETATT=`, `:ATT?`, `:MN?`, `:SN?`, `:FIRMWARE?`,
  `:STARTUPATT:VALUE:`). Requests are snapped to the step and checked against the limits
  before the device sees them. Keep the power-on value at 120 dB for PA work.
- To check it: `lsusb` shows `20ce:0023`, and `atten_get ident=true` (Read in the Tools
  panel) returns model `RCDAT-6G-120H` and the serial. This line sets 30 dB and reads it
  back:

```bash
python3 -m dpmg.payload.mc_atten hidraw 30
```

### Plug-in detection (`transport: auto`)

With `atten.transport: auto` the service finds the attenuator by itself. While the front
end is open it looks for VID 0x20CE PID 0x0023 on hidraw every 2 s, from its own thread,
so the command loop never waits on it and no GPIO line is touched.

- Nothing found: the state is `not connected`.
- One found: it is opened and read (model, serial, current attenuation) and then used by
  `atten_set`, `atten_get`, `payload_status` and the payload guard as if it had been there
  at start. The state is `connected`, with the node and the time it was attached.
- The attach is read only. No attenuation and no power-on value are written, so plugging
  it in never changes what the RX chain sees. Set the value from the Payload tab or with
  `atten_set` afterwards.
- Two or more found: none is opened and the state says to set `atten.path`. A
  `path: /dev/hidrawN` picks one.
- A unit that goes away (the node disappears, or a read fails with an I/O error) is
  closed and dropped; the state is `lost`. A single timeout is retried once before it
  counts.
- A unit that is found but cannot be opened (no udev rule, wrong group) shows
  `error: <reason>`. It is retried on every scan and the reason is logged once.
- `frontend_reload` and a service stop end the watcher and close the unit.

While the attenuator is missing and a `payload:` section is set, the guard refuses every
key, the same fail-closed rule as a missing attenuator at start. Pulling it out while the
HPA is keyed does not stop that TX; it is logged, and every later key goes through the
guard again. `transport: hidraw` and `http` open the unit at start and on reload, with
no watcher. `none` leaves it off.

`./deploy.sh --frontend --atten auto` writes this section (`--atten none`,
`hidraw[:/dev/hidrawN]` and `http://ip` too). Without `--atten` the radio's current
section is kept, and a first file gets `auto`.

### Switch readback ADC presence

While the front end is open the service also checks every 5 s whether the ADS1x15 is on
the `tr_readback` bus and address (default `/dev/i2c-1`, 0x48), by reading its config
register. Nothing is written. The result is `present`, `absent`, `bus missing` or
`smbus2 missing`. The check is skipped while the HPA
is keyed, the switch is in TX, a TX run is active or a key sequence holds the front-end
lock. It is information only: the readback interlock is still turned on by
`tr_readback.enabled` and nothing else.

Both states are in `payload_status` under `devices` (`atten`: state, model, serial,
path, db, since; `tr_adc`: state, bus, address) and in `frontend_status` under `atten`.
The Payload tab shows them as chips in its Devices row and pops a notice when one
appears or goes away.

## HPA temperature and supply current (Empower 1189 DB9)

The 1189's DB9 control connector carries two analog monitor outputs that the service can
read through the same ADS1115 used for the switch readback, and show on the Payload tab
(the HPA box and the Devices row). This is display only: a missing chip never refuses a
key.

| DB9 pin | signal | scale | typical |
|---|---|---|---|
| 3 | temperature | 10 mV per degree C on a 0.5 V offset: T = (V - 0.5) / 0.01 | 0.75 V at 25 C, 1.35 V at 85 C |
| 2 | supply current | 25 mV per 100 mA: I = V / 0.25 | 0.075 V at 0.3 A (shutdown), 0.375 V at 1.5 A (enabled, quiescent) |

Wiring: the chip must sit on the socket's I2C-1 bus (socket pins 21 to 25 and 27 carry
SPI0 and I2C-1; `/dev/i2c-1` on the G40) at address 0x48 (ADDR pin to ground), the same
chip and address as `tr_readback`. Take DB9 pin 3 to AIN1 and DB9 pin 2 to AIN2, leaving
AIN0 for the switch readback. Payload 2 (drawing "Payload wiring 1189 v2 02Nov26") is wired differently: DB9 pin 2
(current) to ADC0, pin 3 (temperature) to ADC1 and the switch line to ADC3, so its
rf_frontend.yaml has `current_channel: 0`, `temp_channel: 1` and, if the readback is
turned on, `tr_readback.channel: 3`. Tie the DB9 ground pin (the 1189 manual's DB9 table names
it) to the ADC ground and the G40 socket ground, so all three share one reference. Both
outputs run from 0 to about 2.5 V at full drive, so the 4.096 V full-scale range reads
them with no divider; do not pick a lower range. The ADS1115 is powered from a socket +3.3 V pin
(33 mA budget, the chip takes well under 1 mA); its inputs must not exceed the supply
plus 0.3 V, which these outputs cannot. Keep the analog leads short and away from the
RF path.

The carrier's own I2C-1 devices sit at other addresses, so nothing answers at 0x48 until
the chip is fitted. Until then the Payload tab shows the monitor as `not fitted` with
the reason (`no ADC at that address`, or `smbus2 missing` on an image without the
module). `smbus2` is in
`requirements-radio.txt`; an offline radio gets it from the wheel in `deploy/wheels/`
through the overlay build above, or with `pip install smbus2` inside the running
container (lost on the next image rebuild unless the overlay is applied).

Config (`rf_frontend.example.yaml`, `hpa_monitor`): `enabled`, `transport` (`i2c` or
`mock`), `bus`, `address`, `temp_channel` (1), `current_channel` (2), `full_scale_v`
(4.096), the scale numbers `temp_offset_v` (0.5), `temp_v_per_c` (0.01),
`current_v_per_a` (0.25), `current_offset_v` (0), `poll_s` (2: a status call re-reads
the chip at most this often, the cached figure and its age come back in between) and
`valid_when_off` (false: see below), `warn_c` (70: the tab turns the HPA box and the chip amber and prints a warning above
it; the 1189 derates from 85 C). The same channel cannot be given to both the switch
readback and the monitor on one chip. One lock per bus keeps the two readers from
interleaving a conversion.

Reported as `hpa_monitor` in `frontend_status` and `payload_status`: `state` (`ok` or
`not fitted`), `hpa_temp_c`, `hpa_current_a`, the raw `hpa_temp_v` and `hpa_current_v`,
`hot`, `warn_c`, `reason` and `age_s`. The `devices` block gains `hpa_adc` (`present`,
`not fitted`, `sim` or `off`, with bus, address and channels). With the mock front end
(`bench/payload_sim_frontend.yaml`) the figures are simulated: 35 C, and a current that
follows the key (0.3 A off, 1.5 A keyed).

## T/R switch timing from the FPGA (proxy mode), per Epiq

There are two ways to drive the external T/R switch:

- gpio mode: Linux toggles a CPU GPIO (pin 17 or 15, output only) around the software TX
  calls. This is what the service does, and it works on libsidekiq 4.25.0.
- proxy mode: the NV100 FPGA drives the switch in step with the RF transmit window. The
  TR_N connector signals map to GPIO0 and GPIO1 on the NV100 cards. It is set up with
  these user FPGA registers:

  ```c
  #define FPGA_PROXY_MODE_CTRL           0x8800
  #define FPGA_PROXY_MODE_PRE_TX_DELTA   0x8804
  #define FPGA_PROXY_MODE_POST_TX_DELTA  0x8808

  skiq_write_user_fpga_reg(card, FPGA_PROXY_MODE_CTRL, 1);
  skiq_write_user_fpga_reg(card, FPGA_PROXY_MODE_PRE_TX_DELTA, pre_tx_delta);
  skiq_write_user_fpga_reg(card, FPGA_PROXY_MODE_POST_TX_DELTA, post_tx_delta);
  ```

  The pre and post TX deltas are in system clock counts (1.5 us is 60 counts at
  40 MHz). Pick the porch values for the switch and HPA timing.

Known issue: proxy mode is broken in libsidekiq 4.22.0 and later, which includes the
4.25.0 the G40 runs. Epiq lists the fix for 4.27.0, so on 4.25.0 the front end runs in
gpio mode.

## When an HPA monitor reading can be believed

The status carries `quality` next to the figures, and the GUI shows the figures only for `ok`:

| `quality` | when | GUI |
|---|---|---|
| `ok` | HPA keyed, figures possible | the temperature and current |
| `hpa off` | HPA not keyed | HPA off: monitor not valid |
| `floating` | both inputs above 1.5 V and within 0.15 V of each other | inputs floating, check the DB9 |
| `bad` | outside -55 to 125 C, or above 15 A | bad reading |

The datasheet does not say the outputs work in shutdown, and a shut down amplifier can
give figures that are not believable, which is why `hpa off` hides them. If a meter at
the DB9 shows the outputs do work in shutdown, set `valid_when_off: true`. A reading
never gates a key.

## NV100 GPIO lines (GPIO3 / GPIO8), per Epiq

The carrier direction lines select which NV100 GPIO signals reach the G40 connector:
dir-nva-gpio3 and dir-nva-gpio8 on the first NV100, dir-nvb-gpio3 and dir-nvb-gpio8 on
the second. Set a direction line high to make that GPIO an output toward the connector,
or low to make it an input. Keep the libgpiod line request held for as long as the
setting is needed. This makes GPIO3 on NV100 A an output toward the connector:

```bash
gpioset $(gpiofind dir-nva-gpio3)=1
```

The direction line only selects the carrier path. To use NV100 GPIO3 as a custom FPGA
signal on connector pin H2, the FPGA design has to be changed and recompiled so it drives
or reads that signal. The CPU_GP_OUT_1/2 pins (17, 15) are output only. CPU_GPIO (pin 16)
is bidirectional through cpu-gpio-direction (high for output, low for input).
