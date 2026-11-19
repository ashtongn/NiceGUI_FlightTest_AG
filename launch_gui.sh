#!/usr/bin/env bash
# launch_gui.sh: bring up the MOTHBALL FURY ground-station GUI on the x86 box.
#
# Default is the Docker deployment (host files are the source of truth;
# this script syncs them into the container):
#
#   ./launch_gui.sh              # deploy the current tree into container 'dpmg',
#                                #   restart it, open http://localhost:8080
#   ./launch_gui.sh --sim        # also start a simulated G40 service on the
#                                #   host so the Ground Link tab has traffic
#   ./launch_gui.sh --native     # no docker: run the GUI natively
#                                #   (8080 if free, else 8081; Ctrl-C stops)
#   ./launch_gui.sh --native --port N
#
# Docker mode details:
#   - The GUI needs UDP broadcast reception for ANNOUNCE discovery, which
#     bridge networking cannot deliver. If the container is not running
#     with --network host, or without the ~/.dpmg mount below, it is
#     recreated (same image, restart policy unless-stopped, output/ of this
#     repo bind-mounted at /app/output).
#   - The mission view keeps its plan autosave and the offline map store under
#     ~/.dpmg (DPMG_MISSION_AUTOSAVE, DPMG_MAP_STORE). The container runs as
#     root, so the host's $HOME/.dpmg is bind-mounted at /root/.dpmg: a plan
#     survives a container restart and the map tiles built on the host
#     (tools/maps/vendor_map_assets.py, tools/maps/build_base_map.py) are
#     served inside.
#   - Code sync is tar-over-exec of all top-level .py, the dpmg/ package
#     mothball_fury.svg and requirements*.txt. requirements.txt is
#     pip-installed inside.
#   - The container is only restarted after the synced tree passes an
#     import self-check inside the container, so a broken sync never takes
#     down a running GUI.
set -euo pipefail
cd "$(dirname "$0")"
REPO="$(pwd)"

CONTAINER=dpmg
IMAGE=dpmg
GUI_URL="http://localhost:8080"

MODE=docker
SIM=0
PORT=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --native) MODE=native; shift ;;
        --docker) MODE=docker; shift ;;
        --sim)    SIM=1; shift ;;
        --port)   PORT="${2:?--port needs a value}"; shift 2 ;;
        *) echo "unknown arg: $1 (use --native / --docker / --sim / --port N)"; exit 2 ;;
    esac
done

