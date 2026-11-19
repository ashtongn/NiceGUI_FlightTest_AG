#!/usr/bin/env bash
# dpmg_up.sh: guided one-shot bring-up of a DPMG radio service (G40 / X40).
#
# Prompts for the radio kind, serial, the GUI host it announces to, and the UDP
# ports, then runs the whole sequence for you: check/build the image, clear any
# stale card claim, remove the old container, grant the RT budget (only if you
# want TX), start dpmg_svc, and verify it came up. Your answers are saved to
# .env so the next run and the bench/deploy tools reuse them.
#
# Run on the radio's Jetson, from the code folder:   bash deploy/radio/dpmg_up.sh
# For the GUI (x86 control host) just run ./launch_gui.sh: that side is already
# one command. This script replaces the multi-step radio runbook in
# docs/QUICKSTART.md; deploy/radio/g40_up_r36.sh (G40, JetPack 6) / x40_tx_up.sh (X40) remain as
# the fast, non-interactive equivalents once you know your settings.
set -euo pipefail
# work from the repo root: .env and the docker build context live there
cd "$(dirname "$0")/../.."

IMAGE=dpmg
CONTAINER=dpmg_svc

# pull current values from .env (if any) so the prompts default to last time's
[ -f .env ] && { set -a; . ./.env; set +a; }
def_gui=${DPMG_GUI_HOST:-192.168.0.25}
def_cmd=${DPMG_CMD_PORT:-47810}
def_disc=${DPMG_DISCOVERY_PORT:-47811}
def_code=${DPMG_RADIO_CODE_DIR:-/home/sidekiq/work/NiceGui_test}

ask() { local p="$1" d="$2" a; read -rp "  $p [$d]: " a; printf '%s' "${a:-$d}"; }
yn()  { local p="$1" d="$2" a; read -rp "  $p [$d]: " a; a="${a:-$d}"; [[ "$a" =~ ^[Yy] ]]; }
set_env() {                       # upsert KEY=VALUE in .env without clobbering others
    local k="$1" v="$2"; touch .env
    if grep -q "^${k}=" .env; then sed -i "s|^${k}=.*|${k}=${v}|" .env
    else echo "${k}=${v}" >> .env; fi
}

echo "=== DPMG radio bring-up ==="
KIND=$(ask "radio kind (g40/x40)" "${DPMG_KIND:-g40}")
case "$KIND" in
    g40) def_serial=${DPMG_SERIAL:-G40-0001} ;;
    x40) def_serial=${DPMG_SERIAL:-X40-0001} ;;
    *)   echo "  ! unknown kind '$KIND' (expected g40 or x40)"; exit 2 ;;
esac
SERIAL=$(ask   "radio serial (its permanent identity)" "$def_serial")
GUI_HOST=$(ask "GUI host IP to announce to (the x86)"  "$def_gui")
CMD_PORT=$(ask "command UDP port"                      "$def_cmd")
DISC_PORT=$(ask "discovery UDP port"                   "$def_disc")
CODE_DIR=$(ask "code dir on this radio"                "$def_code")
# JetPack 6 (cgroup v2) has no per-cgroup RT budget: TX threads use the global
# kernel budget, so TX needs no grant and no sudo (and --cpu-rt-runtime is rejected).
CGV2=no; [ -f /sys/fs/cgroup/cgroup.controllers ] && CGV2=yes
WANT_TX=no
if [ "$CGV2" = yes ]; then
    yn "enable TX? (JetPack 6: no sudo needed) (Y/n)" "Y" && WANT_TX=yes
else
    yn "enable TX? needs sudo for the RT budget (y/N)" "N" && WANT_TX=yes
fi

echo
echo "  kind=$KIND  serial=$SERIAL  announce to $GUI_HOST  ports=$CMD_PORT/$DISC_PORT  TX=$WANT_TX"
yn "proceed? (Y/n)" "Y" || { echo "  aborted."; exit 0; }

# remember the answers (non-destructive; keeps any other keys already in .env)
set_env DPMG_GUI_HOST "$GUI_HOST"; set_env DPMG_CMD_PORT "$CMD_PORT"
set_env DPMG_DISCOVERY_PORT "$DISC_PORT"; set_env DPMG_RADIO_CODE_DIR "$CODE_DIR"
set_env DPMG_KIND "$KIND"; set_env DPMG_SERIAL "$SERIAL"
echo "  saved settings to .env"

