# DPMG: Data Packet Message Generator and ground station SDR

DPMG builds bit level message frames, turns them into IQ samples and
transmits them through an Epiq Sidekiq radio. On receive, a GPU wideband
channelizer detects and decodes bursts. The repo also holds a signal
generator and automated checkout toolbox, a JSON driven FSK and 4FSK scenario
builder, and a detect and respond transponder.

To get started, read [docs/QUICKSTART.md](docs/QUICKSTART.md).

## Folder map

```
dpmg_service.py     radio service entry; every radio container runs it (code in dpmg/radio/)
run_gui_link.py     GUI on a chosen port (dev box, Windows console)
deploy.sh update.sh launch_gui.sh   laptop commands: deploy to a radio, push code, start the GUI
start_console.bat launch_gui.bat launch_gui.ps1   Windows console launchers
Dockerfile          the dpmg image (GUI on the x86, service on the radio)
dpmg/               all importable code
  radio/            the radio service and the hardware backends (sdr_core, skiq_bridge, ...)
  link/             wire protocol and the GUI side of the link (proto, link_client, view_sync)
  dsp/              detection, channelizer, decode, AMC and IQ file helpers
  gpu/              GPU channelizer container code (copied flat into the dpmg-gpu image)
  framing/          frame codec, frame spec, CRC
  modem/            packet modem and the RX, decode, TX response loop
  modulation/ demodulation/ fec/ encoding/   waveforms, demodulators, FEC codes
  secure/           secure messages (keys, nonces, replay window, ground opener)
  scenario/         scenario JSON builder and channel plans
  payload/          GPIO front end, attenuator, payload budget and guard
  web/              NiceGUI app and its tabs
  mission/          mission planner (/mission)
  bitpipe/          streamed link (/bitpipe)
  services/ ui/     encoder and validator helpers used by the GUI
  paths.py          the repo root, for files found by location
native/             C helpers loaded with ctypes (rxdrain.c; native/build.sh writes native/lib/)
deploy/radio/       scripts run on the G40/X40 (g40_up_r36.sh, dpmg_up.sh, dpmg_doctor.sh, x40_tx_up.sh)
deploy/docker/      GPU channelizer Dockerfiles
deploy/g40/ ntp/ wheels/ push_update.sh   G40 extras, clock sync, offline wheels, in-place push
bench/              hardware drivers and checkouts (first_checkout.py, bench_transceive.py)
tests/              offline tests (tests/run_all.sh runs them all)
tools/              generators and probes (maps, payload workbook, AMC eval, reorg helpers)
docs/               guides; docs/CODEMAP.md says what each file does
scenarios/          curated scenario specs and example plan files
data/ output/       generated at run time, not in git
```

## Everyday commands

Run these from the repo root on the x86, except `g40_up_r36.sh`, which runs
on the G40. The comment above each command says what it does.

```bash
# start the GUI in the dpmg container on http://localhost:8080
./launch_gui.sh
# push code to a radio, bring up its service, start the GUI
./deploy.sh
# push only the changed files to a radio and restart dpmg_svc
./update.sh
# on the G40: start dpmg-gpu and dpmg_svc
bash deploy/radio/g40_up_r36.sh
# offline test suite; the results table goes to /tmp/dpmg_run_all
tests/run_all.sh -j 8
# simulated radio for GUI work
python3 dpmg_service.py --simulate --kind mock --serial G40-SIM --cmd-port 47910 --discovery-port 47911 --broadcast 127.0.0.1
```

## Architecture

One codebase serves two roles on two hosts:

- The GUI runs on the x86 control host. It is a NiceGUI web app for waveform
  authoring, radio control, PSD, waterfall and constellation plots, detection
  tables and the Tools tab. It runs in Docker on the x86 and serves the UI on
  port 8080. The FEC suite is in `dpmg/fec/`. Its ACM planning tab exists but
  is switched off; see docs/FEC.md.
- The service, `dpmg_service.py`, runs headless on the Jetson of each Sidekiq
  radio. It drives libsidekiq through the ctypes bridge and offers init, RX,
  TX, detection and the tools over a small UDP protocol (`dpmg/link/proto.py`).

