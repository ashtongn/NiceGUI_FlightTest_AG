# G40 offline bring-up

This gets the DPMG containers running on a G40 with no internet and no machine with
internet beside it. It uses the offline kit (not in this repo): the two container images,
the arm64 Docker and NVIDIA toolkit debs, the dmadriver ring fix (`g40/host/driver_config`)
and the Docker `daemon.json`.

The kit is built on a laptop with internet by `deploy/offline_kit/fetch_kit_online.sh`.
Its `install_g40_offline.sh` does every step on this page in one run, and
`deploy/offline_kit/README.md` is the guide for that. The steps below are the same work
done by hand.

Two cases:
- Case A: the G40 already has Docker and only needs the containers. Start here.
- Case B: a freshly flashed G40 with no Docker. Do B first, then A.

Neither reflashes the BSP; that comes from Epiq (see G40_BSP_UPGRADE_NOTES.md). Below,
`<G40 address>` is the G40's address on your network and the login is sidekiq. Run each
step on the G40 unless it says otherwise.

## Copy the kit and the code over

On the laptop, from the kit folder:

```bash
bash copy_kit_to_g40.sh <G40 address>
```

This copies the G40 parts of the kit to `~/dpmg_offline_kit` on the G40 and checks the
copy against `SHA256SUMS`. Use a USB drive if there is no network; it must be exFAT, NTFS
or ext4, since one image file is over 4 GB.

The service code runs from a code folder on the G40, bind mounted, not from inside the
image. `~/work/NiceGui_test` is the usual folder and the default the bench scripts use
(`DPMG_RADIO_CODE_DIR` on the laptop names another). `deploy/radio/g40_up_r36.sh` runs
from whatever folder it sits in. Unpack the radio code from the kit, on the G40:

```bash
mkdir -p ~/work/NiceGui_test
tar xzf ~/dpmg_offline_kit/code/dpmg_radio_code_*.tar.gz -C ~/work/NiceGui_test
```

## Case B: no Docker yet

Skip to Case A if `docker --version` already works.

```bash
cd ~/dpmg_offline_kit/g40
sudo dpkg -i debs/*.deb          # if it reports config-order errors, run it once more
docker --version                 # docker.io 29.1.3

sudo cp host/daemon.json /etc/docker/daemon.json
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
docker info 2>/dev/null | grep -i runtimes   # should list nvidia
```

The dependency order is runc, containerd, docker.io, then the NVIDIA toolkit. A single
`dpkg -i debs/*.deb` handles it, and a second pass finishes any package left
unconfigured.

## Case A: load the images and bring up

```bash
cd ~/dpmg_offline_kit/g40
zstd -dc images/dpmg_latest.tar.zst  | docker load
zstd -dc images/dpmg-gpu_r36.tar.zst | docker load   # ~5.7 GB, slow
docker images --format '{{.Repository}}:{{.Tag}}' | grep -E 'dpmg:latest|dpmg-gpu:r36'
```

### The dmadriver ring fix

Without the 64k DMA ring, the NV100 RX wedges during TX (see RX_WEDGE_WATCHDOG.md). Put
the config in the Epiq driver folder and reboot, so the driver loads again with it:

```bash
cp host/driver_config ~/sidekiq_image_current/driver/r36.5.0/driver_config
sudo reboot
# after it comes back:
sudo cat /sys/module/dmadriver/parameters/DMADescriptorsPerEngine   # 65536
sudo cat /sys/module/dmadriver/parameters/RingBufferPacketCount     # 16384
```

The file is read only by the Epiq loader at boot (`modprobe --config <file>`), so a plain
`modprobe` reload brings the ring back at the stock size. If you cannot reboot, reload the
module with the sizes given explicitly, as in RX_WEDGE_WATCHDOG.md (this stops the radio
service briefly; skip the first and last lines if the stack is not running yet):

```bash
docker stop -t 30 dpmg_svc
docker run --rm --privileged --pid=host -v /:/host dpmg:latest chroot /host sh -c "rmmod dmadriver; insmod /home/sidekiq/sidekiq_image_current/driver/r36.5.0/5.15.185-tegra/dmadriver.ko DMADescriptorsPerEngine=65536 RingBufferPacketCount=16384"
docker start dpmg_svc
```

Then check the two parameters as above.

### Start the stack

```bash
cd ~/work/NiceGui_test
bash deploy/radio/g40_up_r36.sh
docker ps --filter name=dpmg --format '{{.Names}}\t{{.Status}}'   # dpmg-gpu + dpmg_svc Up
```

The script needs no sudo, because JetPack 6 has no RT budget to grant. It sets
`PYTHONPYCACHEPREFIX=/tmp/pycache`, so nothing writes bytecode into the mounted tree.

### Connect

Open the GUI on the x86 (dpmg container, port 8080) and Connect by IP to `<G40 address>`.
Confirm the live spectrum, and that the detector responds to a signal.

## Troubleshooting

- Containers exit: run `docker ps -a`, then `docker logs dpmg_svc`.
- Cards are "in use" after a hard kill: a SIGKILL strands them in
  `/dev/shm/skiq_card_mgr`. Stop the stack with `bash deploy/radio/g40_up_r36.sh --stop`,
  which sends SIGTERM so the service calls `skiq_exit` cleanly. If the cards are already
  stuck, reboot.
- RX goes flat or stale during a transmit: the ring fix did not take. Check the two
  parameters above, redo the fix and reboot.
