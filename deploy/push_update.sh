#!/usr/bin/env bash
# push_update.sh: copy changed product files into the running G40 service and the
# x86 GUI container in place, with backups and hash checks, then restart both.
# Containers are kept (docker restart, not recreate). Run from the repo root:
#
#     bash deploy/push_update.sh FILE...                 # both targets
#     bash deploy/push_update.sh --g40-only FILE...
#     bash deploy/push_update.sh --gui-only FILE...
#
# Paths are relative to the repo root (e.g. dpmg/secure/keys.py). Old copies are kept
# outside the code tree: on the G40 in ~/work/dpmg_backups/<date>/<path>, in the GUI
# container in /var/tmp/dpmg_backups/<date>/<path>. A later deploy the same day uses
# <date>.2, .3 ..., so no backup is ever overwritten. Files in hidden folders and *.bak*
# files are refused. DPMG_G40_HOST picks the radio (no default, needed unless --gui-only) and
# DPMG_SSH_OPTS adds ssh options, such as the key for the USB link.
set -euo pipefail
cd "$(dirname "$0")/.."

G40=${DPMG_G40_HOST:-}
RUSER=${DPMG_RADIO_USER:-sidekiq}
REMOTE=${DPMG_RADIO_DIR:-/home/sidekiq/work/NiceGui_test}
SVC=dpmg_svc
GUI=dpmg
TAG=$(date +%Y%m%d)
G40_BK=${DPMG_RADIO_BACKUPS:-/home/sidekiq/work/dpmg_backups}
GUI_BK=/var/tmp/dpmg_backups
DO_G40=1; DO_GUI=1
case "${1:-}" in
  --g40-only) DO_GUI=0; shift ;;
  --gui-only) DO_G40=0; shift ;;
esac
[ $# -gt 0 ] || { sed -n 2,14p "$0"; exit 2; }
FILES="$*"
for f in $FILES; do [ -f "$f" ] || { echo "missing in this tree: $f"; exit 2; }; done
for f in $FILES; do case "$f" in .*/*|*/.*/*|*.bak*) echo "refusing to push $f"; exit 2 ;; esac; done
# copy each existing file to <bk>/<date>[.n]/<path>; the first free <date>[.n] is used
backup_cmd() { echo "for f in $FILES; do [ -f \$f ] || continue; d=$1/$TAG; n=2; while [ -e \$d/\$f ]; do d=$1/$TAG.\$n; n=\$((n+1)); done; mkdir -p \$d/\$(dirname \$f); cp -p \$f \$d/\$f; done; true"; }
short() { awk '{print substr($1,1,8), $2}'; }
echo "== x86 tree"; sha256sum $FILES | short | tee /tmp/push_update_x86.txt

if [ $DO_G40 = 1 ] && [ -z "$G40" ]; then
  echo "set DPMG_G40_HOST to the radio address (or use --gui-only)"; exit 2
fi

if [ $DO_G40 = 1 ]; then
  SSH="ssh ${DPMG_SSH_OPTS:-} -o ConnectTimeout=8 $RUSER@$G40"
  echo "== G40: backups in $G40_BK/$TAG + copy into $REMOTE"
  $SSH "cd $REMOTE && $(backup_cmd "$G40_BK")"
  tar cf - $FILES | $SSH "cd $REMOTE && tar xf -"
  echo "== G40 hashes inside $SVC (must match the x86 tree)"
  $SSH "docker exec -w /app_svc $SVC sha256sum $FILES" | short | diff /tmp/push_update_x86.txt - \
    && echo "   match" || { echo "   HASH MISMATCH on the G40, not restarting"; exit 1; }
  echo "== G40: restart $SVC (container kept)"
  $SSH "docker restart -t 60 $SVC >/dev/null"
  for i in $(seq 1 40); do sleep 1; $SSH "docker logs --since 60s $SVC 2>&1 | grep -q 'up: cmd_port='" && break; done
  $SSH "docker logs --since 60s $SVC 2>&1 | grep -E 'up: cmd_port=|Traceback' | tail -3" | sed 's/^/   /'
  $SSH "docker inspect -f '{{.Id}} {{.State.Status}} restarts={{.RestartCount}}' $SVC" | cut -c1-12,65- | sed 's/^/   /'
fi

if [ $DO_GUI = 1 ]; then
  echo "== GUI container $GUI: backups in $GUI_BK/$TAG + copy into /app"
  docker exec $GUI sh -c "cd /app && $(backup_cmd "$GUI_BK")"
  for f in $FILES; do
    docker exec $GUI mkdir -p "/app/$(dirname "$f")"
    docker cp "$f" "$GUI:/app/$f"
  done
  docker exec $GUI sh -c "cd /app && sha256sum $FILES" | short | diff /tmp/push_update_x86.txt - \
    && echo "   match" || { echo "   HASH MISMATCH in the GUI container, not restarting"; exit 1; }
  echo "== import self-check"
  docker exec -w /app $GUI python3 -c 'import dpmg.link.proto, dpmg.link.link_client, dpmg.web.link_tab, dpmg.web.nicegui_app; print("   import ok")'
  docker restart $GUI >/dev/null; sleep 4
  for p in / /operator /engineering; do printf '   %s -> ' "$p"; curl -s -o /dev/null -w '%{http_code}\n' "http://localhost:8080$p"; done
fi
echo "== done"
