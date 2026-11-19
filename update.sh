#!/usr/bin/env bash
# update.sh: fast code-only update to a radio over a low-throughput link.
# rsync delta-transfers only the changed source files (generated data + IQ
# recordings excluded) to the radio's code dir, then restarts the service so the
# bind-mounted code reloads. No full push, no image rebuild.
#
#   ./update.sh                 sync changed code + restart dpmg_svc
#   ./update.sh --restart-gpu   also restart the GPU channelizer (dpmg-gpu)
#   ./update.sh --no-restart    sync only; don't restart anything
#   ./update.sh --help
#
# Use deploy.sh instead for first install or when requirements.txt / Dockerfile
# (dependencies) change. Needs rsync on both ends and docker usable without sudo
# on the radio.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && { set -a; . ./.env; set +a; }

ask() { local p="$1" d="$2" a; read -rp "  $p [$d]: " a; printf '%s' "${a:-$d}"; }

RESTART_SVC=yes; RESTART_GPU=no
for arg in "$@"; do
  case "$arg" in
    --restart-gpu) RESTART_GPU=yes ;;
    --no-restart)  RESTART_SVC=no ;;
    -h|--help)
      cat <<'HELP'
update.sh: fast code-only update to a radio (low-throughput friendly).

USAGE
  ./update.sh [OPTIONS]

WHAT IT DOES
  rsync delta-transfers only changed source files to the radio's code dir
  (generated data, IQ/scenario recordings, Word/docs output excluded), then
  restarts the radio service so the bind-mounted code reloads. No image rebuild,
  no ~60 MB full push; typically a few KB on the wire for a small change.

OPTIONS
  --restart-gpu   also restart the GPU channelizer (dpmg-gpu) after the sync
  --no-restart    sync only; don't restart any container
  -h, --help      show this help

Defaults come from .env (DPMG_G40_HOST, DPMG_RADIO_USER, DPMG_RADIO_CODE_DIR).

Use deploy.sh instead when
  first install, or requirements.txt / Dockerfile (dependencies) changed.
HELP
      exit 0 ;;
    *) echo "unknown option: $arg (try --help)"; exit 2 ;;
  esac
done

echo "=== DPMG update: rsync code to the radio, restart service ==="
RADIO_IP=$(ask "radio IP" "${DPMG_G40_HOST:-}")
[ -n "$RADIO_IP" ] || { echo "no radio address given (type it, or set DPMG_G40_HOST in .env)"; exit 2; }
SSH_USER=$(ask "ssh user on the radio" "${DPMG_RADIO_USER:-sidekiq}")
CODE_DIR=$(ask "code dir on the radio" "${DPMG_RADIO_CODE_DIR:-/home/sidekiq/work/NiceGui_test}")
TGT="${SSH_USER}@${RADIO_IP}"

# one shared ssh connection, so you authenticate once
CTL="/tmp/dpmg_update_${RADIO_IP//./_}.sock"
SSH_OPTS="-o ControlMaster=auto -o ControlPath=$CTL -o ControlPersist=5m"
trap 'ssh -o ControlPath="$CTL" -O exit "$TGT" 2>/dev/null || true' EXIT
ssh $SSH_OPTS "$TGT" true

# The modules moved into dpmg/ and this tree no longer has the old root copies.
# Pushing it over a radio folder that still has them (real code, not the short
# forwarding files) leaves old code behind that any old-style import would load,
# so refuse. Deploy into a new code folder instead, or remove the old root .py
# files there first.
old_layout_on_radio() {
    [ ! -f sdr_core.py ] || return 1
    ssh $SSH_OPTS "$TGT" "f='$CODE_DIR/sdr_core.py'; [ -f \"\$f\" ] && ! grep -q 'kept so old imports' \"\$f\"" 2>/dev/null
}
if old_layout_on_radio; then
    echo "  [XX] $CODE_DIR on the radio still has the old root modules (sdr_core.py ...)."
    echo "       Push this tree into a new code folder, or remove the old root .py files"
    echo "       there (keep dpmg_service.py and run_gui_link.py), then re-run. Nothing changed."
    exit 1
fi

# Ground-station-only files (tests, bench, docs, tools, x86 launch/deploy scripts)
# stay off the radio; a JetPack 6 radio also skips the JetPack 5 scripts.
RADIO_SKIP=(bench tests docs tools deploy/g40 deploy/push_update.sh
            deploy.sh update.sh launch_gui.sh launch_gui.bat launch_gui.ps1 .gitignore
            .env.example requirements-dev.txt)
if ssh $SSH_OPTS "$TGT" "[ -f /sys/fs/cgroup/cgroup.controllers ]" 2>/dev/null; then
    RADIO_SKIP+=(deploy/radio/dpmg_doctor.sh deploy/radio/x40_tx_up.sh deploy/docker/Dockerfile.gpu)
fi
SKIP_ARGS=(--exclude='*.md')
for p in "${RADIO_SKIP[@]}"; do SKIP_ARGS+=(--exclude="/$p"); done
# data/ is this machine's own generated files (waveforms, captures, recordings);
# the radio keeps its own there, so syncing ours would overwrite them
SKIP_ARGS+=(--exclude='/data')

# rsync code only (same exclusions as deploy's trimmed push). No --delete: it
# never removes the radio's own data (iq/, waveforms/, logs): it only updates
# and adds changed source files.
echo "--- rsync code to $TGT:$CODE_DIR (changed files only) ---"
rsync -az --itemize-changes -e "ssh $SSH_OPTS" \
  --exclude='epiq_libs' --exclude='.git' --exclude='__pycache__' \
  --exclude='*.iq.bin' --exclude='*.bin' --exclude='*.npy' --exclude='.env' \
  --exclude='output' --exclude='*.cdif' --exclude='*.g40iq' --exclude='*.dat' \
  --exclude='*.wav' --exclude='*.png' --exclude='*.tar*' \
  --exclude='*.docx' --exclude='/.*/' --exclude='*.bak*' \
  --exclude='.venv*' --exclude='.pytest_cache' "${SKIP_ARGS[@]}" \
  ./ "$TGT:$CODE_DIR/"

if [ "$RESTART_SVC" = yes ]; then
  echo "--- restarting dpmg_svc (reload bind-mounted code) ---"
  ssh $SSH_OPTS "$TGT" "docker restart dpmg_svc >/dev/null && echo '  dpmg_svc restarted'"
fi
if [ "$RESTART_GPU" = yes ]; then
  echo "--- restarting dpmg-gpu ---"
  ssh $SSH_OPTS "$TGT" "docker restart dpmg-gpu >/dev/null && echo '  dpmg-gpu restarted'"
fi

echo "=== update done ==="
