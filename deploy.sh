#!/usr/bin/env bash
# deploy.sh: one command from the x86: push code to a radio, rebuild + bring up
# its service container, then start the GUI here. Interactive.
#
#   ./deploy.sh              full deploy (back up, push code, rebuild, bring up, GUI)
#   ./deploy.sh --no-push    code is already on the radio and current: skip the
#                            backup/push/rebuild and (re)start only the service +
#                            GUI. Use this over a low-throughput link once the
#                            radio's code is staged, or to re-bring-up after a
#                            power cycle without re-sending ~60 MB.
#   ./deploy.sh --frontend   set up the G40 GPIO front end (external PA enable,
#                            TX/RX switch, PA alarm): asks in plain words, writes +
#                            validates rf_frontend.yaml, installs it and reloads it
#                            live. No push, no rebuild, no container restart, no RF.
#   ./deploy.sh --frontend-config FILE   same, from a ready-made rf_frontend.yaml
#
# What it does, authenticating to the radio only once:
#   1. back up the radio's current code tree (optional)
#   2. push this repo's code to the radio (no libsidekiq is needed in it)
#   3. remove old containers + rebuild the 'dpmg' image. libsidekiq is not baked;
#      the service mounts the radio's own /usr/lib/epiq at runtime instead.
#   4. run the radio's deploy/radio/dpmg_up.sh: it prompts for kind/serial/ports/TX and starts
#      dpmg_svc (with the atomic RT grant if you pick TX)
#   5. (re)start the GUI on this x86 and open it
#
# Needs SSH access to the radio (keys recommended) and docker usable without sudo
# on both boxes (the sidekiq user is normally in the docker group). Only the TX
# path asks for sudo, on the radio, over the interactive session.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && { set -a; . ./.env; set +a; }

ask() { local p="$1" d="$2" a; read -rp "  $p [$d]: " a; printf '%s' "${a:-$d}"; }
yn()  { local p="$1" d="$2" a; read -rp "  $p [$d]: " a; a="${a:-$d}"; [[ "$a" =~ ^[Yy] ]]; }

# options
SKIP_PUSH=no
RECOVER=no
RECOVER_MODE=full          # full | norebuild | restart  (see recovery tiers below)
# --frontend: every argument after it goes to dpmg/payload/frontend_setup.py generate (answers);
# --frontend-config FILE: install a ready-made file. Neither mixes with the others.
FRONTEND=no; FE_FILE=""; FE_ARGS=(); FE_YES=no; OTHER=()
while [ $# -gt 0 ]; do
  case "$1" in
    --frontend) FRONTEND=yes; shift
      for a in "$@"; do if [ "$a" = --yes ] || [ "$a" = -y ]; then FE_YES=yes; else FE_ARGS+=("$a"); fi; done
      set -- ; break ;;
    --frontend-config) FRONTEND=yes; FE_FILE="${2:-}"; [ -n "$FE_FILE" ] || { echo "--frontend-config needs a FILE"; exit 2; }; shift 2 ;;
    --frontend-config=*) FRONTEND=yes; FE_FILE="${1#*=}"; shift ;;
    --yes|-y) FE_YES=yes; OTHER+=("$1"); shift ;;
    *) OTHER+=("$1"); shift ;;
  esac
done
if [ "$FRONTEND" = yes ]; then
  for a in "${OTHER[@]+"${OTHER[@]}"}"; do
    case "$a" in --yes|-y) ;; *) echo "--frontend / --frontend-config cannot be combined with $a"; exit 2 ;; esac
  done
  set --
else
  set -- "${OTHER[@]+"${OTHER[@]}"}"
fi
for arg in "$@"; do
  case "$arg" in
    -n|--no-push|--bringup-only|--local) SKIP_PUSH=yes ;;
    --restart)                    RECOVER=yes; RECOVER_MODE=restart;   SKIP_PUSH=yes ;;
    --recreate|--fix-fast)        RECOVER=yes; RECOVER_MODE=norebuild; SKIP_PUSH=yes ;;
    -r|--recover|--doctor|--fix)  RECOVER=yes; RECOVER_MODE=full;      SKIP_PUSH=yes ;;
    -h|--help)
      cat <<'HELP'
DPMG deploy: bring a radio's service up from the ground station (x86).

USAGE
  ./deploy.sh [OPTION]

