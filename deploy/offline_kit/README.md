# DPMG offline kit for a Matchstiq G40

Everything needed to put the DPMG radio software on a G40 and the ground station GUI on a
laptop, with no internet on either. The kit is a folder. The scripts that install from it
are kept in the repo under `deploy/offline_kit`, beside this file, and a copy of them goes
into every kit.

The file names, sizes and test notes on this page are those of the kit built on
14 November 2026.

## Building the kit on a laptop with internet

The big files of the kit are not in the repo. `fetch_kit_online.sh` downloads or builds
them. Run it on an x86 Linux laptop with Docker, the Docker buildx plugin, zstd and
python3, from a checkout of the repo:

```bash
bash deploy/offline_kit/fetch_kit_online.sh
```

It makes the folder `DPMG_G40_offline_kit` beside the repo folder (`--out` names another)
and fills it part by part:

| Part | Where it comes from |
|---|---|
| `code` | The repo at the commit that is checked out, packed as the two archives. |
| `host` | `deploy/g40` and `deploy/wheels` in the repo. |
| `debs` | Ubuntu 22.04 arm64 packages for Docker, and NVIDIA's package repo for the container toolkit 1.16.2. |
| `g40image` | `dpmg:latest` for arm64, built from the repo's `Dockerfile`. |
| `gpuimage` | `dpmg-gpu:r36` for arm64, built from `deploy/docker/Dockerfile.gpu.r36` on NVIDIA's l4t-jetpack r36.4.0. About 6 GB to download, and 35 GB of free disk. |
| `guiimage` | The GUI image for the laptop, built from the repo's `Dockerfile` plus `requirements-crypto.txt`. |
| `maps` | The map store, made with `tools/maps`: renderer pieces, world base map, Arizona and Idaho. |

`--only` and `--skip` take a list of parts, `--check` only lists what is there. A part whose
files are already in the folder is left alone, so after a failed download run the script
again. The arm64 parts run under emulation; if Docker on the laptop has none, the script
prints the one command that sets it up.

What differs from the 14 November kit:

- The archive names carry the commit that was checked out, not 70c0120.
- The two G40 images are built new. They are not the images saved from G40-AX35, their ids
  differ, and the Python packages that the Dockerfiles do not pin come at the versions of
  the day.
- The Ubuntu packages in `g40/debs` come at the versions of the day. The script tries them
  in a clean arm64 system with the network off before it keeps them.
- The Epiq card firmware updater is not downloaded. Epiq gives it out through its support
  site; put it in `g40/firmware` by hand.
- The example IQ files and the field cards are not tracked in git. The script takes them
  from the repo folder when they are there, and otherwise makes the IQ files with
  `flight_test/files/make_example_files.py`.

## What is in the kit

| Item | What it is | Size |
|---|---|---|
| `install_g40_offline.sh` | The install script. Runs on the G40. | |
| `copy_kit_to_g40.sh` | Copies the G40 parts of the kit from the laptop to the G40. | |
| `install_laptop_offline.sh` | Loads the GUI image, unpacks the map store and the code on the laptop. | |
| `SHA256SUMS` | Checksums of every file under `code`, `g40` and `laptop`. | |
| `code/NiceGui_flight_70c0120.tar.gz` | The whole NiceGui_flight folder at commit 70c0120: code, docs, tests, flight plans, the example IQ files for the file playback card and the field cards. For the laptop. | 43 MB |
| `code/dpmg_radio_code_70c0120.tar.gz` | The radio part of the same commit, with the example IQ files in `data/waveforms/flight`. The install script unpacks it on the G40. | 19 MB |
| `code/librxdrain_aarch64.so` | The receive drain library, built for the G40. Used only if the build on the G40 fails. | |
| `laptop/dpmg_gui_x86_70c0120.tar.gz` | The ground station GUI image for an x86 laptop, built from commit 70c0120. | 0.6 GB |
| `laptop/dpmg_maps.tar` | Offline map store for the mission view: world base map, Arizona and Idaho in detail. Unpacks to `~/.dpmg/maps`. | 0.7 GB |
| `g40/images/` | The two container images: `dpmg:latest` (radio service) and `dpmg-gpu:r36` (GPU channelizer). | 5.8 GB |
| `g40/debs/` | Docker and the NVIDIA container toolkit as 73 arm64 packages. A fallback: a G40 flashed with BSP v1.3.0 already has both. | 87 MB |
| `g40/host/` | Docker `daemon.json`, the receive ring fix `driver_config`, the attenuator udev rule, the GPS antenna bias service (not installed by the script) and the wheels for the ADC readback. | |
| `g40/firmware/` | Epiq card firmware updater for libsidekiq 4.25 (NV100 FPGA v3.21.0). Run by hand, see below. | 67 MB |

