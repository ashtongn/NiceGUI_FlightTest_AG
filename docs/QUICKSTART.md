# Quickstart: from nothing to a running ground station

This guide brings up the GUI on the x86, starts one radio service, attaches
the radio and runs one end to end check. The top level
[README](../README.md) describes the architecture.

The addresses below are the committed defaults in `dpmg/deploy_config.py`:
`192.168.0.22` (G40), `192.168.0.26` (X40) and `192.168.0.25` (x86 GUI). Set
your own in `.env`.

`./deploy.sh` on the x86 does all of this in one command: it pushes code to
the radio, builds the image, brings up the service and starts the GUI. The
steps below do the same by hand. The full runbook is [BRINGUP.md](BRINGUP.md).

## 1. Prerequisites

On the x86 (the GUI host):
- Docker.
- This repo, checked out.
- The Python packages from `requirements.txt` (nicegui, numpy, scipy, pyyaml,
  posix_ipc). The `dpmg` image installs them. `requirements.txt` pulls in
  `requirements-radio.txt`, which pulls in `requirements-gui.txt`. A Windows
  laptop installs only `requirements-gui.txt` (see section 3b).

On each radio (Jetson):
- Docker with the NVIDIA runtime.
- libsidekiq installed at `/usr/lib/epiq`. The container bind mounts it.
- The `dpmg` image for the service and, for GPU decode, the `dpmg-gpu` image.
- On JetPack 5 (X40) only, a kernel with `RT_GROUP_SCHED=y`. TX needs it;
  RX, PSD and detection work without it. JetPack 6 (G40) has no per cgroup
  RT budget, and TX uses the global kernel budget.

Build the images from the repo root. The first line is the service image.
The GPU image, for GPU decode, is built on the radio: the second line on the
G40 (JetPack 6), the third on a JetPack 5 radio.

```bash
docker build -t dpmg .
docker build -f deploy/docker/Dockerfile.gpu.r36 -t dpmg-gpu:r36 .
docker build -f deploy/docker/Dockerfile.gpu -t dpmg-gpu .
```

## 2. Configure hosts

```bash
cp .env.example .env
```

Edit `.env` and set `DPMG_G40_HOST`, `DPMG_X40_HOST` and `DPMG_GUI_HOST` to
your addresses. `dpmg/deploy_config.py` reads these variables, so every tool
picks up the site's addresses. The stock Epiq login is `sidekiq/sidekiq`.
Change it and use SSH keys for automation. Do not commit real addresses or
passwords; git ignores `.env`.

## 3. Bring up the GUI (x86)

```bash
./launch_gui.sh
```

`launch_gui.sh` copies the source into the `dpmg` container, installs the
requirements with pip and runs an import self-check inside the container. It
restarts the container only if the check passes. The GUI needs host
networking, because the UDP ANNOUNCE discovery broadcast cannot cross
Docker's bridge.

Open `http://localhost:8080`, or `http://192.168.0.25:8080` from another
machine.

Options: `--native` runs the GUI without Docker, for development. `--sim`
also starts a simulated G40 service on the host, so the SDR tab has traffic
with no radio.

## 3b. Windows ground station (native, no Docker)

The GUI never loads libsidekiq, so a Windows laptop runs it natively from the
same source tree. Install Python 3.11 or newer from python.org and tick "Add
to PATH". Then, in the repo folder:

```bat
launch_gui.bat
```

PowerShell works too: `powershell -ExecutionPolicy Bypass -File
.\launch_gui.ps1`. Either one creates `.venv`, installs
`requirements-gui.txt` (no `posix_ipc`), runs `run_gui_link.py 8080` and opens
`http://localhost:8080` once the GUI answers. To run beside another GUI, add
`8081 47841` (`-Port 8081 -DiscoveryPort 47841` in PowerShell). Point the
radio at the laptop's address (`--broadcast <laptop IP>` or `DPMG_GUI_HOST`),
the same as for a Linux ground station.

Windows Firewall must pass two inbound UDP flows: the radio's ANNOUNCE on
47811, and the telemetry replies on the GUI's link port. The GUI picks
that port at start up, so allow by program, not by port. Run this as
Administrator, with your own path and radio subnet:

```bat
netsh advfirewall firewall add rule name="DPMG GUI (UDP in)" dir=in action=allow protocol=UDP program="C:\DPMG\NiceGui\.venv\Scripts\python.exe" remoteip=192.168.0.0/24
```

A rule by port works as well: UDP 47811 and 49152 to 65535 from the radio
subnet. Answering the first start "Windows Security Alert" with Allow access
on Private networks does the same. Rules for the Public profile are usually
blocked by policy. Without a rule the radio never appears, and Connect by IP
connects but STATUS and PSD stay stale.

`start_console.bat` in the repo root does the same on 8080 and also opens the
mission planner, which is part of the GUI, at `http://localhost:8080/mission`.
`docs/WINDOWS_CONSOLE.md` covers the Jetson USB link on Windows (two
adapters, the NCM one is live), the firewall rules and the multicast feed.

Docker Desktop does not work for this. Its containers live in a Linux VM.
`--network host` binds inside the VM, and the optional host networking proxy
forwards single ports only, so the ANNOUNCE broadcast never reaches the
container. Run natively as above.