OPTIONS
  (no option)        Full deploy: back up the radio's tree, push this repo's
                     code (~0.5 MB; data/recordings excluded), rebuild the 'dpmg'
                     image on the radio, bring up the service, and start the GUI.
                     Use when requirements.txt / Dockerfile (dependencies) changed.
                     On a JetPack 6 radio (G40 on BSP v1.3.0, X40) no image is
                     built: the radio must already have Docker, the nvidia
                     runtime and the images dpmg-gpu:r36 and dpmg:latest. The
                     script checks this first and stops if one is missing. The
                     first install of a G40 is the offline kit, see
                     deploy/offline_kit/README.md.
  -n, --no-push      Code is already on the radio and current: skip the backup,
                     push, and image rebuild; (re)start only the service + GUI.
                     Use over a low-throughput link once code is staged, or to
                     re-bring-up after a power cycle. Leaves dpmg-gpu running.
                     (aliases: --bringup-only, --local)
  Recovery tiers (fastest first; all skip push/backup and then start the GUI):
  --restart          Fastest (seconds): `docker restart` dpmg-gpu + dpmg_svc
                     on the radio. For a hung / wedged service or a stuck card
                     when the containers still exist. No teardown, no rebuild.
  --recreate         Fast (~30 s). Runs deploy/radio/dpmg_doctor.sh --no-rebuild: kill stale
                     processes, remove + re-run the containers from the existing
                     image, clear the /dev/shm card lock + channelizer IPC,
                     re-grant the RT budget, verify. Recovers most broken states
                     without the slow image build. (alias: --fix-fast)
  -r, --recover      Full (~1 min+). Same as --recreate but also rebuilds the
                     'dpmg' image first. Use only when requirements.txt / the
                     Dockerfile changed; otherwise prefer --recreate. Prompts once
                     for sudo on the radio. (aliases: --doctor, --fix)
  -h, --help         Show this help and exit.
  G40 GPIO front end (HPA enable / TX-RX switch / HPA alarm; nothing else runs):
  --frontend [ANSWERS] [--yes]
                     Shows the G40 27-pin GPIO socket table, then asks by socket
                     pin: which pin is wired to the HPA enable (17, 15, or 16 with
                     its direction line PCC.03 set to output), which pin drives the
                     TX/RX switch (15/17/16, or 7-10 = NV100 TR_N FPGA proxy, not
                     yet scoped), which pin carries the HPA alarm (18 or none).
                     Power, ground, SPI/I2C and NV100 GPIO pins are refused, and
                     one pin cannot take two roles. Chip, line name and pad are
                     filled in from the pin. Polarity (does HIGH turn the HPA on,
                     which level selects TX, alarm HIGH or LOW on fault) comes from
                     the datasheet or an HPA/switch preset: empower-1189 (LOW = on,
                     no alarm), empower-1193/1212 (HIGH = on, alarm HIGH),
                     empower-1208/1211 and switch pe71s2041 (polarity not on file:
                     must be entered). Also asks: pull-up level shifter
                     (BOB-12009/BSS138) or none, watchdog ms (500).
                     Writes rf_frontend.yaml, validates it with dpmg/payload/rf_frontend.py on
                     this x86, shows it, asks to confirm, backs up the radio's file
                     (rf_frontend.yaml.bak_<time>), installs it in the code dir
                     (/app_svc in dpmg_svc) and applies it live with
                     bench/rf_frontend_bench.py reload; on a failed reload the
                     backup is restored and reloaded. No push, no rebuild, no
                     container restart, never keys RF. ANSWERS skip questions:
                       --kind none|gpio|mock   --hpa MODEL   --switch MODEL
                       --pa-pin 17|15|16   --switch-pin 15|17|16|7-10
                       --fault-pin 18|none   --fault-level high|low
                       --pa-active-high true|false   --tr-tx-level 1|0
                       --level-shifter pullup|none   --watchdog-ms N
                       --atten none|auto|hidraw[:/dev/hidrawN]|http://ip
                         (RX attenuator; auto = picked up on USB when plugged
                         in, read only on attach; default: keep the radio's
                         current atten section, auto for a first file)
                       --port JACK=ROLE[:CHAIN]  a jack's lockout (tx | rx | txrx | none,
                         CHAIN = plain text for the operator); repeat per jack. Without
                         it the radio's current ports: section is kept
                       --ports-clear  remove the lockout
                       --batch (ask nothing; unanswered = preset/current value)
                     e.g. ./deploy.sh --frontend --hpa empower-1189 --switch pe71s2041 \
                            --pa-pin 17 --switch-pin 15 --tr-tx-level 1 --fault-pin none --batch
                          ./deploy.sh --frontend --port TRx2B=tx:"20 dB pad, HPA, switch" \
                            --port TRx1A=rx --port TRx2A=rx --batch
                     --yes skips the final confirmation. Socket table alone:
                     python3 -m dpmg.payload.frontend_setup pins
  --frontend-config FILE
                     Same install + reload, from a ready-made rf_frontend.yaml
                     (validated the same way first).