The kit is about 7.7 GB (7.2 GiB). It does not hold the Epiq flash package (see the reflash section).
One file in `g40/images` is 5.7 GB, so a USB drive must be exFAT, NTFS or ext4. FAT32 cannot
hold it.

## What it matches

- G40 on Epiq BSP v1.3.0: JetPack 6, L4T R36.5, libsidekiq 4.25.0.
- Container images with the same ids as G40-AX35 runs on 14 November 2026:
  `dpmg:latest` 9d43b051aa5b and `dpmg-gpu:r36` a93753121839.
- Code: NiceGui_flight commit 70c0120.

## Laptop

The laptop needs Linux on x86 and Docker, usable without sudo. Docker itself is not in the
kit. From the kit folder:

```bash
bash install_laptop_offline.sh
```

This loads the GUI image as `dpmg`, unpacks the map store to `~/.dpmg/maps` and the code to
`~/work/NiceGui_flight` (give another folder as the argument to change that), and starts the
GUI with `./launch_gui.sh`. The GUI is
then at http://localhost:8080. Start with `README.md` and `docs/QUICKSTART.md` in the code
folder. The map store covers the world at low zoom and Arizona and Idaho in detail;
`docs/MISSION_PLANNER.md` says how to add another area.

To start the GUI again later: `cd ~/work/NiceGui_flight && ./launch_gui.sh`.

## G40

The login on the G40 is `sidekiq`. Replace `<G40 address>` with the unit's address.

1. Check that the unit is on JetPack 6. On the G40:

```bash
head -1 /etc/nv_tegra_release
```

It must say R36. If it says R35, the unit needs a reflash first (see the reflash section).

2. Copy the kit to the G40. From the kit folder on the laptop:

```bash
bash copy_kit_to_g40.sh <G40 address>
```

With no network, copy `install_g40_offline.sh`, `SHA256SUMS`, `code` and `g40` to a folder
on the G40 from a USB drive instead.

3. Look first. This changes nothing and lists what an install would do:

```bash
ssh -t sidekiq@<G40 address> 'cd ~/dpmg_offline_kit && sudo bash install_g40_offline.sh --check'
```

4. Install. Give the laptop's address so the G40 announces to it and takes its time from it:

```bash
ssh -t sidekiq@<G40 address> 'cd ~/dpmg_offline_kit && sudo bash install_g40_offline.sh --laptop-ip <laptop address>'
```

Loading the two images takes a few minutes. On a unit that never had the receive ring fix
the script ends with "Restart needed". Restart the G40 (`sudo reboot`) and run the same
command again. The second run takes a few seconds, checks the ring and starts the containers.

5. In the ground station GUI use Connect by IP with the G40's address, port 47810.

The script starts two containers, `dpmg-gpu` and `dpmg_svc`. Both come back by themselves
after a restart of the G40. The radio stays idle until the GUI sets it up.

## What a freshly flashed G40 already has