The GUI and service talk over UDP. Discovery (ANNOUNCE and HELLO) is keyed on
the radio's serial, never its IP. After the HELLO the link carries commands, a
1 Hz STATUS, quantized PSD frames at 4 Hz, and detections. The operator
attaches a radio in the SDR tab. Because the serial identifies the radio, its
IP can change without breaking the link.

The G40 and X40 share one code path. They differ only at run time, through
`--kind`. `make_backend()` returns the same `EpiqBackend` for both, and the
backend branches on the stored `_kind`. For example, the X40 forces
`RF_PORT_CONFIG_FIXED` for its dedicated RX and TX connectors. There is no
separate source tree per radio.

The signal path:

```
TX:  author bits, modulate, compose wideband IQ, transmit on the radio

RX:  receive on the radio
       wideband frame
       energy detector (FFT slot grid), passes active slots only
       GPU channelizer (dpmg-gpu), one baseband stream per channel
       demodulation and frame decode
       burst history and decode log
```

The GPU channelizer runs in its own container (`dpmg-gpu`, CuPy on L4T). The
service reaches it through POSIX shared memory, so the service and GPU
containers both run with `--ipc=host`.

### Bitpipe: the streamed link

Everything above moves single messages. The bitpipe (`dpmg/bitpipe/`) adds a
continuous two way link over the same radios. Chat and files go in at one
end and come out at the other. Frames lost on a noisy path are sent again. It has three parts:

- The container runs on the console or a laptop, where the keys are. It
  splits application bytes into numbered frames and multiplexes the streams.
  It seals each frame with AES-256-GCM-SIV (the same `dpmg/secure/` code the
  secure messages use), adds a header and CRC, codes each frame as one
  `dpmg/fec` block, interleaves it and puts the 32 bit sync in front.
  Receive runs the same steps in reverse, with the selective repeat ARQ before
  the decrypt. Acknowledgements ride in every frame going the other way.
- The stream modem runs inside `dpmg_service.py` on the radio host. The
  `bitpipe_start` command starts it. It asks the container for bits at its
  own symbol rate, modulates them (2-FSK, 4FSK, BPSK or QPSK) and feeds the
  scheduler's TX path as one unbroken stream. It demodulates the RX frames and
  sends soft bits back to the container. When the container has nothing to
  send it answers with idle frames, so the carrier never drops and the far
  end stays locked. The modem never sees clear text or keys.
- The data plane between them is a TCP stream of small records (pull, bits,
  soft bits, hello, status). On the bench the modem listens on port 47899 and
  the container dials out to it, because the console firewall drops new
  inbound connections.

```
  console or laptop                               radio host (G40, Super Nano)
  +-----------------------------+                 +-----------------------------+
  | chat 47901   file 47902     |                 | dpmg_service.py             |
  | control 47898 (JSON lines)  |                 |                             |
  |                             |    TCP 47899    |  stream modem               |
  | BitpipeContainer            |-----------------|    pulls frames, modulates, |
  |   streams, ARQ, encrypt,    |  record stream  |    demodulates, soft bits   |
  |   CRC, FEC, interleave,     |                 |                             |
  |   sync                      |                 |  scheduler TX source and    |
  |                             |                 |  RX frame hook              |
  | keys stay on this side      |                 +--------------+--------------+
  +-----------------------------+                                |
                                                           radio TX and RX
```

The GUI has a `/bitpipe` view. It shows the link state, bit and frame error
rates and the round trip, with a chat box and file send. The command below
runs the same panel on its own; add `--sim` for a simulated far end with no
radio. The modem picks the waveform; the container only deals in bytes and
bits.

```bash
python3 -m dpmg.bitpipe.peer --panel 8095
```

The bitpipe runs with any of its four waveforms on one radio in FDD loopback
or between two radios over a cabled path, clear or encrypted.
[docs/BITPIPE.md](docs/BITPIPE.md) has the frame layout, the service commands
and the hardware harness.

## Features

- Message encoder: define a frame (sync, length, payload, CRC), then modulate
  it and write it to an IQ file.
