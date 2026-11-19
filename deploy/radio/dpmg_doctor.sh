#!/bin/bash

# options (parsed first, before anything that needs sudo)
# --no-rebuild (aka --fast): skip the docker image rebuild and reuse the image
#   already on the radio. The image only needs rebuilding when requirements.txt
#   or the Dockerfile changed; most broken states (wedged card, stale containers,
#   stuck /dev/shm locks, a dead service) recover without a rebuild, and skipping
#   it cuts the better part of a minute off the recovery. The code is bind-mounted
#   (/app_svc), so code changes are already live without any rebuild.
REBUILD=yes
for arg in "$@"; do
    case "$arg" in
        --no-rebuild|--fast) REBUILD=no ;;
        -h|--help)
            echo "dpmg_doctor.sh [--no-rebuild|--fast]"
            echo "  (default)      full recovery: teardown + clear + rebuild image + start + verify"
            echo "  --no-rebuild   same, but reuse the existing image (no docker build), much faster"
            exit 0 ;;
        *) echo "dpmg_doctor.sh: unknown option '$arg' (try --help)"; exit 2 ;;
    esac
done

# host state that resets on reboot
# RT budget grant (kernel RT_GROUP_SCHED; must precede dpmg_svc creation for TX).
# Order matters: grant the parent cgroup first (nothing can inherit a budget the
# parent doesn't hold), then ensure the docker/ child dir exists (created lazily
# by the daemon when a container starts, so on a clean run it isn't there yet),
# then grant the child. Granting only the child against a missing dir would need
# a second run.
echo 950000 | sudo tee /sys/fs/cgroup/cpu,cpuacct/cpu.rt_runtime_us >/dev/null \
    && echo "[doctor] parent RT budget: 950000us" \
    || echo "[doctor] WARNING: parent RT grant failed" >&2
sudo mkdir -p /sys/fs/cgroup/cpu,cpuacct/docker
echo 900000 | sudo tee /sys/fs/cgroup/cpu,cpuacct/docker/cpu.rt_runtime_us >/dev/null \
    && echo "[doctor] docker RT budget: 900000us" \
    || echo "[doctor] WARNING: docker RT grant failed: SCHED_FIFO TX will not work" >&2
# end DOCTOR_HOST_PREP
# dpmg_doctor.sh: diagnose and recover the DPMG radio stack (split ground station).
#
# Run on the radio's Jetson, from the code dir:  bash deploy/radio/dpmg_doctor.sh
#
# Architecture: the GUI runs on the x86 control host (container 'dpmg', started
# with launch_gui.sh) and is not touched here. This radio runs two containers:
#   dpmg_svc: the headless radio service (dpmg_service.py --kind g40|x40). It
#               bind-mounts the host code at /app_svc, so host files are live in
#               the container: no source copy needed.
#   dpmg-gpu: the GPU channelizer service (CuPy, L4T). Bakes its code, so its
#               two files are copied in after start. Shares POSIX shm + named
#               semaphores with dpmg_svc, so both run --ipc=host.
#
# Config comes from .env / deploy_config defaults (override with DPMG_* env vars):
#   DPMG_KIND (g40|x40), DPMG_SERIAL, DPMG_GUI_HOST (announce target),
#   DPMG_CMD_PORT, DPMG_DISCOVERY_PORT, DPMG_RADIO_CODE_DIR.
#
# Uses sudo for: the RT grant above, killing a root-owned native process, and
# clearing the /dev/shm card lock + stale channelizer IPC. Prompted once.
#
# Order: syntax-check then kill stale then remove old containers then clear shm
#        then rebuild image then start dpmg-gpu then start dpmg_svc (RT) then verify.

set -u
# work from the repo root: .env, the code and the docker build context live there
cd "$(dirname "$0")/../.." || exit 1

[ -f .env ] && { set -a; . ./.env; set +a; }
KIND=${DPMG_KIND:-g40}
case "$KIND" in g40) DEF_SERIAL=G40-0001 ;; x40) DEF_SERIAL=X40-0001 ;; *) DEF_SERIAL=G40-0001 ;; esac
SERIAL=${DPMG_SERIAL:-$DEF_SERIAL}
GUI_HOST=${DPMG_GUI_HOST:-192.168.0.25}
CMD_PORT=${DPMG_CMD_PORT:-47810}
DISC_PORT=${DPMG_DISCOVERY_PORT:-47811}
CODE_DIR=${DPMG_RADIO_CODE_DIR:-/home/sidekiq/work/NiceGui_test}

IMAGE=dpmg
SVC=dpmg_svc
GPU=dpmg-gpu
RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; RST=$'\033[0m'
ok()   { echo "${GRN}[OK]${RST}  $*"; }
warn() { echo "${YEL}[!!]${RST}  $*"; }
fail() { echo "${RED}[XX]${RST}  $*"; }

echo "=== DPMG doctor (radio: $SVC + $GPU, kind=$KIND) ==="

# 0. sudo preflight
if ! sudo -n true 2>/dev/null; then
    warn "this script needs sudo (RT grant, kill root process, clear shm locks)"
    sudo -v || { fail "sudo required - re-run and enter your password"; exit 1; }