Epiq's BSP v1.3.0 image comes with Docker 29.1.3, containerd, runc, the NVIDIA container
toolkit 1.16.2, zstd, rsync and gcc, with libsidekiq 4.25.0 in `/usr/lib/epiq` and the clock
tools in `/epiq` and `/usr/bin/hmc_from_file`. The radio code needs nothing else from the
host: everything else it uses is inside the two container images. On top of the image, a
working unit has only what this kit installs: the two images, the code, `driver_config`, the
udev rule, `daemon.json` and the time source file.

## What the script does

Each step looks first and changes only what is missing. Running it again is safe.

1. Checks the unit: G40, JetPack 6, free disk (about 25 GB for a first install).
2. Docker and the NVIDIA container toolkit: installed from `g40/debs` only if missing. Sets
   the nvidia runtime in Docker if it is not set (this restarts Docker).
3. Receive ring fix: copies `driver_config` into the Epiq driver folder. It loads at the
   next boot. Without it the receiver can go stale while the unit transmits.
4. Attenuator udev rule, for a Mini-Circuits RCDAT on USB.
5. Loads the two container images if they are not there.
6. Unpacks the radio code into `~/work/NiceGui_test`. A code folder that is already there is
   left alone unless `--replace-code` is given, which first saves the old code under
   `~/work/dpmg_backups`. An `rf_frontend.yaml` in the folder is never touched.
7. Builds the receive drain library inside the `dpmg` image.
8. Starts the containers with `deploy/radio/g40_up_r36.sh`.

Options, also shown by `bash install_g40_offline.sh --help`:

| Option | Effect |
|---|---|
| `--check` | Look only, change nothing. |
| `--laptop-ip ADDR` | Announce to this laptop and take time from it. Without it the unit announces to 255.255.255.255 and its clock setting is left alone. |
| `--serial NAME` | Name shown in the GUI. The default comes from the hostname: `matchstiq-g40-AX35` gives `G40-AX35`. |
| `--static-ip ADDR/NN` | Add a fixed address on the Ethernet port, for example `192.168.0.23/24`. |
| `--max-power` | MAXN power mode and `jetson_clocks` at boot. |
| `--adc-readback` | Add the smbus2 wheel to the `dpmg` image. Only the I2C ADC switch readback needs it. |
| `--replace-code` | Back up the code folder and install the kit's code over it. |
| `--amp-safe` | See the next section. |
| `--no-start` | Do not start the containers. |
| `--code-dir DIR` | Code folder, default `~/work/NiceGui_test`. |
| `--skip-checksums` | Do not check the kit files against `SHA256SUMS`. |

A log of each run is written to the home folder on the G40 as `dpmg_kit_install_<time>.log`.

## A unit with an amplifier or a switch on its GPIO pins

The kit installs no `rf_frontend.yaml`, so a new unit runs with no amplifier, switch or
attenuator settings. `rf_frontend.example.yaml` and `docs/GPIO_FRONTEND.md` in the repo
describe the file; `./deploy.sh --frontend` on the laptop writes and installs one.

While the service is stopped nothing holds the GPIO pins, and an amplifier wired to them can
switch on. The script therefore refuses `--replace-code` and `--adc-readback` on a running
unit that has an `rf_frontend.yaml`, until `--amp-safe` is added. Put a rated load on the
amplifier output before using it. The same holds for a restart of the G40.

## Card firmware

Epiq pairs libsidekiq 4.25 with NV100 FPGA v3.21.0, and every G40 that has run this software
on BSP v1.3.0 had that version. A unit that shipped with JetPack 5 probably has older
firmware, such as v3.19. The install finishes on such a unit, since the script does not touch
the cards, but the radio has not been run that way: update the cards first. On the G40, with the containers stopped:

```bash
cd ~/work/NiceGui_test && bash deploy/radio/g40_up_r36.sh --stop
sh ~/dpmg_offline_kit/g40/firmware/sidekiq_hardware_updater_for_v4.25.0.sh --list | grep nv100 | grep -v golden
```