# 1. image
docker image inspect "$IMAGE" >/dev/null 2>&1 || {
    echo "  image '$IMAGE' not found."
    yn "build it now (docker build -t $IMAGE .)? (Y/n)" "Y" \
        && docker build -t "$IMAGE" . \
        || { echo "  need the '$IMAGE' image first; aborting"; exit 1; }
}

# 2. clear a stale card claim + any old container (rc=-16 busy comes from this)
echo "--- clearing stale card claim + old container ---"
docker stop -t 30 "$CONTAINER" >/dev/null 2>&1 || true   # SIGTERM releases the cards
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run --rm --ipc=host "$IMAGE" bash -c "rm -f /dev/shm/skiq_card_mgr*" >/dev/null 2>&1 || true

run_svc() {                       # $1 = extra docker args (RT budget for TX)
    # PYTHONPYCACHEPREFIX: keep Python's __pycache__ in the container's /tmp. The
    # container is root, so a cache in the bind-mounted tree would be root-owned.
    docker run -d --name "$CONTAINER" --restart unless-stopped --network host \
        --privileged --ipc=host -e PYTHONPYCACHEPREFIX=/tmp/pycache $1 -w /app_svc \
        -v "$CODE_DIR":/app_svc -v /usr/lib/epiq:/usr/lib/epiq:ro \
        "$IMAGE" \
        python dpmg_service.py --kind "$KIND" --host-type "$KIND" --serial "$SERIAL" \
            --cmd-port "$CMD_PORT" --discovery-port "$DISC_PORT" --broadcast "$GUI_HOST" >/dev/null
}

# 3. start: with the RT budget granted atomically, only if TX was requested
if [ "$WANT_TX" = yes ] && [ "$CGV2" = yes ]; then
    echo "--- JetPack 6: starting with TX (global RT budget, no grant) ---"
    run_svc ""
    # the GPU channelizer for decode, as g40_up_r36.sh starts it
    if ! docker ps --format '{{.Names}}' | grep -q '^dpmg-gpu$' && docker image inspect dpmg-gpu:r36 >/dev/null 2>&1; then
        docker rm dpmg-gpu >/dev/null 2>&1 || true
        docker run -d --name dpmg-gpu --runtime nvidia --ipc=host --restart unless-stopped dpmg-gpu:r36 >/dev/null \
            && echo "  started dpmg-gpu (dpmg-gpu:r36)"
    fi
elif [ "$WANT_TX" = yes ]; then
    echo "--- granting RT budget (atomic) + starting with TX ---"
    echo 950000 | sudo tee /sys/fs/cgroup/cpu,cpuacct/cpu.rt_runtime_us >/dev/null
    sudo mkdir -p /sys/fs/cgroup/cpu,cpuacct/docker
    echo 900000 | sudo tee /sys/fs/cgroup/cpu,cpuacct/docker/cpu.rt_runtime_us >/dev/null
    run_svc "--cpu-rt-runtime=900000"
else
    echo "--- starting RX-only (no sudo; TX disabled) ---"
    run_svc ""
fi

# 4. verify
sleep 3
echo "--- verify ---"
if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    echo "  [OK] $CONTAINER is up"
else
    echo "  [XX] $CONTAINER did not stay up; last logs:"; docker logs "$CONTAINER" 2>&1 | tail -20; exit 1
fi
if [ "$WANT_TX" = yes ] && [ "$CGV2" = yes ]; then
    echo -n "  global RT budget (sched_rt_runtime_us): "; cat /proc/sys/kernel/sched_rt_runtime_us
elif [ "$WANT_TX" = yes ]; then
    echo -n "  RT budget (want 900000): "; cat /sys/fs/cgroup/cpu,cpuacct/docker/cpu.rt_runtime_us
fi
docker logs "$CONTAINER" 2>&1 | tail -6

MYIP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "=== done ==="
echo "  Service '$CONTAINER' is running on this radio (${MYIP:-<this host>})."
echo "  On the x86 GUI:  SDR tab, Connect by IP, ${MYIP:-<radio ip>}"
echo "  Logs:  docker logs -f $CONTAINER"
