#!/usr/bin/env bash
# site_rollback.sh: put the payload G40 back on the code it ran before the site deploy.
#
#   bash site_rollback.sh files       the deploy files back from the radio's backups, then
#                                     docker restart -t 60 dpmg_svc (about 2 minutes). The
#                                     container keeps the two extra read-only mounts, which
#                                     the old code does not use.
#   bash site_rollback.sh container   files as above, then dpmg_svc made again from the image
#                                     saved before the deploy (dpmg:payload-pre-site-<date>) with
#                                     the old container's mounts, arguments and environment
#                                     (before/old_run_cmd.sh); for a service that does not come
#                                     up in the new container (about 2 more minutes)
#
# The backups come from deploy_state.env (BACKUP_DIR, written by site_deploy.sh); without it
# the newest ~/work/dpmg_backups/<today>* folder is used and named on the screen. Ends with
# site_read_state.sh rollback, which compares with the before read.
set -euo pipefail
source "$(dirname "$0")/site_env.sh"
MODE="${1:-}"
case "$MODE" in files|container) ;; *) sed -n 2,17p "$0"; exit 2 ;; esac
mkdir -p "$RES"
check_bundle
open_link
STATE_ENV="$RES/deploy_state.env"
BACKUP_DIR=""; COMMIT_TAG=""; OVERLAY=no; OLD_LATEST_ID=""
[ -f "$STATE_ENV" ] && source "$STATE_ENV"
if [ -z "$BACKUP_DIR" ] || [ "$BACKUP_DIR" = "$RBK/" ]; then
  BACKUP_DIR=$($SSH "ls -1d $RBK/$(date +%Y%m%d)* 2>/dev/null | sort -V | tail -1")
  say "no BACKUP_DIR recorded; using the newest of today: $BACKUP_DIR"
fi
[ -n "$BACKUP_DIR" ] || { say "no backup folder found on the radio: see ROLLBACK.md, by hand"; exit 1; }
say "rollback ($MODE) from $BACKUP_DIR"

for f in $(deploy_files); do
  $SSH "if [ -f $BACKUP_DIR/$f ]; then cp -p $BACKUP_DIR/$f $REMOTE/$f && echo restored $f; else echo no backup of $f, left as it is; fi" \
    | sed 's/^/   /' | tee -a "$RES/site_log.txt"
done

if [ "$MODE" = files ]; then
  say "docker restart -t 60 dpmg_svc"
  $SSH "docker restart -t 60 dpmg_svc >/dev/null"
else
  [ -f "$RES/before/old_run_cmd.sh" ] || { say "no before/old_run_cmd.sh: see ROLLBACK.md, by hand"; exit 1; }
  IMG=$(awk '{for(i=1;i<=NF;i++) if ($i ~ /^dpmg:payload-pre-site-/) print $i}' "$RES/before/old_run_cmd.sh" | head -1)
  $SSH "docker image inspect $IMG >/dev/null" || { say "image $IMG not on the radio: see ROLLBACK.md"; exit 1; }
  if [ "$OVERLAY" = yes ] && [ -n "$OLD_LATEST_ID" ]; then
    say "dpmg:latest back to $OLD_LATEST_ID (the image before the overlay)"
    $SSH "docker tag $OLD_LATEST_ID dpmg:latest"
  fi
  say "dpmg_svc made again from $IMG with the old settings"
  $SSH "docker stop -t 30 dpmg_svc >/dev/null 2>&1; docker rm dpmg_svc >/dev/null 2>&1; true"
  $SSH "bash -s" < "$RES/before/old_run_cmd.sh" | sed 's/^/   /'
fi
for i in $(seq 1 45); do
  sleep 2
  $SSH "docker logs --since 120s dpmg_svc 2>&1 | grep -q 'up: cmd_port='" && break
done
$SSH "docker logs --since 120s dpmg_svc 2>&1 | grep -E 'up: cmd_port=|Traceback' | tail -3" | sed 's/^/   /' | tee -a "$RES/site_log.txt"
bash "$KIT/site_read_state.sh" rollback