- Modulations: FSK, 4FSK and GFSK tone modes; the PSK family (BPSK, QPSK,
  pi/4-QPSK, DQPSK, OQPSK); the higher order set 8PSK, 8QAM, 16QAM, 16APSK
  and 32APSK; DSSS and CSS/LoRa spread spectrum waveforms.
- GPU channelizer: a wideband FFT energy detector on a fixed slot grid passes
  active bands to a batched CuPy channelizer. Each channel is demodulated and
  frame decoded, with a live DETECTED, DECODED and LOST timeline and CSV
  export.
- Tools tab: test signal generators (LFM chirp, tone, two tone, band noise),
  an automated ground checkout (RX floor, armed TX to RX loopback SNR and
  isolation), region carve, a channelizer read-IQ test, and a detect and
  respond transponder. The transponder emulates its TX by default; it keys
  the radio only when armed with emulation off. See
  [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
- JSON scenario generator: `dpmg/scenario/gen_scenario_json.py` turns a
  `.json` spec into a composite `.iq.bin` and a channelizer `.plan.json`,
  with parametric FSK and 4FSK and a preset library.
- FEC suite: `dpmg/fec/` has soft Viterbi, RS(255,223), QC-LDPC, TPC and an
  ACM controller. The ACM GUI tab is switched off. See
  [docs/FEC.md](docs/FEC.md).
- Several radios at once (for example G40 and X40), each with its own radio
  card in the GUI.
- Bitpipe: a continuous two way link (chat and files) over one or two radios,
  encrypted and FEC coded, with retransmission. See
  [docs/BITPIPE.md](docs/BITPIPE.md).

## Platforms

| | G40 | X40 |
|---|---|---|
| Radio | 2x Sidekiq NV100 | Matchstiq X40, 18 GHz, 4R1T |
| libsidekiq | 4.25.0 (JetPack 6 / BSP v1.3.0) | 4.21.0 (API-compatible) |
| Sample rate | 0.541667 to 61.44 Msps (21 Epiq-listed rates; NV100 datasheet 24 ksps to 61.44 Msps) | 50 Msps floor (see below) |
| Notable | primary decode platform | wideband survey and TX; no narrowband decode (see below) |

The bridge also has a mock backend (`--simulate`) for work without hardware.
The USB bench radios on the Jetson Orin Nano Super (Ettus B200mini,
ADALM-PLUTO, Wavelet xSDR, HackRF Pro, LimeSDR Mini) have their own repo,
the Super Nano tree, with launch scripts, Dockerfiles, docs and tests.
Their backends are still compiled into `dpmg/radio/sdr_core.py` here.

The X40 50 Msps floor: the X40's loaded FPGA bitstream has no sample
decimation build, so every requested rate rounds up to 50 Msps. The 64k RX
FIFO then overflows under drain jitter, which shows as RXGAP splices.
Detection, wideband survey and TX generation work at that rate. Narrowband
over the air decode is limited by throughput and needs a bitstream with
decimation from Epiq. Build X40 TX scenarios at 50 Msps.

On the G40, build scenarios at the rate the radio will run.

## Documentation

Getting started
- [docs/QUICKSTART.md](docs/QUICKSTART.md): from nothing to a running GUI, one radio and an end to end check.
- [docs/WALKTHROUGH.md](docs/WALKTHROUGH.md): guided tour of the main features on the G40, Setup A.
- [docs/CODEMAP.md](docs/CODEMAP.md): what each file in the tree does.
- [docs/Payload_2_Wiring_G40_1189.pdf](docs/Payload_2_Wiring_G40_1189.pdf): Payload 2 wiring, Matchstiq G40 and Empower 1189.

Radio bring-up and operations
- [docs/BRINGUP.md](docs/BRINGUP.md): bring-up and verification runbook (GUI, fresh radio install, recovery) for the G40 (JetPack 6) and the X40 (JetPack 5).
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md): running the containers, the RT budget, syncing code, the transponder.
- [docs/NETWORKING.md](docs/NETWORKING.md): every port, protocol and service on the wire, firewall rules for a new console, and what blocks discovery, telemetry, the position feed or clock sync on a new network.
- [docs/WINDOWS_CONSOLE.md](docs/WINDOWS_CONSOLE.md): the console on a Windows laptop.
- [docs/G40_BSP_UPGRADE_NOTES.md](docs/G40_BSP_UPGRADE_NOTES.md): extra steps for reflashing the G40 with the Epiq BSP.
- [docs/G40_OFFLINE_BRINGUP.md](docs/G40_OFFLINE_BRINGUP.md): Docker and the containers on a G40 with no internet, from an offline bundle.
- [docs/RX_WEDGE_WATCHDOG.md](docs/RX_WEDGE_WATCHDOG.md): the NV100 RX wedge, the watchdog settings and the driver fix.
- [docs/GPIO_FRONTEND.md](docs/GPIO_FRONTEND.md): wiring and setup for the external PA enable, TX/RX switch and USB attenuator. The same `rf_frontend.yaml` also carries the port map and the payload guard (sections "Port map" and "Setup").
- [docs/LOGGING.md](docs/LOGGING.md): the operator's Log information files and columns, and what `ref_locked` does not show.

