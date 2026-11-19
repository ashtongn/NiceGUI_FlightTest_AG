# Bring-up and verification runbook

This runbook starts the GUI on the x86, brings a radio online, checks it, and
recovers it if it wedges. One codebase serves two roles: the GUI runs on the
x86 control host and the radio service on each radio's Jetson.

Addresses come from `dpmg/deploy_config.py` or `.env`. The defaults are G40
`192.168.0.22`, X40 `192.168.0.26`, x86 GUI `192.168.0.25`, command port
`47810` and discovery port `47811`. Every step ends with a Verify line. A
fresh install is healthy once B5 and B6 pass.

The two radios run different JetPack releases, so some steps differ:

| | G40 | X40 |
|---|---|---|
| JetPack / L4T | JetPack 6, L4T R36.5 (Epiq BSP v1.3.0) | JetPack 5, L4T R35 |
| libsidekiq in `/usr/lib/epiq` | 4.25.0 (4.19.0 to 4.24.0 names link to it) | 4.21.0 |
| Docker cgroup | v2, no per-cgroup RT budget | v1, RT budget per cgroup |
| TX needs sudo | no | yes, one prompt for the RT grant |
| GPU channelizer image | `dpmg-gpu:r36` (`deploy/docker/Dockerfile.gpu.r36`) | `dpmg-gpu` (`deploy/docker/Dockerfile.gpu`) |
| Start script | `deploy/radio/g40_up_r36.sh` | `deploy/radio/x40_tx_up.sh` |
| Recovery script | `deploy/radio/g40_up_r36.sh --stop`, then `deploy/radio/g40_up_r36.sh` | `deploy/radio/dpmg_doctor.sh` |

`deploy/radio/dpmg_up.sh` works on both; it skips the RT grant on JetPack 6.

On the G40 (JetPack 6, Epiq BSP v1.3.0):

- The R36 kernel is built without `CONFIG_RT_GROUP_SCHED` and Docker uses
  cgroup v2, so SCHED_FIFO threads draw on the global
  `kernel.sched_rt_runtime_us`. Docker rejects `--cpu-rt-runtime` there.
- The GPU image `dpmg-gpu:r36` is built from
  `deploy/docker/Dockerfile.gpu.r36` on `l4t-jetpack:r36.4.0` (CUDA 12,
  `cupy-cuda12x` 12.3.0). `deploy/docker/Dockerfile.gpu` is the JetPack 5
  version for the X40.
- `deploy/radio/g40_up_r36.sh` reads `APP` (the service code folder; by
  default the folder the script sits in, two levels above `deploy/radio/`)
  and `DPMG_BROADCAST` (the GUI host to announce to, default 192.168.0.25).
  The serial comes from the hostname (`matchstiq-g40-XXXX` gives `G40-XXXX`);
  `DPMG_SERIAL` overrides it.
  `deploy/radio/g40_up_r36.sh --stop` stops both containers cleanly.

## After a reboot (nothing to run)

Every container runs with `--restart unless-stopped`, so after a power cycle
they come back on their own: `dpmg` (the GUI) on the x86, `dpmg_svc` and
`dpmg-gpu` on the G40. Verify: `docker ps` on each box shows them Up, and http://localhost:8080/
shows the G40's card with LINK OK.

A reboot does reset the radio state. Set it again in the GUI:

1. Set Timing / ref clock to gpsdo and tick "GPS antenna bias 3.3 V". The G40
   turns the antenna bias off on every boot, and without it the GPSDO never
   locks.
2. Set the sample rate, center frequency, RX and TX ports and TX attenuation.
3. Press START. The first init after a boot runs the NV100 RF calibration and
   can take up to about 60 s.

Verify: the status line shows STREAMING and ref LOCKED. Use the rest of this
runbook only when the containers are missing (deleted, or a new install) or
broken.

## Fastest path: one command from the x86

```bash
./deploy.sh
```

The script authenticates to the radio once. Over that one connection it:

1. backs up the radio's current code tree, if you say yes;
2. pushes this repo's code, leaving out `epiq_libs`, `.git`, `.env`,
   generated data and recordings, `output`, every `.md` file and the ground
   station files (`bench`, `tests`, `docs`, `tools`, `deploy/g40`,
   `deploy/push_update.sh`, the deploy and launch scripts,
   `requirements-dev.txt`), and on a JetPack 6 radio the JetPack 5 scripts
   and GPU Dockerfile;