WHAT GETS ASKED (interactive)
  radio IP, ssh user, code dir, (backup?), (start GUI?). The radio's deploy/radio/dpmg_up.sh
  then prompts for kind/serial/ports and whether to enable TX (sudo for the RT
  budget). Defaults come from .env (DPMG_G40_HOST, DPMG_RADIO_USER,
  DPMG_RADIO_CODE_DIR) when present.

RELATED COMMANDS
  ./update.sh            Fast code-only update: rsync only the changed files to
                         the radio (delta transfer) + restart dpmg_svc. No image
                         rebuild. Use for iterative code changes on a slow link.
  On the radio, from the code dir:
    bash deploy/radio/dpmg_up.sh      Start/replace the radio service (prompts;
                                      builds image only if missing).
    bash deploy/radio/dpmg_doctor.sh  Full recovery: rebuild image, start the GPU
                                      channelizer + service, re-grant the RT
                                      budget, verify (needs sudo).
    bash deploy/radio/g40_up_r36.sh   G40 on JetPack 6: start dpmg-gpu + dpmg_svc.

NOTES
  * The code is bind-mounted into the container, so code changes do not need an
    image rebuild; only dependency (requirements/Dockerfile) changes do.
  * libsidekiq is not baked; the service mounts the radio's /usr/lib/epiq.
HELP
      exit 0 ;;
    *) echo "unknown option: $arg (try --help)"; exit 2 ;;
  esac
done

if [ "$FRONTEND" = yes ]; then
    echo "=== DPMG front end (--frontend): G40 PA enable / TX-RX switch / PA alarm, written to rf_frontend.yaml ==="
    echo "    (no push, no rebuild, no container restart, no RF: the file is reloaded live)"
elif [ "$RECOVER" = yes ]; then
    case "$RECOVER_MODE" in
      restart)   echo "=== DPMG RECOVER (--restart): restart dpmg-gpu + dpmg_svc on the radio (fastest) ===" ;;
      norebuild) echo "=== DPMG RECOVER (--recreate): dpmg_doctor.sh --no-rebuild (teardown + clear + start, no image build) ===" ;;
      full)      echo "=== DPMG RECOVER (--recover): dpmg_doctor.sh (teardown + rebuild + dpmg-gpu + dpmg_svc) ===" ;;
    esac
elif [ "$SKIP_PUSH" = yes ]; then
    echo "=== DPMG bring-up (--no-push): radio code assumed present + current ==="
else
    echo "=== DPMG deploy: push code, radio service, GUI ==="
fi
RADIO_IP=$(ask "radio IP" "${DPMG_G40_HOST:-}")
[ -n "$RADIO_IP" ] || { echo "no radio address given (type it, or set DPMG_G40_HOST in .env)"; exit 2; }
SSH_USER=$(ask "ssh user on the radio" "${DPMG_RADIO_USER:-sidekiq}")
CODE_DIR=$(ask "code dir on the radio" "${DPMG_RADIO_CODE_DIR:-/home/sidekiq/work/NiceGui_test}")

