#!/bin/bash
# g40_up_r36.sh: bring up the DPMG stack on a JetPack 6 radio (L4T R36.5): the G40
# on Epiq BSP v1.3.0 or the X40 on Epiq Jetson BSP v4.0.0, both libsidekiq 4.25.0.
#   dpmg-gpu : GPU channelizer (CuPy/CUDA 12), --runtime nvidia, shares host IPC
#   dpmg_svc : radio service (Sidekiq via host /usr/lib/epiq), privileged, host net
#
# Difference from JP5: the R36 kernel has CONFIG_RT_GROUP_SCHED off and Docker
# runs cgroup v2, so there is no per-cgroup RT budget to grant: SCHED_FIFO
# threads in the container draw on the global kernel.sched_rt_runtime_us
# (950000). That removes the sudo RT-grant step and --cpu-rt-runtime (which
# Docker rejects on cgroup v2). No sudo needed here.
#
# One-time host prep (sudo), already required for --runtime nvidia:
#   sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
# Build the GPU image on the G40 (context = repo):
#   docker build -f deploy/docker/Dockerfile.gpu.r36 -t dpmg-gpu:r36 .
#
# Run on the G40 (192.168.0.22), from the code folder:
#   bash deploy/radio/g40_up_r36.sh   |   bash deploy/radio/g40_up_r36.sh --stop
#   The kind and serial name come from the hostname; DPMG_KIND=... and
#   DPMG_SERIAL=... override them. On the X40 run it the same way from its code folder.
set -euo pipefail
# the code folder: APP, else the folder this script was run from (deploy/radio/ in it)
APP="${APP:-$(cd "$(dirname "$0")/../.." && pwd)}"
if [ ! -f "$APP/dpmg/radio/dpmg_service.py" ]; then
  echo "$APP does not hold this code layout (no dpmg/radio/dpmg_service.py); set APP to the code folder." >&2
  exit 1
fi
if [ -f "$APP/sdr_core.py" ] && ! grep -q 'kept so old imports' "$APP/sdr_core.py"; then
  echo "$APP still has the old root modules (sdr_core.py ...), which old-style imports would load." >&2
  echo "Remove the old root .py files there (keep dpmg_service.py and run_gui_link.py) or use a new folder." >&2
  exit 1
fi
BROADCAST="${DPMG_BROADCAST:-192.168.0.25}"   # the GUI host to announce to
# this unit's kind and name in the GUI: DPMG_KIND and DPMG_SERIAL, else from the Epiq
# hostname (matchstiq-g40-0001 gives g40 and G40-0001; matchstiq-x40-jetson-0001 gives
# x40 and X40-0001), else g40 and G40-0001
HOST_NAME="$(hostname 2>/dev/null || true)"
case "$HOST_NAME" in
  matchstiq-g40-*)        HOST_KIND=g40; HOST_SERIAL="G40-${HOST_NAME#matchstiq-g40-}" ;;
  matchstiq-x40-jetson-*) HOST_KIND=x40; HOST_SERIAL="X40-${HOST_NAME#matchstiq-x40-jetson-}" ;;
  matchstiq-x40-*)        HOST_KIND=x40; HOST_SERIAL="X40-${HOST_NAME#matchstiq-x40-}" ;;
  *)                      HOST_KIND=""; HOST_SERIAL="" ;;
esac
KIND="${DPMG_KIND:-${HOST_KIND:-g40}}"
SERIAL="${DPMG_SERIAL:-${HOST_SERIAL:-G40-0001}}"

# Stop gracefully: SIGTERM lets dpmg_service tear down and call skiq_exit. A
# SIGKILL (docker rm -f on a running container) leaves the cards registered to
# the dead process in /dev/shm/skiq_card_mgr*, and every libsidekiq user
# (dCFE, Epiq tools, DPMG) then reports "card in use" until the G40 reboots.
stop_c() { docker stop -t 30 "$@" >/dev/null 2>&1 || true; docker rm "$@" >/dev/null 2>&1 || true; }

if [ "${1:-}" = "--stop" ]; then
  stop_c dpmg_svc dpmg-gpu
  echo "stopped dpmg_svc + dpmg-gpu"; exit 0
fi

# Both images and the nvidia runtime must be here before anything is stopped. Without
# this, docker run tries to pull the image from the internet and reports
# "pull access denied", which says nothing about what is missing.
MISSING=""
docker info >/dev/null 2>&1 || { echo "Docker is not installed, not running, or this login may not use it." >&2; exit 1; }
docker image inspect dpmg-gpu:r36 >/dev/null 2>&1 || MISSING="$MISSING dpmg-gpu:r36"
docker image inspect dpmg:latest >/dev/null 2>&1 || MISSING="$MISSING dpmg:latest"
if [ -n "$MISSING" ]; then
  echo "Docker image not found on this radio:$MISSING. The containers were left as they were." >&2
  echo "With no internet on the radio: install from the offline kit (install_g40_offline.sh)." >&2
  echo "With internet on the radio, build here, in $APP:" >&2
  echo "    docker build -f deploy/docker/Dockerfile.gpu.r36 -t dpmg-gpu:r36 ." >&2
  echo "    docker build -t dpmg:latest ." >&2
  echo "The names matter: an image called dpmg-gpu:latest is not found by this script." >&2
  exit 1
fi
if ! docker info 2>/dev/null | grep -i 'runtimes' | grep -qw nvidia; then
  echo "Docker has no nvidia runtime, so dpmg-gpu cannot start. The containers were left as they were." >&2
  echo "Set it up once:  sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker" >&2
  echo "If nvidia-ctk is not there, the NVIDIA container toolkit is missing: install from the offline kit." >&2
  exit 1
fi

# 1) GPU channelizer first (the service's channelizer client attaches to its shm)
stop_c dpmg-gpu
docker run -d --name dpmg-gpu --runtime nvidia --ipc=host \
  --restart unless-stopped dpmg-gpu:r36

# 2) radio service; shares host IPC with the GPU container
stop_c dpmg_svc
docker run -d --name dpmg_svc --restart unless-stopped --network host \
  --privileged --ipc=host -w /app_svc -e PYTHONPYCACHEPREFIX=/tmp/pycache -e DPMG_RX_CPU="${DPMG_RX_CPU:-}" \
  -e DPMG_TX_MODE="${DPMG_TX_MODE:-}" -e DPMG_TX_BLOCK="${DPMG_TX_BLOCK:-}" \
  -e DPMG_TX_ASYNC_PRIO="${DPMG_TX_ASYNC_PRIO:-}" -e DPMG_SECURE_MSG="${DPMG_SECURE_MSG:-}" \
  -v "$APP":/app_svc -v /usr/lib/epiq:/usr/lib/epiq:ro \
  -v /epiq:/epiq:ro -v /usr/bin/hmc_from_file:/usr/bin/hmc_from_file:ro \
  dpmg:latest \
  python dpmg_service.py --kind "$KIND" --host-type "$KIND" --serial "$SERIAL" \
    --cmd-port 47810 --discovery-port 47811 --broadcast "$BROADCAST"

sleep 3
echo "--- result ---"
echo "kind $KIND, serial $SERIAL, announce to $BROADCAST"
echo -n "global sched_rt_runtime_us: "; cat /proc/sys/kernel/sched_rt_runtime_us
docker ps --filter name=dpmg --format '{{.Names}}\t{{.Status}}'