3. on a JetPack 5 radio (the X40), rebuilds the `dpmg` image and runs the
   radio's `deploy/radio/dpmg_up.sh`, which asks for kind, serial, ports and
   TX and starts `dpmg_svc`. On a JetPack 6 radio (the G40, detected from
   cgroup v2) it does not rebuild: the G40 has no internet for the build and
   the code is bind-mounted. It asks for the serial name (default from the
   hostname), stops the containers gracefully and starts `dpmg-gpu` and
   `dpmg_svc` with `deploy/radio/g40_up_r36.sh`, TX enabled, no sudo;
4. starts the GUI here on the x86.

Then verify with B5 and B6. Parts A and B do the same thing by hand.

Other `deploy.sh` options: `--no-push` (the code is already on the radio,
only bring it up), `--restart` (`docker restart` both radio containers),
`--frontend` (set up the G40 PA enable and TX/RX switch GPIO file).
`./deploy.sh --help` lists them all.

libsidekiq is not baked into any image. The service mounts the radio's
`/usr/lib/epiq` read-only at runtime, so nothing ships from the x86 and no
version can mismatch. If the radio has no `/usr/lib/epiq/libsidekiq.so.*`,
`deploy.sh` stops and tells you to install the Epiq SDK there first.

---

## Part A: GUI (on the x86, `192.168.0.25`)

A1. Configure addresses (once), only when yours differ from the defaults in
`dpmg/deploy_config.py` (G40 192.168.0.22, X40 192.168.0.26, GUI host
192.168.0.25, ports 47810 and 47811). Copy the example file, then edit
`DPMG_G40_HOST`, `DPMG_GUI_HOST` and the ports:

```bash
cp .env.example .env
```

`deploy.sh` and `update.sh` load `.env` when they start. The Python code reads
only the environment, so load the file before you check it:

```bash
set -a; . ./.env; set +a; python3 -c "from dpmg import deploy_config as c; print(c.G40_HOST, c.GUI_HOST)"
```

A2. Build the image (first time):

```bash
docker build -t dpmg .
```

Verify: `docker image inspect dpmg >/dev/null && echo ok`. The GUI needs no
libsidekiq. The skiq bridge is optional and the GUI never opens a local radio,
so the image holds no Epiq libraries, and the tree has no `epiq_libs/` folder.

A3. Launch:

```bash
./launch_gui.sh
```

The script syncs the tree into the `dpmg` container (host networking),
installs the requirements, runs an import self-check inside the container,
restarts the container only if the check passes, and opens port 8080. It does
not build the image; without one it stops and asks for A2. Verify: the script
prints `serving: <title>...` and the pages load at http://localhost:8080/
(operator console) and http://localhost:8080/engineering.

For a preview with no radio, `./launch_gui.sh --sim` also starts a simulated
G40 service on the x86. `./launch_gui.sh --native` runs the GUI without
Docker, on 8080, or 8081 if 8080 is taken.

---

## Part B: Radio (on the G40, `192.168.0.22`)

### The guided script

```bash
bash deploy/radio/dpmg_up.sh
```

It asks for kind, serial, GUI IP, UDP ports and TX. It then clears any stale
card claim, starts `dpmg_svc` and, if the `dpmg-gpu:r36` image is present,
`dpmg-gpu`, and checks the result. It saves your answers to `.env`. On the G40
the TX question says "no sudo needed". On the X40 a yes to TX asks once for
sudo for the RT grant. Verify: it ends with `[OK] dpmg_svc is up` and the
Connect by IP hint.

Once you know your settings, `bash deploy/radio/g40_up_r36.sh` does the same
on the G40 with no questions (`bash deploy/radio/x40_tx_up.sh` on the X40).
Then skip to B5. The steps below do the same thing by hand, for a fresh
install.

### Step by step (fresh install)

B1. Host prerequisites. Check that the nvidia runtime is listed (and on the
G40, Cgroup Version 2), that libsidekiq is installed, and that the code tree
is present. `<radio code folder>` below is where `deploy.sh` pushed the tree
(`DPMG_RADIO_CODE_DIR`, default in `dpmg/deploy_config.py`):

```bash
docker info | grep -i -E "runtimes|cgroup version"
ls /usr/lib/epiq/libsidekiq.so*
ls <radio code folder>/dpmg_service.py
```

If `nvidia` is missing from the runtimes (`dpmg-gpu` needs it), register it:

```bash
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
```

Verify: the nvidia runtime is listed, `libsidekiq.so.4.25.0` exists (G40) and
the code tree is present. On the X40 also check that
`zcat /proc/config.gz | grep RT_GROUP_SCHED` shows `=y`; TX needs it.