# G40 GPIO front end: build/validate rf_frontend.yaml here, install + reload it
#     live on the radio (dpmg/payload/frontend_setup.py does the questions/validation/report;
#     bench/rf_frontend_bench.py status|reload are the only radio commands used).
if [ "$FRONTEND" = yes ]; then
    python3 -c 'import yaml' 2>/dev/null || {
        echo "  [XX] python3 with pyyaml is needed on this x86 (pip install pyyaml)"; exit 1; }
    TGT="${SSH_USER}@${RADIO_IP}"
    REMOTE="$CODE_DIR/rf_frontend.yaml"
    FE_TMP=$(mktemp -d /tmp/dpmg_frontend.XXXXXX)
    CTL="/tmp/dpmg_deploy_${RADIO_IP//./_}.sock"
    SSH=(ssh -o ControlMaster=auto -o ControlPath="$CTL" -o ControlPersist=5m)
    trap 'ssh -o ControlPath="$CTL" -O exit "$TGT" 2>/dev/null || true; rm -rf "$FE_TMP"' EXIT
    fe_bench() { DPMG_G40_HOST="$RADIO_IP" python3 bench/rf_frontend_bench.py "$1" </dev/null > "$2" 2>&1 || true; }
    echo "--- connecting to $TGT (authenticate once) ---"
    "${SSH[@]}" "$TGT" true </dev/null

    # the radio's current file: defaults for the questions, its attenuator section is
    # kept, and it is the restore point
    : > "$FE_TMP/current.yaml"
    if "${SSH[@]}" "$TGT" "test -f '$REMOTE'" </dev/null; then
        "${SSH[@]}" "$TGT" "cat '$REMOTE'" </dev/null > "$FE_TMP/current.yaml"
        echo "--- current $REMOTE ---"; sed 's/^/    /' "$FE_TMP/current.yaml"
    else
        echo "  (no $REMOTE yet: the service runs with frontend: none)"
    fi

    echo "--- [radio] frontend_status (read only) ---"
    fe_bench status "$FE_TMP/status.txt"
    if ! grep -q '^radio_state:' "$FE_TMP/status.txt"; then
        sed 's/^/    /' "$FE_TMP/status.txt" | tail -5
        echo "  [XX] dpmg_svc on $RADIO_IP did not answer frontend_status; nothing changed."; exit 1
    fi
    if grep -q '"keyed": true' "$FE_TMP/status.txt"; then
        echo "  [XX] the PA is keyed right now: stop TX / press SAFE first. Nothing changed."; exit 1
    fi
    echo "  [OK] service answers, PA not keyed"

    echo "--- building rf_frontend.yaml ---"
    if [ -n "$FE_FILE" ]; then
        cp "$FE_FILE" "$FE_TMP/new.yaml"
        python3 -m dpmg.payload.frontend_setup check "$FE_TMP/new.yaml" || {
            echo "  [XX] $FE_FILE rejected by rf_frontend.py; nothing changed."; exit 1; }
    else
        python3 -m dpmg.payload.frontend_setup generate --out "$FE_TMP/new.yaml" --current "$FE_TMP/current.yaml" \
            "${FE_ARGS[@]+"${FE_ARGS[@]}"}" || {
            echo "  [XX] no valid config from those answers; nothing changed."; exit 1; }
    fi
    [ -s "$FE_TMP/new.yaml" ] || { echo "  nothing generated; nothing changed."; exit 0; }
    cmp -s "$FE_TMP/new.yaml" "$FE_TMP/current.yaml" && echo "  (identical to the radio's current file)"
    # socket pin 16 needs the direction-line keys (dir_*), which only an up-to-date
    # rf_frontend.py on the radio accepts: refuse rather than install a file it rejects
    if grep -q 'dir_line' "$FE_TMP/new.yaml"; then
        # a radio still on the old layout has no dpmg/payload/rf_frontend.py and
        # fails this check (the safe side)
        R_SHA=$("${SSH[@]}" "$TGT" "sha256sum '$CODE_DIR/dpmg/payload/rf_frontend.py' 2>/dev/null" </dev/null | cut -d' ' -f1)
        L_SHA=$(sha256sum dpmg/payload/rf_frontend.py | cut -d' ' -f1)
        if [ "$R_SHA" != "$L_SHA" ]; then
            echo "  [XX] this config uses socket pin 16 (direction line PCC.03), which needs the"
            echo "       dpmg/payload/rf_frontend.py from this tree on the radio, running (radio's copy differs)."
            echo "       Deploy it first (./update.sh, which restarts dpmg_svc), then re-run. Nothing changed."
            exit 1
        fi
    fi
    if [ "$FE_YES" != yes ]; then
        yn "install this on $RADIO_IP and reload it now? (y/N)" "N" || { echo "aborted; nothing changed."; exit 0; }
    fi

    BK=$("${SSH[@]}" "$TGT" bash -s -- "$CODE_DIR" "$(date +%Y%m%d_%H%M%S)" <<'EOF'
set -e; cd "$1"
if [ -f rf_frontend.yaml ]; then cp -p rf_frontend.yaml "rf_frontend.yaml.bak_$2"; echo "rf_frontend.yaml.bak_$2"; fi
EOF
)
    "${SSH[@]}" "$TGT" "cat > '$REMOTE.new' && mv -f '$REMOTE.new' '$REMOTE'" < "$FE_TMP/new.yaml"
    echo "  installed $REMOTE (= /app_svc/rf_frontend.yaml in dpmg_svc)${BK:+; backup $CODE_DIR/$BK}"

    echo "--- [radio] frontend_reload (PA forced LOW, lines released, new map applied) ---"
    fe_bench reload "$FE_TMP/reload.txt"
    if python3 -m dpmg.payload.frontend_setup report "$FE_TMP/reload.txt" --expect "$FE_TMP/new.yaml"; then
        echo "  [OK] front end configured on $RADIO_IP"; FE_RC=0
    else
        sed 's/^/    /' "$FE_TMP/reload.txt" | tail -4
        echo "--- reload failed: restoring the previous config and reloading it ---"
        if [ -n "$BK" ]; then "${SSH[@]}" "$TGT" "cp -p '$CODE_DIR/$BK' '$REMOTE'" </dev/null
        else "${SSH[@]}" "$TGT" "rm -f '$REMOTE'" </dev/null; fi
        fe_bench reload "$FE_TMP/reload2.txt"
        if [ -s "$FE_TMP/current.yaml" ]; then EXP=(--expect "$FE_TMP/current.yaml"); else EXP=(); fi
        python3 -m dpmg.payload.frontend_setup report "$FE_TMP/reload2.txt" "${EXP[@]+"${EXP[@]}"}" \
            && echo "  [OK] previous config restored and reloaded" \
            || echo "  [XX] the restored config did not reload cleanly either: check docker logs dpmg_svc"
        FE_RC=1
    fi
    echo
    if [ "$FE_RC" = 0 ]; then ACTIVE="$FE_TMP/new.yaml"; else ACTIVE="$FE_TMP/current.yaml"; fi
    python3 -m dpmg.payload.frontend_setup warn "$ACTIVE"
    exit "$FE_RC"
