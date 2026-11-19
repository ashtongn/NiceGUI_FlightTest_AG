# Deployment and operations

How to run and operate the two roles. Addresses come from
`dpmg/deploy_config.py`, or from `.env`; set your own. The defaults are G40
`192.168.0.22`, X40 `192.168.0.26`, x86 GUI `192.168.0.25`, command port
`47810` and discovery port `47811`.

## The two roles

| Role | Where | Container | Image |
|---|---|---|---|
| GUI | x86 control host | `dpmg` | `dpmg` |
| Service | radio Jetson (one per radio) | `dpmg_svc` | `dpmg` |
| GPU channelizer | radio Jetson (for decode) | `dpmg-gpu` | `dpmg-gpu:r36` on the G40 (JetPack 6), `dpmg-gpu` on JetPack 5 radios |

The GUI and the service use the same image with a different command.
`--kind {g40,x40}` picks the radio behavior at runtime, so there is one
codebase and no per-radio fork.

## deploy.sh (from the ground station)

`deploy.sh` runs on the x86. It pushes the code to the radio, brings the radio
service up and starts the GUI. Run it from the repo root:

```bash
./deploy.sh
```

| Option | Use it when | What it does |
|---|---|---|
| (none) | First install, or new code to push | Backs up and pushes the code, rebuilds the radio image (JetPack 5 only), runs the bring-up, starts the GUI. |
| `-n`, `--no-push` | The code on the radio is already current, or after a power cycle | Skips backup, push and rebuild. Starts the service and the GUI. Aliases: `--bringup-only`, `--local`. |
| `--restart` | Service hung or a card stuck, containers still exist | Runs `docker restart` on the radio's GPU and service containers. Takes seconds. |
| `--recreate` | `--restart` did not help | Clears stale processes, the card lock and channelizer shared memory, then recreates the containers from the existing image. About 30 s. Alias: `--fix-fast`. |
| `-r`, `--recover` | `requirements.txt` or the Dockerfile changed | Same as `--recreate`, plus an image rebuild first. Asks once for sudo on the radio. Aliases: `--doctor`, `--fix`. |
| `--frontend` | Wiring the HPA enable, TX/RX switch or HPA alarm | Asks by G40 socket pin, writes and installs `rf_frontend.yaml`, applies it live. Keeps the radio's current `ports:` section (the port map) and `payload` section (the payload guard); see GPIO_FRONTEND.md, "Port map" and "Setup". Never keys RF. |
| `--frontend-config FILE` | You already have an `rf_frontend.yaml` | Validates, installs and applies that file, port map and payload guard included. |

The recovery options skip the push and start the GUI afterwards. Try them in
order: `--restart`, then `--recreate`, then `--recover`. `./deploy.sh --help`
prints the full text.

On a JetPack 5 radio `deploy.sh` brings the service up through
`deploy/radio/dpmg_up.sh`, which uses the RT budget path, and `--recreate` and
`--recover` run `deploy/radio/dpmg_doctor.sh`. On the JetPack 6 G40, detected
from cgroup v2, the bring-up, `--recreate` and `--recover` all run
`deploy/radio/g40_up_r36.sh` instead (next section). There `--recover` cannot
rebuild the image, since the G40 has no internet, so it recreates the
containers from the existing image. The G40 announces to `DPMG_GUI_HOST` if
it is set, otherwise to the x86's own address on the route to the radio.
`deploy.sh` asks for the serial name, defaulting to the one in the radio's
hostname.

## GUI container (x86)

`./launch_gui.sh` syncs the tree, runs the import check, restarts the
container and opens port 8080.

Host networking is required. UDP ANNOUNCE broadcasts never cross Docker's
bridge or a published port, so discovery hears nothing in bridge mode. If the
container is not already `--network host`, or lacks the `~/.dpmg` mount,
`launch_gui.sh` recreates it: image `dpmg`, restart `unless-stopped`, this
repo's `output/` bind-mounted at `/app/output` and `~/.dpmg` at
`/root/.dpmg`. It does not build the image; run `docker build -t dpmg .`
first.

The source is copied into the container, not bind-mounted. The host tree is
the source of truth. The script copies it in with tar over `docker exec`, and
restarts only after an import self-check inside the container passes. Edit on
the host, then run the script again.

## Service container: G40 (JetPack 6)