B2. Build the images on the radio. The second line is for the G40; on the X40
use `-f deploy/docker/Dockerfile.gpu -t dpmg-gpu` instead.

```bash
cd <radio code folder>
docker build -t dpmg .
docker build -f deploy/docker/Dockerfile.gpu.r36 -t dpmg-gpu:r36 .
```

Both builds need internet (apt and pip), which the G40 normally does not have.
Rebuild only when `requirements.txt` or a Dockerfile changed. Code changes are
live through the bind mount without a rebuild. libsidekiq is not in the image;
the service mounts the radio's own `/usr/lib/epiq` at runtime. Verify:
`docker image inspect dpmg dpmg-gpu:r36 >/dev/null && echo ok`.

B3. Clear any stale card claim:

```bash
docker run --rm --ipc=host dpmg:latest bash -c "rm -f /dev/shm/skiq_card_mgr*"
```

Verify: no error. A stranded claim makes the next `skiq_init` fail `rc=-16 busy`.

B4. Start the GPU channelizer, then the service:

```bash
docker run -d --name dpmg-gpu --runtime nvidia --ipc=host --restart unless-stopped dpmg-gpu:r36
docker run -d --name dpmg_svc --restart unless-stopped --network host --privileged --ipc=host -w /app_svc -e PYTHONPYCACHEPREFIX=/tmp/pycache -v <radio code folder>:/app_svc -v /usr/lib/epiq:/usr/lib/epiq:ro dpmg:latest python dpmg_service.py --kind g40 --host-type g40 --serial <serial> --cmd-port 47810 --discovery-port 47811 --broadcast 192.168.0.25
```

This is what `deploy/radio/g40_up_r36.sh` runs. The script mounts the code
folder it sits in (`APP` names another), takes the serial from the hostname
(`DPMG_SERIAL` overrides it) and passes the optional `DPMG_RX_CPU`,
`DPMG_TX_MODE`, `DPMG_TX_BLOCK`, `DPMG_TX_ASYNC_PRIO` and `DPMG_SECURE_MSG`
variables through when they are set. On JetPack 6 TX
works from this container as it is. SCHED_FIFO threads use the global kernel
budget (`/proc/sys/kernel/sched_rt_runtime_us`, 950000), so there is no RT
grant and no `--cpu-rt-runtime` (Docker rejects that option on cgroup v2). Verify: `docker ps --filter name=dpmg` shows both Up. `docker logs dpmg-gpu`
ends with `ready, waiting for frames`. `docker logs dpmg_svc` shows
`service <serial> (g40) up` and no skiq errors.

B5. Attach from the GUI with Connect by IP: engineering view, SDR tab, enter
`192.168.0.22`. Use the IP, not autodiscovery. The x86 `ufw` drops the
radio's unsolicited ANNOUNCE. With Connect by IP the GUI sends the HELLO, so
the replies ride a flow the firewall allows. Verify: a host card appears and
changes its key from `@192.168.0.22:47810` to the radio's serial. The card
shows the radio's libsidekiq version once the first init (B6) has run; the
service reports it in the init ack, not at connect.

B6. First light, init and RX. In the SDR tab set the clock, then init and
start RX. This is receive only: nothing transmits and no loopback cable is
needed (that is B8). Pick the clock this way:

- gpsdo, with the GPS antenna bias on, when a GPS antenna is fitted;
- external when a 10 MHz and 1 PPS reference is wired to REF IN and 1PPS IN
  and there is no GPS antenna. On a G40 this also needs
  `sudo /usr/bin/epiq-gps-ref ext10M` on the radio first, or the init is
  refused. That command alone does not put the 10 MHz on the cards: the
  10 MHz reaches them through the HMC7044, set by hand with `hmc_from_file`
  and `clkin_input_source = 1`, and DPMG neither sets nor reads it (see
  LOGGING.md, "What ref_locked does not tell you");
- internal for a quick check with neither.

Verify: a live PSD, and retuning the center moves it. Ref shows LOCKED once
the GPSDO locks. On external the ref flag is the GPS module's report, not
proof that the 10 MHz is in use; check that with `pps_tester` as LOGGING.md
describes. With nothing on the
RX jack expect a flat noise floor and few or no detections; detections need a
signal at the RX port (an antenna, or the B8 cable). The first NV100 RFIC
calibration takes about 35 s, so init can take up to about 60 s.