fi

DO_BACKUP=no
if [ "$SKIP_PUSH" != yes ]; then
    DO_BACKUP=yes; yn "back up the radio's old tree first? (Y/n)" "Y" || DO_BACKUP=no
fi
DO_GUI=yes;    yn "start the GUI on this x86 at the end? (Y/n)" "Y" || DO_GUI=no

TGT="${SSH_USER}@${RADIO_IP}"
CODE_BASE=$(basename "$CODE_DIR"); CODE_PARENT=$(dirname "$CODE_DIR")

echo
echo "  radio=$TGT  code=$CODE_DIR  backup=$DO_BACKUP  gui=$DO_GUI"
yn "proceed? (Y/n)" "Y" || { echo "aborted."; exit 0; }

# reuse one ssh connection for every step, so you authenticate once
CTL="/tmp/dpmg_deploy_${RADIO_IP//./_}.sock"
SSH=(ssh -o ControlMaster=auto -o ControlPath="$CTL" -o ControlPersist=5m)
trap 'ssh -o ControlPath="$CTL" -O exit "$TGT" 2>/dev/null || true' EXIT
echo "--- connecting to $TGT (authenticate once) ---"
"${SSH[@]}" "$TGT" true

# 0b. libsidekiq is not baked into the image; the service mounts the radio's own
#     /usr/lib/epiq at runtime. Confirm the radio actually has it, since without it
#     the service can't load the SDK (nothing to mount).
echo "--- checking the radio's libsidekiq ---"
G40_SYS=$("${SSH[@]}" "$TGT" "ls /usr/lib/epiq/libsidekiq.so.* 2>/dev/null | head -1" || true)
G40_VER=${G40_SYS##*libsidekiq.so.}
if [ -z "$G40_VER" ]; then
    echo "  [XX] the radio has no libsidekiq at /usr/lib/epiq: install the Epiq SDK there first."
    echo "       (the service mounts it at runtime; there's nothing to load without it.)"
    exit 1
fi
# 0c. JetPack 6 radio? (cgroup v2: no per-cgroup RT budget, image build needs internet).
#     Then the bring-up is g40_up_r36.sh, announcing to this x86 (or DPMG_GUI_HOST).
RADIO_JP6=no
if "${SSH[@]}" "$TGT" "[ -f /sys/fs/cgroup/cgroup.controllers ]" 2>/dev/null; then RADIO_JP6=yes; fi
JP6_BROADCAST=${DPMG_GUI_HOST:-$(ip route get "${TGT#*@}" 2>/dev/null | grep -o 'src [0-9.]*' | cut -d' ' -f2)}
JP6_BROADCAST=${JP6_BROADCAST:-192.168.0.25}
[ "$RADIO_JP6" = yes ] && echo "  radio runs JetPack 6 (cgroup v2): bring-up via g40_up_r36.sh, announce to $JP6_BROADCAST"
# each G40 announces under its own serial name; a second unit must not reuse another unit's name.
# Epiq names the unit matchstiq-g40-<id> or matchstiq-x40-jetson-<id>, so the kind and the
# default serial come from the radio's hostname
# (check by hand with: ssh <user>@<radio IP> hostname).
RADIO_HOSTNAME=$("${SSH[@]}" "$TGT" hostname 2>/dev/null || true)
case "$RADIO_HOSTNAME" in
    matchstiq-g40-*)        HOST_KIND=g40; HOST_SERIAL="G40-${RADIO_HOSTNAME#matchstiq-g40-}" ;;
    matchstiq-x40-jetson-*) HOST_KIND=x40; HOST_SERIAL="X40-${RADIO_HOSTNAME#matchstiq-x40-jetson-}" ;;
    matchstiq-x40-*)        HOST_KIND=x40; HOST_SERIAL="X40-${RADIO_HOSTNAME#matchstiq-x40-}" ;;
    *)                      HOST_KIND="";  HOST_SERIAL="" ;;