port_free() { ! (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

udp_port_free() { ! ss -uln 2>/dev/null | grep -q ":$1 "; }

start_sim() {
    if udp_port_free 47810; then
        nohup python3 dpmg_service.py --simulate --kind mock \
            --serial G40-0001-SIM --host-type g40 \
            --cmd-port 47810 --discovery-port 47811 \
            --broadcast 127.0.0.1 >/tmp/dpmg_sim_service.log 2>&1 &
        echo "$!" >/tmp/dpmg_sim_service.pid
        echo "sim G40 service up (pid $(cat /tmp/dpmg_sim_service.pid);" \
             "stop: kill \$(cat /tmp/dpmg_sim_service.pid))"
    else
        echo "note: port 47810 busy: a radio service is already running, reusing it"
    fi
}

open_when_up() {
    local url="$1"
    for _ in $(seq 1 120); do
        sleep 0.5
        if curl -fs -o /dev/null "$url" 2>/dev/null; then
            xdg-open "$url" 2>/dev/null || echo "open $url in your browser"
            return 0
        fi
    done
    echo "WARNING: GUI did not answer at $url within 60 s"
    return 1
}

# native
if [[ "$MODE" == "native" ]]; then
    python3 -c "import nicegui" 2>/dev/null || {
        echo "ERROR: nicegui not importable (pip install nicegui --break-system-packages)"
        exit 1
    }
    if [[ -z "$PORT" ]]; then
        if port_free 8080; then PORT=8080; else
            PORT=8081
            echo "note: port 8080 busy (docker GUI?), using $PORT"
        fi
    fi
    port_free "$PORT" || { echo "ERROR: port $PORT is busy too"; exit 1; }
    [[ "$SIM" -eq 1 ]] && start_sim
    echo "starting native GUI on http://localhost:$PORT (Ctrl-C to stop)"
    open_when_up "http://localhost:$PORT" &
    exec python3 run_gui_link.py "$PORT"
fi

# docker
docker ps >/dev/null 2>&1 || {
    echo "ERROR: cannot talk to docker (group membership? daemon down?)"
    exit 1
}

wait_for_boot() {
    # never sync while the app is still importing: overwriting a tree that
    # is being imported crashes the boot (it loads half-new, half-old modules)
    for _ in $(seq 1 120); do
        curl -fs -o /dev/null "$GUI_URL" 2>/dev/null && return 0
        sleep 0.5
    done
    echo "WARNING: app not answering at $GUI_URL after 60 s; syncing anyway"
}

# 1. ensure the container exists and runs with host networking and the
#    ~/.dpmg mount (mission plan autosave + map store; see the header)
DPMG_HOME="${DPMG_HOME:-$HOME/.dpmg}"
mkdir -p "$DPMG_HOME"      # create it as this user, not as root via docker

create_container() {
    docker run -d --name "$CONTAINER" --network host \
        --restart unless-stopped \
        -v "$REPO/output":/app/output \
        -v "$DPMG_HOME":/root/.dpmg "$IMAGE" >/dev/null
}

has_dpmg_mount() {
    docker inspect "$CONTAINER" --format '{{range .Mounts}}{{.Destination}} {{end}}' \
        | grep -q ' */root/.dpmg '
}

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    docker image inspect "$IMAGE" >/dev/null 2>&1 || {
        echo "ERROR: no '$CONTAINER' container and no '$IMAGE' image."
        echo "  build one first:  docker build -t $IMAGE ."
        exit 1
    }
    echo "creating container '$CONTAINER' (host networking, output at $REPO/output,"
    echo "  $DPMG_HOME at /root/.dpmg)"
    create_container
elif [[ "$(docker inspect "$CONTAINER" --format '{{.HostConfig.NetworkMode}}')" != "host" ]]; then
    echo "container uses bridge networking: UDP ANNOUNCE discovery cannot"
    echo "reach it. Recreating with --network host (image $IMAGE,"
    echo "output/ now bind-mounted from this repo: $REPO/output)"
    docker rm -f "$CONTAINER" >/dev/null
    create_container
elif ! has_dpmg_mount; then
    echo "container has no $DPMG_HOME mount (mission plans and the map store"
    echo "would be lost on restart). Recreating with it (image $IMAGE)"
    docker rm -f "$CONTAINER" >/dev/null
    create_container
fi

# 2. sync the host tree (source of truth) into /app
wait_for_boot
echo "syncing code into $CONTAINER:/app"
tar cf - --exclude='__pycache__' --exclude='*.bak_*' \
    ./*.py dpmg mothball_fury.svg requirements*.txt payload_profiles.yaml \
    | docker exec -i "$CONTAINER" tar xf - -C /app

# 3. dependencies (idempotent; requirements are pinned where it matters)
echo "installing requirements inside the container"
docker exec "$CONTAINER" pip install -q --root-user-action=ignore \
    -r /app/requirements.txt
# pyshp (zipped shapefiles dropped on the planner's map) is a vendored wheel,
# installed from the local file so this works offline too
for w in deploy/wheels/pyshp-*.whl; do
    [[ -f "$w" ]] || continue
    docker cp "$w" "$CONTAINER:/tmp/$(basename "$w")"
    docker exec "$CONTAINER" pip install -q --no-deps --root-user-action=ignore \
        "/tmp/$(basename "$w")"
done

# 4. the modules now live in dpmg/; the sync never deletes, so remove the old
#    root copies (the image and earlier syncs left them in /app) or a stray old
#    import would quietly load old code. The list is the old paths in moves.tsv.
OLD_ROOT_PY=$(awk -F'\t' '!/^#/ && $2 !~ /\// && $2 ~ /\.py$/ && $2 != "dpmg_service.py" {print $2}' \
    tools/reorg/moves.tsv)
if [[ -n "$OLD_ROOT_PY" ]]; then
    echo "removing old root modules from $CONTAINER:/app"
    # shellcheck disable=SC2086
    docker exec -w /app "$CONTAINER" rm -f $OLD_ROOT_PY
fi

# 5. import self-check before restarting, so a bad sync can't kill the GUI
echo "import self-check"
docker exec -i -w /app "$CONTAINER" python - <<'EOF'
import importlib, importlib.util
for m in ("dpmg.link.proto", "dpmg.link.link_client", "dpmg.web.link_tab",
          "dpmg.mission.mission_tab", "dpmg.web.nicegui_app"):
    importlib.import_module(m)
assert importlib.util.find_spec("sdr_core") is None, "old root sdr_core.py still in /app"
print("imports OK")
EOF

# 6. restart to load the new code, then verify identity
docker restart "$CONTAINER" >/dev/null
echo "container restarted; waiting for $GUI_URL"
open_when_up "$GUI_URL"
title="$(curl -fs "$GUI_URL" | grep -oE '<title>[^<]*</title>' || true)"
echo "serving: ${title:-<no title?>}"

[[ "$SIM" -eq 1 ]] && start_sim

echo "done. GUI: $GUI_URL (container '$CONTAINER', restart=unless-stopped)"