WSL2 is an option for the Linux tooling (`launch_gui.sh --native`, the bench
scripts). It needs Windows 11 22H2 or later and WSL 2.0 or later in mirrored
networking mode: put `networkingMode=mirrored` under `[wsl2]` in
`%UserProfile%\.wslconfig`, then run `wsl --shutdown`. The distro then shares
the laptop's address, so the radio's unicast ANNOUNCE reaches it. Also allow
the UDP in the Hyper-V firewall, as Microsoft's WSL firewall guidance
describes:

```powershell
Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction Allow
```

The radio cannot reach WSL2 in its default NAT mode.

Scenario storage: "Disk" defaults to `output\scenarios` on Windows
(`/app/scenarios` on Linux). "RAM (tmpfs)" is hidden unless
`DPMG_SCENARIO_RAM_DIR` points at an existing RAM drive folder
(`dpmg/deploy_config.py`). The radio side scripts (`deploy.sh`,
`deploy/radio/dpmg_up.sh`, `--sim`) run on Linux or WSL2 only.

## 4. Bring up a radio service

SSH to the radio. The guided script is the easiest way. It asks for the
radio kind, serial, GUI IP, UDP ports and whether to enable TX. It then
clears any stale card claim, starts `dpmg_svc` and checks it, and saves the
answers to `.env`:

```bash
bash deploy/radio/dpmg_up.sh
```

To start the service by hand (no sudo), run this on the G40. It is the
`docker run` that `deploy/radio/g40_up_r36.sh` issues: the script mounts the
code folder it sits in (`APP` names another) and takes the serial from the
hostname (`matchstiq-g40-XXXX` gives `G40-XXXX`; `DPMG_SERIAL` overrides it).

```bash
docker run -d --name dpmg_svc --restart unless-stopped --network host --privileged --ipc=host -w /app_svc -e PYTHONPYCACHEPREFIX=/tmp/pycache -v <radio code folder>:/app_svc -v /usr/lib/epiq:/usr/lib/epiq:ro dpmg:latest python dpmg_service.py --kind g40 --host-type g40 --serial <serial> --cmd-port 47810 --discovery-port 47811 --broadcast 192.168.0.25
```

TX on the G40 (JetPack 6) needs no RT grant: answer "y" to TX in
`deploy/radio/dpmg_up.sh`, or run `bash deploy/radio/g40_up_r36.sh`. On the
X40 (JetPack 5) the RT budget must be granted with `--cpu-rt-runtime=900000`
in the same step as the container start (see [DEPLOYMENT.md](DEPLOYMENT.md));
`bash deploy/radio/x40_tx_up.sh` does this and asks for sudo. If the stack on
a JetPack 5 radio is wedged, `bash deploy/radio/dpmg_doctor.sh` rebuilds and
recovers it. It uses the JetPack 5 RT grant, so on the G40 run
`./deploy.sh --restart` from the x86 or `g40_up_r36.sh` instead.

`--serial` is the radio's permanent identity. `--kind` selects G40 or X40
behaviour at run time. `--broadcast` is the ANNOUNCE target, the x86 address.

A G40 with an external PA, TX/RX switch or RX attenuator also needs its
`rf_frontend.yaml`, written by `./deploy.sh --frontend`. That file also
carries the port map (which SMA jack may do what) and the payload guard; see
[GPIO_FRONTEND.md](GPIO_FRONTEND.md), sections "Port map" and "Setup".

## 5. Attach the radio in the SDR tab with Connect by IP

Open the SDR tab. Type the radio's address (`192.168.0.22`) into Connect by
IP and connect.

Use Connect by IP rather than discovery. The x86 runs `ufw`, which drops the
radio's unsolicited ANNOUNCE broadcasts to UDP 47811. With Connect by IP the
GUI starts the exchange with a unicast HELLO, so the HELLO_ACK, STATUS and PSD
replies come back on a flow the firewall already allows. This is the
dependable path on a firewalled x86.

To allow passive discovery instead, open the discovery port once:

```bash
sudo ufw allow from 192.168.0.0/24 to any port 47811 proto udp
```

The radio card starts with a placeholder serial (`@ip:port`) and switches to
the real serial from the first HELLO_ACK. The first NV100 RFIC calibration
takes about 35 s, so init can take up to about 60 s.

## 6. Run one end to end check

In the SDR tab, init the radio and start RX. The live PSD (4 Hz) and the
detections stream in. Retune the centre frequency and check that the PSD
centre moves.

From the command line, `bench/bench_transceive.py` talks to the service
with the protocol client directly. In order, the lines below print one STATUS
heartbeat, init the radio and check `chan_mode`, start RX, and run the
channelizer read-IQ detect test for 2 s:

```bash
python3 bench/bench_transceive.py --host 192.168.0.22 status
python3 bench/bench_transceive.py --host 192.168.0.22 init-dual
python3 bench/bench_transceive.py --host 192.168.0.22 start-rx
python3 bench/bench_transceive.py --host 192.168.0.22 chan-test --seconds 2
```

`bench/bench_transceive.py` defaults to the old address `192.168.40.101`, so
always pass `--host` with the radio's address. Any command that keys TX
refuses to run without `--arm`.