The G40 runs JetPack 6 (L4T R36.5, Epiq BSP v1.3.0, libsidekiq 4.25). One
script brings up the GPU channelizer and the radio service. On the G40, the
first line starts `dpmg-gpu` and then `dpmg_svc`; the second stops both
gracefully:

```bash
bash deploy/radio/g40_up_r36.sh
bash deploy/radio/g40_up_r36.sh --stop
```

No sudo and no RT grant are needed. The JetPack 6 kernel runs Docker on cgroup
v2 with no per-cgroup RT budget, so the service's real-time TX threads use the
global kernel budget. Do not pass `--cpu-rt-runtime`; Docker rejects it on
cgroup v2.

The script runs the service as below. It mounts the code folder it sits in
(`APP` names another), takes the serial from the hostname
(`matchstiq-g40-XXXX` gives `G40-XXXX`; `DPMG_SERIAL` overrides it) and
passes `DPMG_RX_CPU`, `DPMG_TX_MODE`, `DPMG_TX_BLOCK`, `DPMG_TX_ASYNC_PRIO`
and `DPMG_SECURE_MSG` through from its own environment.

```bash
docker run -d --name dpmg_svc --restart unless-stopped --network host --privileged --ipc=host -w /app_svc -e PYTHONPYCACHEPREFIX=/tmp/pycache -v <radio code folder>:/app_svc -v /usr/lib/epiq:/usr/lib/epiq:ro dpmg:latest python dpmg_service.py --kind g40 --host-type g40 --serial <serial> --cmd-port 47810 --discovery-port 47811 --broadcast 192.168.0.25
```

The flags that matter:

- `--network host`: the UDP link and discovery need the host's network.
- `--privileged`: libsidekiq needs raw device access.
- `--ipc=host`: the service shares POSIX shared memory and semaphores with
  `dpmg-gpu` (the channelizer). Without it GPU decode cannot attach, and
  nothing reports the failure.
- `--serial`: the radio's permanent identity. Discovery keys on it, not the IP.
- `--broadcast`: where ANNOUNCE goes. Set it to the x86's address
  (`DPMG_BROADCAST` overrides it for the script).

One-time host setup on a freshly flashed G40 (sudo), then build the GPU image
on the G40 from the repo:

```bash
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
docker build -f deploy/docker/Dockerfile.gpu.r36 -t dpmg-gpu:r36 .
```

### GPSDO and the GPS antenna bias

The G40's GPS antenna needs its 3.3 V bias, and the bias is off after every
G40 reboot. Without it the GPSDO never locks. Turn it on from the GUI: set
Timing / ref clock to `gpsdo` and tick GPS antenna bias 3.3 V. A 3D fix takes
about a minute with a view of the sky, and lock follows within seconds. Every
bench checkout runs on `gpsdo`, with lock confirmed before keying.

The bench scripts never switch the bias on by themselves. If it reads off they
stop with a message, unless the run was started with `--ant-bias` (only with
an active antenna fitted; a passive antenna can be a DC short). The GUI shows
who last switched the bias, next to the checkbox.

### Secure messaging flag

Secure messages are off by default. The radio forwards sealed frames only
when `dpmg_svc` runs with `DPMG_SECURE_MSG=1` in its environment. Copying
files and restarting cannot add an environment variable, so the container
must be recreated with it. On the G40, `DPMG_SECURE_MSG=1 bash
deploy/radio/g40_up_r36.sh` does that, and running the script again without
the variable turns the feature off. Elsewhere add `-e DPMG_SECURE_MSG=1` to
the `docker run`. The radio holds no keys. Keys stay on the ground station,
where the GUI or the bench reads them from the file named by `DPMG_KEY_FILE`.

## Service container: JetPack 5 radios (X40)

On JetPack 5 the TX path runs `SCHED_FIFO` and needs a Docker cgroup RT
budget, so add `--cpu-rt-runtime=900000` to the `docker run`. Docker resets
`/sys/fs/cgroup/cpu,cpuacct/docker/cpu.rt_runtime_us` to zero whenever a
non-RT container starts, and on every reboot. A grant made before a non-RT
container starts is lost. The grant and the RT container launch must
therefore run back to back, with the `docker run ... --cpu-rt-runtime=900000`
straight after these three lines:

```bash
echo 950000 | sudo tee /sys/fs/cgroup/cpu,cpuacct/cpu.rt_runtime_us
sudo mkdir -p /sys/fs/cgroup/cpu,cpuacct/docker
echo 900000 | sudo tee /sys/fs/cgroup/cpu,cpuacct/docker/cpu.rt_runtime_us
```