esac
JP6_KIND="${DPMG_KIND:-${HOST_KIND:-g40}}"
JP6_SERIAL="${DPMG_SERIAL:-${HOST_SERIAL:-G40-0001}}"
if [ "$RADIO_JP6" = yes ]; then
    echo "  radio hostname: ${RADIO_HOSTNAME:-unknown}"
    JP6_KIND=$(ask "radio kind (g40 or x40)" "$JP6_KIND")
    JP6_SERIAL=$(ask "radio serial name" "$JP6_SERIAL")
fi
echo "  [OK] radio libsidekiq: $G40_VER; the service will mount it at runtime"

# 0d. JetPack 6: this script does not build the two images there, so they must be on
#     the radio already, with Docker's nvidia runtime set up. Look before anything is
#     touched: stopping the service and then failing to start it leaves the radio down.
if [ "$RADIO_JP6" = yes ] && ! { [ "$RECOVER" = yes ] && [ "$RECOVER_MODE" = restart ]; }; then
    MISSING=$("${SSH[@]}" "$TGT" bash -s <<'EOF' || true
docker info >/dev/null 2>&1 || { echo "Docker (not installed, not running, or this login may not use it)"; exit 0; }
docker image inspect dpmg-gpu:r36 >/dev/null 2>&1 || echo "the image dpmg-gpu:r36"
docker image inspect dpmg:latest >/dev/null 2>&1 || echo "the image dpmg:latest"
docker info 2>/dev/null | grep -i 'runtimes' | grep -qw nvidia || echo "the nvidia runtime in Docker"
EOF
)
    if [ -n "$MISSING" ]; then
        echo "  [XX] the radio is not set up for this software. Missing on $RADIO_IP:"
        printf '%s\n' "$MISSING" | sed 's/^/         /'
        echo "       This script updates a radio that has had its first install. It does not"
        echo "       install Docker or build the images on JetPack 6."
        echo "       First install: use the offline kit (deploy/offline_kit/README.md). From the"
        echo "       kit folder on this machine:"
        echo "           bash copy_kit_to_g40.sh $RADIO_IP"
        echo "       then run the install command it prints. The radio needs no internet for that."
        echo "       A radio that has internet and only lacks the images can build them itself,"
        echo "       in its code folder:"
        echo "           docker build -f deploy/docker/Dockerfile.gpu.r36 -t dpmg-gpu:r36 ."
        echo "           docker build -t dpmg:latest ."
        echo "       Nothing was changed on the radio."
        exit 1
    fi
    echo "  [OK] radio has the images dpmg-gpu:r36 and dpmg:latest and the nvidia runtime"
fi

# 1. back up the radio's old tree (skips epiq_libs + regeneratable dumps)
if [ "$DO_BACKUP" = yes ]; then
    echo "--- [radio] backing up old tree ---"
    "${SSH[@]}" "$TGT" bash -s -- "$CODE_PARENT" "$CODE_BASE" <<'EOF' || echo "  (no old tree; first deploy?)"