fi

# 1. syntax check (radio-side host files)
echo "--- checking Python syntax ---"
# every .py the service can load: the root entry, root modules and the dpmg/
# package (parsed only, so nothing is written into the code folder)
if python3 - <<'PYEOF' 2>/tmp/synerr
import ast, pathlib, sys
files = sorted(pathlib.Path(".").glob("*.py")) + sorted(pathlib.Path("dpmg").rglob("*.py"))
bad = 0
for p in files:
    try:
        ast.parse(p.read_text(encoding="utf-8"), str(p))
    except SyntaxError as e:
        print(f"{p}:{e.lineno}: {e.msg}", file=sys.stderr)
        bad += 1
print(f"{len(files)} files parsed")
sys.exit(1 if bad else 0)
PYEOF
then
    ok "Python syntax"
else
    fail "syntax error(s):"; cat /tmp/synerr
    fail "Fix the syntax error(s) above before continuing."; exit 1
fi

# 2. kill native service processes (incl. root-owned)
echo "--- clearing native service processes that hold the card ---"
if pgrep -f "dpmg_service.py" >/dev/null 2>&1; then
    warn "found native python running the service: killing with sudo"
    sudo pkill -9 -f "dpmg_service.py"; sleep 2
    pgrep -f "dpmg_service.py" >/dev/null 2>&1 \
        && fail "could not kill native process - check 'ps aux | grep dpmg_service'" \
        || ok "native service process killed"
else
    ok "no native service process"
fi

# 3. remove old containers (service + all gpu instances)
echo "--- removing old containers ---"
docker rm -f "$SVC" >/dev/null 2>&1 && ok "removed old service container" || ok "no old service container"
GPU_IDS=$(docker ps -aq --filter "ancestor=$GPU" 2>/dev/null)
if [ -n "$GPU_IDS" ]; then
    echo "$GPU_IDS" | xargs -r docker rm -f >/dev/null 2>&1
    ok "removed old GPU service container(s)"
else
    ok "no old GPU service container"
fi
docker rm -f "$GPU" >/dev/null 2>&1 || true
sleep 3

# 4. clear stale shm: card lock AND channelizer IPC
echo "--- clearing stale /dev/shm locks ---"
if pgrep -f "dpmg_service.py" >/dev/null 2>&1 || \
   docker ps --format '{{.Names}}' | grep -qE "^(${SVC}|${GPU})$"; then
    warn "a holder is still alive: not clearing shm (unsafe). Kill it and re-run."
else
    if ls /dev/shm/skiq_card_mgr* >/dev/null 2>&1; then
        sudo rm -f /dev/shm/skiq_card_mgr* && ok "stale card lock cleared (rc=-16 culprit)" \
            || fail "could not remove card lock"
    else
        ok "no stale card lock"
    fi
    if find /dev/shm -maxdepth 1 \( -name 'dpmg_*' -o -name 'sem.dpmg_*' \) -type f | grep -q .; then
        sudo find /dev/shm -maxdepth 1 \( -name 'dpmg_*' -o -name 'sem.dpmg_*' \) -type f -delete \
            && ok "stale channelizer IPC cleared" || fail "could not remove channelizer IPC"
    else
        ok "no stale channelizer IPC"
    fi
fi

# 5. (re)build the service image
if [ "$REBUILD" = no ] && docker image inspect "$IMAGE" >/dev/null 2>&1; then
    ok "reusing existing service image '$IMAGE' (--no-rebuild; code is bind-mounted so it's already live)"
else
    if [ "$REBUILD" = no ]; then
        warn "service image '$IMAGE' is missing: building it despite --no-rebuild"
    fi
    echo "--- rebuilding service image (this can take a minute) ---"
    if docker build -t "$IMAGE" . >/tmp/buildlog 2>&1; then
        ok "image '$IMAGE' built"
    else
        fail "docker build failed - last lines:"; tail -20 /tmp/buildlog; exit 1
    fi
fi
if ! docker image inspect "$GPU" >/dev/null 2>&1; then
    warn "GPU image '$GPU' not found: building it (large, slow)"
    if [ -f deploy/docker/Dockerfile.gpu ]; then
        docker build -f deploy/docker/Dockerfile.gpu -t "$GPU" . >/tmp/gpubuild 2>&1 \
            && ok "GPU image built" || { fail "GPU build failed:"; tail -20 /tmp/gpubuild; }
    else
        fail "deploy/docker/Dockerfile.gpu missing - cannot build GPU service"
    fi
else
    ok "GPU image present (rebuild manually: docker build -f deploy/docker/Dockerfile.gpu -t $GPU .)"
fi