Features
- [docs/MISSION_PLANNER.md](docs/MISSION_PLANNER.md): the mission view: plans, timeline and location steps, running them, the map and the clocks.
- [docs/RADIO_ROLE_AND_FPGA.md](docs/RADIO_ROLE_AND_FPGA.md): the radio role (what each radio is acting as) and the FPGA image panel.
- [docs/DSSS_BUILDER.md](docs/DSSS_BUILDER.md): LFSR codes, carrier families and multi-user DSSS overlays.
- [docs/FEC.md](docs/FEC.md): the CDM-625A class FEC suite and how to run it.
- [docs/BITPIPE.md](docs/BITPIPE.md): the bitpipe streamed link: container, stream modem, data plane, hardware harness.
- `docs/manuals/DPMG_Users_Manual.pdf`: the operator and mission planner screens step by step, with appendices on the modulations, FEC, jammers, IQ files and the JSON formats. Written for a reader with no engineering background.
- `docs/datasheets/`: datasheets for the payload parts, opened from the Payload tab. `tools/gen_payload_workbook.py` generates `docs/Payload_EIRP_Link_Budget.xlsx`.

All addresses come from `dpmg/deploy_config.py`, and environment variables
override them. Copy `.env.example` to `.env` and set your own hosts. The
committed defaults (X40 `192.168.0.26`, x86 `192.168.0.25`) match one bench.
The G40 address has no default, as there is more than one G40: set
`DPMG_G40_HOST`, or give `--host`, and the deploy scripts and bench tools
refuse to run without one.

## Requirements

- Docker, with the NVIDIA runtime on the radio Jetson for the GPU container.
- An Epiq Sidekiq radio (G40 or X40) with libsidekiq on the radio host.
- TX runs `SCHED_FIFO` threads. On JetPack 6 (G40) they use the global kernel
  RT budget and need nothing extra. On JetPack 5 (X40) the kernel needs
  `RT_GROUP_SCHED` and Docker needs an RT budget grant
  (`--cpu-rt-runtime=900000`). See docs/BRINGUP.md.
- Python packages from `requirements.txt` (NiceGUI, numpy, scipy, pyyaml,
  posix_ipc). It is split into `requirements-gui.txt` (the GUI, installable on
  Windows) and `requirements-radio.txt` (adds posix_ipc). A Windows laptop
  runs the GUI natively with `launch_gui.bat` or `launch_gui.ps1`; see
  docs/QUICKSTART.md section 3b. `requirements-dev.txt` adds pytest for the
  pytest based tests in `tests/`. It is for the dev host only, not the images.

## Development notes

- The TX path is sensitive to timing. It runs `SCHED_FIFO` with garbage
  collection disabled while streaming. On JetPack 5 the RT budget is granted
  per host. It resets on reboot and whenever a container without RT starts, so
  grant it again before launching the RT service container.
- The NV100 is QI native. IQ against QI wire order matters on the G40, and the
  bridge pins QI by default. How a file's IQ order is read is chosen per radio
  in the GUI.
- The GPU service is a separate process in its own container, reached only
  over shared memory. It starts and is health checked on its own.