set -e; cd "$1"
[ -d "$2" ] || exit 0
tar czf "${2}_backup_$(date +%Y%m%d_%H%M%S).tar.gz" \
  --exclude="${2}/epiq_libs" --exclude='*.iq.bin' --exclude='*.bin' \
  --exclude='*.npy' --exclude='__pycache__' --exclude='*.tar*' \
  --exclude='output' --exclude='*.cdif' --exclude='*.g40iq' --exclude='*.dat' \
  --exclude='*.wav' --exclude='*.png' "$2"
ls -lh "${2}"_backup_*.tar.gz | tail -1
EOF
fi

if [ "$SKIP_PUSH" = yes ]; then
    echo "--- [--no-push] skipping backup/push/rebuild; using the code already on $CODE_DIR ---"
    echo "    (the code is bind-mounted into the container, so no image rebuild is needed"
    echo "     for code changes. dpmg_up.sh will build the image only if it is missing.)"
else
    # 2. push code: no epiq_libs anywhere; the service mounts the radio's own libs at
    #    runtime, so nothing large goes over the wire and no version can mismatch.
    # The modules moved into dpmg/ and this tree no longer has the old root copies.
    # Pushing it over a radio folder that still has them (real code, not the short
    # forwarding files) leaves old code behind that any old-style import would load,
    # so refuse. Deploy into a new code folder instead, or remove the old root .py
    # files there first.
    old_layout_on_radio() {
        [ ! -f sdr_core.py ] || return 1
        "${SSH[@]}" "$TGT" "f='$CODE_DIR/sdr_core.py'; [ -f \"\$f\" ] && ! grep -q 'kept so old imports' \"\$f\"" 2>/dev/null
    }
    if old_layout_on_radio; then
        echo "  [XX] $CODE_DIR on the radio still has the old root modules (sdr_core.py ...)."
        echo "       Push this tree into a new code folder, or remove the old root .py files"
        echo "       there (keep dpmg_service.py and run_gui_link.py), then re-run. Nothing changed."
        exit 1
    fi

    echo "--- [x86 to radio] pushing code ---"
    "${SSH[@]}" "$TGT" "mkdir -p '$CODE_DIR'"
    # push code only: skip generated data + recordings so the transfer stays
    # small (a few MB) on a low-throughput link. IQ/scenario recordings are
    # managed separately on the radio, not shipped with the code.
    # ground-station-only files stay off the radio; JetPack 6 also skips the JetPack 5 scripts
    RADIO_SKIP=(bench tests docs tools deploy/g40 deploy/push_update.sh
                deploy.sh update.sh launch_gui.sh launch_gui.bat launch_gui.ps1 .gitignore
                .env.example requirements-dev.txt)
    [ "$RADIO_JP6" = yes ] && RADIO_SKIP+=(deploy/radio/dpmg_doctor.sh deploy/radio/x40_tx_up.sh
                                           deploy/docker/Dockerfile.gpu)
    SKIP_ARGS=(--exclude='*.md')
    for p in "${RADIO_SKIP[@]}"; do SKIP_ARGS+=(--exclude="./$p"); done
    # data/ is this machine's own generated files (waveforms, captures, recordings);
    # the radio keeps its own there, so pushing ours would overwrite them
    SKIP_ARGS+=(--exclude='./data')
    for d in .[!.]*/; do [ -d "$d" ] && SKIP_ARGS+=(--exclude="./${d%/}"); done
    tar czf - --exclude='epiq_libs' --exclude='.git' --exclude='__pycache__' \
        --exclude='*.iq.bin' --exclude='*.bin' --exclude='*.npy' --exclude='.env' \
        --exclude='output' --exclude='*.cdif' --exclude='*.g40iq' --exclude='*.dat' \
        --exclude='*.wav' --exclude='*.png' --exclude='*.tar*' \
        --exclude='*.docx' --exclude='*.bak*' \
        --exclude='.venv*' --exclude='.pytest_cache' "${SKIP_ARGS[@]}" . \
      | "${SSH[@]}" "$TGT" "tar xzf - -C '$CODE_DIR'"
    echo "  code synced to $CODE_DIR"

    # 3. remove old containers + rebuild the image. libsidekiq is not baked (the
    #    service mounts the radio's /usr/lib/epiq at runtime), so there's nothing to
    #    stage: the build just needs the pushed code.
    # JP6: the radio is offline (the image build needs apt) and the code is
    # bind-mounted, so no rebuild is needed; stop gracefully (SIGTERM releases the
    # cards) instead of rm -f, and g40_up_r36.sh recreates both containers below.
    if [ "$RADIO_JP6" = yes ]; then
        echo "--- [radio] JetPack 6: no image rebuild (code is bind-mounted); stopping containers gracefully ---"
        "${SSH[@]}" "$TGT" "docker stop -t 30 dpmg_svc dpmg-gpu >/dev/null 2>&1; docker rm dpmg_svc dpmg-gpu >/dev/null 2>&1; true"
    else
    echo "--- [radio] removing old containers + rebuilding image (~1 min) ---"
    "${SSH[@]}" "$TGT" bash -s -- "$CODE_DIR" <<'EOF'