The same check from the command line. `init-dual` sets
up RX TRx1A and TX TRx2A and does not transmit.

```bash
python3 bench/bench_transceive.py --host 192.168.0.22 status
python3 bench/bench_transceive.py --host 192.168.0.22 init-dual
python3 bench/bench_transceive.py --host 192.168.0.22 start-rx
python3 bench/bench_transceive.py --host 192.168.0.22 chan-test --seconds 2
```

B7. TX. The G40 needs nothing extra: the containers from B4 can transmit. On
the X40, TX needs the RT budget granted and the service started with
`--cpu-rt-runtime=900000`, in one step. Run this on the X40 only; it asks once
for sudo.

```bash
bash deploy/radio/x40_tx_up.sh
```

Verify (X40): it prints `docker rt_runtime: 900000` and
`RT=900000 status=running`. Without RT the X40 still gives discovery, RX, PSD
and detection; only TX is dead.

B8. Armed loopback checkout, if cabled jack to jack through a pad:

```bash
python3 bench/bench_transceive.py --host 192.168.0.22 checkout --arm
```

Or use SDR tab > Tools > Ground checkout. Verify: RX floor, TX to RX loopback
SNR, frequency error and TX/RX isolation are all within bounds.

---

## Recovery

### G40 (JetPack 6)

Try the fastest step first:

1. Restart the containers (seconds). From the x86 run `./deploy.sh --restart`,
   or on the G40 run `docker restart dpmg-gpu dpmg_svc`.
2. Recreate them (about 30 s), on the G40:

   ```bash
   bash deploy/radio/g40_up_r36.sh --stop
   bash deploy/radio/g40_up_r36.sh
   ```

   `--stop` uses `docker stop -t 30`, so the service shuts down cleanly and
   releases the cards. Never `docker rm -f` a running `dpmg_svc`. The SIGKILL
   leaves the cards claimed in `/dev/shm/skiq_card_mgr*`, and every
   libsidekiq user reports "card in use" until B3 is run or the G40 reboots.
   From the x86, `./deploy.sh --recreate` and `./deploy.sh --recover` both run
   this script on a JetPack 6 radio; `--recover` cannot rebuild the image
   offline, so it recreates from the existing one.
3. If the cards stay busy (`rc=-16`), stop the containers, run B3, then start
   again. If that fails, reboot the G40.

Do not run `deploy/radio/dpmg_doctor.sh` on the G40. It uses the JetPack 5 RT
grant and `--cpu-rt-runtime`, which JetPack 6 does not have.

### X40 (JetPack 5): `deploy/radio/dpmg_doctor.sh`

Run it on the X40 when the stack is wedged: the service will not start,
`rc=-16 busy`, or the GPU channelizer is stuck. It rebuilds the image and
recreates both containers, so the radio is offline for about 1 min.
`--no-rebuild` skips the image build (about 30 s).

It does not touch your source code. It reads the tree for a syntax check and
the image build, and bind-mounts it live into `dpmg_svc`. The only file copy
goes into the `dpmg-gpu` container. Your `.py` files on disk are never
changed, replaced or deleted. There is one sudo prompt, then it runs the whole
recovery: kill stale processes, `docker rm -f` the containers, clear the
`/dev/shm` locks, rebuild, start `dpmg-gpu` and `dpmg_svc`, and verify.

```bash
DPMG_KIND=x40 bash deploy/radio/dpmg_doctor.sh
```

It reads `.env` and the `DPMG_*` variables for kind, serial, GUI host, ports
and code folder. Run `bash deploy/radio/dpmg_up.sh` once first if your
settings differ from the defaults. From the x86, `./deploy.sh --recreate` (no
rebuild) and `./deploy.sh --recover` (with rebuild) run it for you on a
JetPack 5 radio.

---

## Two failure modes to know

- `skiq_init rc=-16` (busy) after a `docker restart` or `docker rm`: a stale
  card claim was stranded. Stop the service, run B3 again, then start it.
- The radio does not appear in the SDR tab: `ufw` drops the broadcast. Use
  Connect by IP (B5), or open the discovery port once:
  `sudo ufw allow from 192.168.0.0/24 to any port 47811 proto udp`.

A radio is keyed by serial, so its IP can change and the GUI binds to the new
address on its own.

## See also

[G40_BSP_UPGRADE_NOTES.md](G40_BSP_UPGRADE_NOTES.md), [QUICKSTART.md](QUICKSTART.md),
[DEPLOYMENT.md](DEPLOYMENT.md),
[CODEMAP.md](CODEMAP.md)