`deploy/radio/x40_tx_up.sh` does all of it in one step: it removes the old
container, grants the parent and then the docker child, and runs
`docker run --cpu-rt-runtime=900000 --ipc=host`. Run it on the X40; it asks
once for sudo.

```bash
bash deploy/radio/x40_tx_up.sh
```

`bash deploy/radio/dpmg_up.sh` with a yes to TX gives the same result, but
asks for kind, serial, IP and ports first. Without RT you still get
discovery, RX, PSD and detection; only TX is dead. The service's command line
arguments are fixed when the container is created, so changing them on a
JetPack 5 radio means `docker rm` and `docker run`. That starts a container
again, so grant RT again in the same step.

## Service restart hazard

`docker restart` and `docker rm` hard-kill the service. That can leave a
stale `/dev/shm/skiq_card_mgr*` claim, and the next `skiq_init` fails with
`rc=-16` (busy). The service handles SIGTERM with a clean skiq teardown. If a
claim is stranded anyway, clear it:

```bash
docker run --rm --ipc=host dpmg:latest bash -c "rm -f /dev/shm/skiq_card_mgr*"
```

Prefer `docker stop -t 30` to `docker rm -f`; `deploy/radio/g40_up_r36.sh
--stop` does this.

## Discovery on a firewalled x86

The x86 runs `ufw`, which drops the radios' unsolicited inbound ANNOUNCE. The
reply to a HELLO the GUI sent is part of an established flow and is allowed.
So there are two ways to attach a radio:

- Connect by IP in the engineering view
  (`http://localhost:8080/engineering`, SDR tab). The GUI sends a unicast HELLO
  to the radio, and HELLO_ACK, STATUS and PSD ride the allowed established
  flow. No firewall change is needed, and this is the dependable way to attach
  a radio. The operator console then shows the radio too.
- Autodiscovery. Open the discovery port with
  `sudo ufw allow from 192.168.0.0/24 to any port 47811 proto udp` and set the
  service `--broadcast` to `255.255.255.255` (a limited broadcast survives
  subnet changes) or to the x86's address.

A radio is keyed by serial, so its IP can change and the GUI follows it.

## Syncing code to a radio

The service mounts `/app_svc` from the radio's code folder
(`-v <radio code folder>:/app_svc`), so for the service you can push files and
restart. `./update.sh` rsyncs every changed file and restarts `dpmg_svc`.
`deploy/push_update.sh` copies the named files, with backups and a hash
check; `--g40-only` skips the GUI container. Paths are relative to the repo
root and keep their folders on the radio.

```bash
./update.sh
bash deploy/push_update.sh --g40-only dpmg/radio/dpmg_service.py dpmg/dsp/tools_engine.py
```

The service's command line and environment are fixed when the container is
created, so changing either needs `docker rm` and `docker run`. On the G40,
run `deploy/radio/g40_up_r36.sh` again. For the GUI container, run
`launch_gui.sh` again (it copies the tree in). The GPU container bakes in
`dpmg/gpu/gpu_service.py` and `dpmg/gpu/gpu_channelizer2.py`. Rebuild the GPU
image, or `docker cp` them into `/app` in `dpmg-gpu` as
`deploy/radio/dpmg_doctor.sh` does; a copied file is lost when the container
is recreated.

## The Tools tab transponder

The transponder watches RX, detects any signal above a trigger SNR, buffers
the detections, builds a response waveform from a JSON `response` spec, and
reports the latency. From the bench:

```bash
python3 bench/bench_transceive.py --host 192.168.0.22 transponder --seconds 3 --trigger-snr 15 --resp-json resp.json
```

It reports two latencies per event: the processing latency, from the data
being in hand to a built response, and the total, which adds the capture
dwell in front of it. By default the response is built and measured but not
radiated. `--arm` sends `emulate=false` and keys one real single-shot TX. The
GUI transponder panel (engineering view, SDR tab, Tools panel under the PSD
plot) has the trigger SNR input, a response message text area, a `.json`
upload, an emulate/ARM toggle and the latency readout.

## Related

- [QUICKSTART.md](QUICKSTART.md): first bring-up.
- [FEC.md](FEC.md): the FEC and ACM suite.
- [BRINGUP.md](BRINGUP.md): bring-up runbook and recovery.