set -e; cd "$1"
docker rm -f dpmg dpmg_svc dpmg-gpu 2>/dev/null || true
docker build -t dpmg .
EOF
    fi
fi

# The radio scripts live in deploy/radio/. A radio code folder pushed before that
# move (used with --no-push / --restart / --recreate) still has them at its root.
radio_script() {     # prints remote shell that sets S to the script to run
    printf 'if [ -f deploy/radio/%s ]; then S=deploy/radio/%s; else S=%s; fi' "$1" "$1" "$1"
}

# 4. bring the radio stack up. Normal path uses dpmg_up.sh (prompts + optional TX
#    sudo, starts dpmg_svc). --recover uses dpmg_doctor.sh (full teardown +
#    rebuild + dpmg-gpu + dpmg_svc + RT grant + verify). -t gives a TTY so the
#    prompts / sudo work over ssh.
if [ "$RADIO_JP6" = yes ] && ! { [ "$RECOVER" = yes ] && [ "$RECOVER_MODE" = restart ]; }; then
    # JP6 (cgroup v2: no per-cgroup RT budget; offline image): g40_up_r36.sh is the
    # bring-up for the normal path and for --recreate / --recover. It stops the old
    # containers gracefully and starts dpmg-gpu:r36 + dpmg_svc with TX enabled (no sudo).
    if [ "$RECOVER" = yes ] && [ "$RECOVER_MODE" = full ]; then
        echo "    (JetPack 6: --recover cannot rebuild the image offline; recreating from the existing image)"
    fi
    echo "--- [radio] JetPack 6: starting dpmg-gpu + dpmg_svc with g40_up_r36.sh ---"
    ssh -t -o ControlPath="$CTL" "$TGT" \
      "cd '$CODE_DIR' && $(radio_script g40_up_r36.sh) && DPMG_BROADCAST='$JP6_BROADCAST' DPMG_KIND='$JP6_KIND' DPMG_SERIAL='$JP6_SERIAL' APP='$CODE_DIR' bash \"\$S\""
elif [ "$RECOVER" = yes ]; then
    case "$RECOVER_MODE" in
      restart)
        echo "--- [radio] restarting containers (fast; no teardown/rebuild) ---"
        ssh -t -o ControlPath="$CTL" "$TGT" \
          "docker restart dpmg-gpu dpmg_svc 2>/dev/null; sleep 3; \
           docker ps --filter name=dpmg --format '{{.Names}}  {{.Status}}'; \
           echo '--- last service log ---'; docker logs --tail 4 dpmg_svc 2>&1 | tail -4" ;;
      norebuild)
        echo "--- [radio] recovering without image rebuild (dpmg_doctor.sh --no-rebuild; prompts once for sudo) ---"
        ssh -t -o ControlPath="$CTL" "$TGT" "cd '$CODE_DIR' && $(radio_script dpmg_doctor.sh) && bash \"\$S\" --no-rebuild" ;;
      full)
        echo "--- [radio] recovering the stack (dpmg_doctor.sh; prompts once for sudo) ---"
        ssh -t -o ControlPath="$CTL" "$TGT" "cd '$CODE_DIR' && $(radio_script dpmg_doctor.sh) && bash \"\$S\"" ;;
    esac
else
    echo "--- [radio] starting the service (answer dpmg_up.sh's prompts) ---"
    ssh -t -o ControlPath="$CTL" "$TGT" "cd '$CODE_DIR' && $(radio_script dpmg_up.sh) && bash \"\$S\""
fi

# 5. GUI on this x86
if [ "$DO_GUI" = yes ]; then
    echo "--- [x86] starting the GUI ---"
    docker image inspect dpmg >/dev/null 2>&1 || { echo "  building GUI image..."; docker build -t dpmg .; }
    ./launch_gui.sh
fi

echo
echo "=== done ==="
echo "  Radio $RADIO_IP: dpmg_svc up.   GUI: http://localhost:8080"
echo "  In the GUI: SDR tab, Connect by IP, $RADIO_IP"