# 6. start the GPU channelizer service (one instance)
echo "--- starting GPU channelizer service ---"
if docker image inspect "$GPU" >/dev/null 2>&1; then
    docker run -d --name "$GPU" --runtime nvidia --ipc=host \
        "$GPU" python3 -u /app/gpu_service.py >/dev/null 2>&1
    sleep 2
    # GPU image bakes its code: push the two live files in, then restart
    docker cp dpmg/gpu/gpu_channelizer2.py "$GPU":/app/gpu_channelizer2.py \
        && docker cp dpmg/gpu/gpu_service.py "$GPU":/app/gpu_service.py && docker restart "$GPU" >/dev/null \
        && ok "gpu_channelizer2.py + gpu_service.py synced" \
        || warn "GPU file sync failed: service/client may mismatch"
    GPU_READY=0
    for _ in $(seq 1 15); do
        sleep 1
        if docker logs "$GPU" 2>/dev/null | grep -q "ready, waiting for frames"; then GPU_READY=1; break; fi
        docker ps --format '{{.Names}}' | grep -q "^${GPU}$" || break
    done
    [ "$GPU_READY" -eq 1 ] && ok "GPU service ready" || {
        fail "GPU service did not report ready. Last logs:"; docker logs "$GPU" 2>&1 | tail -15
        warn "the service will still start, but channelizer decode won't work until GPU is up"; }
else
    warn "no GPU image: skipping (channelizer decode disabled; RX/PSD/detection still work)"
fi

# 7. start the radio service (bind-mounts host code; RT for TX)
echo "--- starting radio service ($SVC) ---"
# clear a stale card claim one more time (rc=-16 guard), then run
docker run --rm --ipc=host "$IMAGE" bash -c "rm -f /dev/shm/skiq_card_mgr*" >/dev/null 2>&1 || true
docker run -d --name "$SVC" --restart unless-stopped --network host \
    --privileged --ipc=host --cpu-rt-runtime=900000 -w /app_svc -e PYTHONPYCACHEPREFIX=/tmp/pycache \
    -v "$CODE_DIR":/app_svc -v /usr/lib/epiq:/usr/lib/epiq:ro \
    "$IMAGE" \
    python dpmg_service.py --kind "$KIND" --host-type "$KIND" --serial "$SERIAL" \
        --cmd-port "$CMD_PORT" --discovery-port "$DISC_PORT" --broadcast "$GUI_HOST" \
    >/dev/null 2>&1

# 8. verify the service stays up
echo "--- verifying the service stays up ---"
STAYED=1
for _ in 1 2 3 4 5; do
    sleep 2
    docker ps --format '{{.Names}}' | grep -q "^${SVC}$" || { STAYED=0; break; }
done
if [ "$STAYED" -eq 0 ]; then
    fail "service exited after starting. Exit + last logs:"
    docker ps -a --filter "name=$SVC" --format 'exit: {{.Status}}'
    docker logs "$SVC" 2>&1 | tail -25
    exit 1
fi
ok "service container running and stayed up"

# 9. confirm bridge + IPC + announce
echo "--- checking libsidekiq bridge inside the service ---"
BRIDGE=$(docker exec "$SVC" python -c \
    "from dpmg.radio import sdr_core; print(sdr_core._SKIQ_BRIDGE_AVAILABLE)" 2>/tmp/bridgeerr)
[ "$BRIDGE" = "True" ] && ok "bridge available" || { fail "bridge not available:"; cat /tmp/bridgeerr; }

echo "--- checking service <-> GPU IPC namespace ---"
SVC_IPC=$(docker inspect "$SVC" --format '{{.HostConfig.IpcMode}}' 2>/dev/null)
GPU_IPC=$(docker inspect "$GPU" --format '{{.HostConfig.IpcMode}}' 2>/dev/null)
[ "$SVC_IPC" = "host" ] && ok "service --ipc=host" || fail "service IPC mode is '$SVC_IPC' (need host)"
[ -n "$GPU_IPC" ] && { [ "$GPU_IPC" = "host" ] && ok "GPU --ipc=host" || warn "GPU IPC mode is '$GPU_IPC'"; }

echo "--- checking RT budget (needed for TX) ---"
RT=$(cat /sys/fs/cgroup/cpu,cpuacct/docker/cpu.rt_runtime_us 2>/dev/null)
[ "$RT" = "900000" ] && ok "docker RT budget = 900000" || warn "docker RT budget = '${RT:-?}' (TX needs 900000)"

if docker logs "$SVC" 2>&1 | grep -qiE "session|announce|listening"; then
    ok "service is announcing / listening"
else
    warn "did not see session/announce yet; may still be starting (first RFIC cal ~35s)"
fi

# 10. final
SVC_N=$(docker ps --filter "name=$SVC" --format '{{.Names}}' | grep -c "^${SVC}$")
GPU_N=$(docker ps --filter "ancestor=$GPU" --format '{{.Names}}' | wc -l)
[ "$SVC_N" -eq 1 ] && ok "one service container" || warn "expected 1 service, found $SVC_N"
[ "$GPU_N" -le 1 ] && ok "at most one GPU service ($GPU_N running)" \
    || warn "multiple GPU services ($GPU_N): they fight over semaphores. Kill extras."

MYIP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo ""
echo "=== RESULT ==="
ok "Radio stack up: $SVC (+ $GPU), shared IPC, locks cleared, RT granted."
echo "    From the x86 GUI:  SDR tab, Connect by IP, ${MYIP:-<this radio ip>}"
echo "    Tail logs:  docker logs -f $SVC   |   docker logs -f $GPU"