If the versions listed differ from what the cards have, update, and do not power off while
it runs:

```bash
sudo sh ~/dpmg_offline_kit/g40/firmware/sidekiq_hardware_updater_for_v4.25.0.sh all
```

Power cycle the G40 afterwards.

## After the install

- `docker ps` on the G40 shows `dpmg-gpu` and `dpmg_svc` as Up.
- `docker logs dpmg_svc | grep "up: cmd_port"` shows the service name and port 47810.
- `sudo cat /sys/module/dmadriver/parameters/DMADescriptorsPerEngine` reads 65536 after the
  restart.
- After the first receive, `docker logs dpmg_svc | grep "drain engine"` reads
  `[rx] drain engine: native`.
- External 10 MHz and 1 PPS: `docs/BRINGUP.md` and `docs/RADIO_ROLE_AND_FPGA.md`. The kit
  changes no clock setting on the unit.
- GPS antenna bias at boot: `g40/host/epiq-gps-ant-bias.service`, with the install steps in
  its header. Install it only for a known active antenna.

Later code updates go from the laptop with `deploy/push_update.sh` or `./deploy.sh`, as on
any other G40. Neither of those installs Docker, the images or the ring fix; this kit does.

## Reflash, only for a G40 still on JetPack 5

The flash package is not in this kit: it is about 10.3 GB. It is Epiq's BSP v1.3.0 for the
G20 and G40, file `g20g40_61_g212ad9e-release_v1.3.0-0-g212ad9e_2026-08-31T1743_flash.tar.xz`,
sha256 starting b327bbc6a43d125a, from Epiq, with Epiq's README for the flash steps. The
flash runs from an x86 Ubuntu laptop with Docker, with the G40 on USB-C in recovery mode,
and needs Epiq's two flash containers (`docker.epiq.rocks/jetson-linux-bsp-flash:l4t-R36-5-0-v6`
and `docker.epiq.rocks/epiq-nfs-ganesha:v2`).

The extra steps we needed are in `docs/G40_BSP_UPGRADE_NOTES.md` in the repo (fixed address,
the NFS firewall rule), and `deploy/g40/guarded_flash.sh` is the wrapper that refuses to flash
when more than one Jetson is on USB. Disconnect every other Jetson from the laptop's USB
before flashing. After the flash, do the card firmware step above, then the G40 steps from
the top.

## What has been run and what has not

- G40 install, 15 November 2026 (UTC), on G40-AX35 with its DPMG code folder moved away and
  its two image tags removed first, so the unit looked like one that never had DPMG:
  `copy_kit_to_g40.sh`, `--check`, then the install with `--laptop-ip`. It loaded both images,
  unpacked the code, built the receive drain library, installed the udev rule, set the time
  source and started the containers, in about two minutes. The service came up idle, the GUI
  connected and the G40 clock synchronized to the laptop.
- That unit already had Docker, the NVIDIA toolkit and the ring fix, as a freshly flashed
  G40 on BSP v1.3.0 does. The install from `g40/debs` has not run on a G40. It was run in a
  bare ARM64 Ubuntu 22.04 system with networking switched off, with the script's own
  commands: all 73 packages installed and docker and nvidia-ctk ran.
- No receive or transmit was run after that install. The radio code is commit 70c0120;
  G40-AX35 ran the 14 November bench tests with an older copy. Three radio files differ in
  code between the two: `dpmg/dsp/decode_pool.py` and `dpmg/framing/frame_codec.py` (the
  9 November uplink decode change) and `dpmg/scenario/gen_scenario_json.py` (peak limit 1.0).
- Laptop: the GUI image was started with networking switched off and served the operator
  and mission pages; `install_laptop_offline.sh` was run without the start step. A first
  `./launch_gui.sh` on a laptop with no internet has not been run.
- The other script paths (restart needed, repeat run, code replacement, damaged kit file,
  unit on JetPack 5, the amplifier check) were run against a stand in on the laptop.
