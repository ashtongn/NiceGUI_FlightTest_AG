#!/usr/bin/env bash
# site_deploy.sh: load the flight radio code onto the payload G40 (site deploy).
#
#   bash site_deploy.sh plan     what would be done, with this unit's values; changes nothing
#   bash site_deploy.sh go       do it
#
# Needs `site_read_state.sh before` from the same results folder. Refuses unless the radio
# is closed (state 0) with TX off and the HPA not keyed. In order:
#   1. checks that the recreate keeps the old container's mounts, image, arguments and
#      environment, and that dpmg-gpu runs dpmg-gpu:r36 (stops here otherwise, nothing changed)
#   2. saves the running dpmg_svc as image dpmg:payload-pre-site-<date> (the rollback copy,
#      which keeps smbus2 and anything else installed into that container)
#   3. if dpmg:latest lacks a front end module (gpiod, hid, smbus2) that the container has,
#      adds the wheels from the bundle to dpmg:latest offline (deploy/g40/Dockerfile.frontend-overlay)
#   4. deploy/push_update.sh --g40-only with the deploy set: backups on the radio in
#      ~/work/dpmg_backups/<date>[.n], hash check, docker restart -t 60
#   5. deploy/radio/g40_up_r36.sh with the old serial, kind, broadcast and DPMG_ settings:
#      dpmg-gpu and dpmg_svc recreated, with the /epiq and /usr/bin/hmc_from_file mounts
#   6. site_read_state.sh after, which prints the ok / NOT OK lines
# rf_frontend.yaml, data/ and native/lib on the radio are not touched by any step.
set -euo pipefail
source "$(dirname "$0")/site_env.sh"
MODE="${1:-}"
case "$MODE" in plan|go) ;; *) sed -n 2,20p "$0"; exit 2 ;; esac
B="$RES/before"
[ -f "$B/query.json" ] && [ -f "$B/inspect_dpmg_svc.json" ] || { echo "run site_read_state.sh before first ($B)"; exit 2; }
check_bundle
open_link
T0=$(date +%s)
trap 'say "FAILED at line $LINENO. Rollback: bash site_rollback.sh files (then container if the service does not come up)"' ERR
el() { echo "[$(( ($(date +%s) - T0) / 60 )) min $(( ($(date +%s) - T0) % 60 )) s]"; }
TAG="dpmg:payload-pre-site-$(date +%Y%m%d)"
STATE_ENV="$RES/deploy_state.env"

# 0. the radio must be closed, TX off, HPA not keyed, read now
DPMG_SRC="$SRC" python3 "$KIT/tools/site_query.py" read "$RES/predeploy_query.json" > "$RES/predeploy_query.txt"
python3 - "$RES/predeploy_query.json" <<'EOF' || { echo "radio not closed or TX on: nothing done"; exit 1; }
import json, sys
d = json.load(open(sys.argv[1]))
hb = d.get("heartbeat") or {}
pay = (d.get("payload_status") or {}).get("result") or {}
ok = hb.get("radio_state") == 0 and pay.get("tx_running") is False and (pay.get("pa") or {}).get("keyed") is False
print(f"radio state {hb.get('radio_state')}, tx_running {pay.get('tx_running')}, HPA keyed {(pay.get('pa') or {}).get('keyed')}")
sys.exit(0 if ok else 1)
EOF

# 1. what the recreate would change
$SSH "docker image inspect -f '{{.Id}}' dpmg:latest; docker image inspect -f '{{.Id}}' dpmg-gpu:r36; docker inspect -f '{{.Image}}' dpmg-gpu" \
  > "$RES/image_ids_now.txt" 2>&1 || true
CHECK=$(python3 - "$B/inspect_dpmg_svc.json" "$RES/image_ids_now.txt" "$REMOTE" <<'EOF'
import json, shlex, sys
c = json.load(open(sys.argv[1]))[0]
ids = [l.strip() for l in open(sys.argv[2])]
remote = sys.argv[3]
probs = []
# mounts g40_up_r36.sh gives the new container
known = {f"{remote}:/app_svc", "/usr/lib/epiq:/usr/lib/epiq:ro", "/epiq:/epiq:ro",
         "/usr/bin/hmc_from_file:/usr/bin/hmc_from_file:ro"}
for b in c["HostConfig"].get("Binds") or []:
    if b not in known and b.removesuffix(":rw") not in known:
        probs.append(f"old mount {b} would be lost")
latest = ids[0] if ids and ids[0].startswith("sha256:") else None
if c["Image"] != latest:
    probs.append(f"dpmg_svc runs image {c['Image'][:19]}, dpmg:latest is {str(latest)[:19]}: a recreate would change the image")
if len(ids) < 3 or not ids[1].startswith("sha256:"):
    probs.append("no dpmg-gpu:r36 image on the radio")
elif not ids[2].startswith("sha256:"):
    probs.append("no dpmg-gpu container now: the recreate would start one")
elif ids[1] != ids[2]:
    probs.append("dpmg-gpu runs an image other than dpmg-gpu:r36")
cmd = c["Config"].get("Cmd") or []
args, i = {}, 0
while i < len(cmd):
    if cmd[i].startswith("--"):
        args[cmd[i]] = cmd[i + 1] if i + 1 < len(cmd) and not cmd[i + 1].startswith("--") else ""
        i += 2 if args[cmd[i]] else 1
    else:
        i += 1
for k in args:
    if k not in ("--kind", "--host-type", "--serial", "--cmd-port", "--discovery-port", "--broadcast"):
        probs.append(f"old service argument {k} {args[k]} would be lost")
if args.get("--cmd-port", "47810") != "47810" or args.get("--discovery-port", "47811") != "47811":
    probs.append("old service ports are not 47810 / 47811")
passed = ("DPMG_RX_CPU", "DPMG_TX_MODE", "DPMG_TX_BLOCK", "DPMG_TX_ASYNC_PRIO", "DPMG_SECURE_MSG")
env = []
for e in c["Config"].get("Env") or []:
    k, _, v = e.partition("=")
    if k.startswith("DPMG_"):
        if k in passed:
            if v:
                env.append(f"{k}={shlex.quote(v)}")
        elif v:
            probs.append(f"old environment {k}={v} would be lost")
print("OLD_SERIAL=" + shlex.quote(args.get("--serial", "")))
print("OLD_KIND=" + shlex.quote(args.get("--kind", "g40")))
print("OLD_BROADCAST=" + shlex.quote(args.get("--broadcast", "")))
print("OLD_ENV=" + shlex.quote(" ".join(env)))
print("PROBLEMS=" + shlex.quote("; ".join(probs)))
EOF
)
eval "$CHECK"
UP="cd $REMOTE && $OLD_ENV DPMG_BROADCAST=$OLD_BROADCAST DPMG_KIND=$OLD_KIND DPMG_SERIAL=$OLD_SERIAL APP=$REMOTE bash deploy/radio/g40_up_r36.sh"
NEEDS=$(python3 - "$B/python_modules.txt" <<'EOF'
import sys
t = open(sys.argv[1]).read().splitlines()
cont = next((l for l in t if l.startswith("container:")), "")
img = next((l for l in t if l.startswith("image")), "")
print(" ".join(m for m in ("gpiod", "hid", "smbus2") if f"{m}=yes" in cont and f"{m}=yes" not in img))
EOF
)
echo "== plan for $OLD_SERIAL at $DPMG_G40_HOST, bundle $SITE_BUNDLE"
echo "   save the running container as $TAG"
[ -n "$NEEDS" ] && echo "   dpmg:latest lacks $NEEDS (the container has it): add the bundle's wheels to dpmg:latest" \
                || echo "   dpmg:latest has the front end modules the container has: no image change"
echo "   push_update.sh --g40-only $(deploy_files | tr '\n' ' ')"
echo "   recreate: $UP"
if [ -n "$PROBLEMS" ]; then
  echo "== STOP, the recreate would change more than the code: $PROBLEMS"
  echo "   nothing was changed. Fly on the code on the unit, or decide each point and run again."
  exit 1
fi
[ "$MODE" = plan ] && { echo "== plan only, nothing changed"; exit 0; }

say "deploy start, bundle $SITE_BUNDLE ($(awk '/^commit:/{print $2}' "$BDIR/BUNDLE_INFO.txt"))"
{ echo "START_UTC=$(date -u +%FT%TZ)"; echo "SITE_BUNDLE=$SITE_BUNDLE"; echo "COMMIT_TAG=$TAG"; } > "$STATE_ENV"

# 2. rollback copy of the running container
say "$(el) 2. docker commit dpmg_svc $TAG"
$SSH "docker commit -m 'site deploy rollback copy' dpmg_svc $TAG" | tee -a "$RES/site_log.txt"
echo "OLD_LATEST_ID=$($SSH "docker image inspect -f '{{.Id}}' dpmg:latest")" >> "$STATE_ENV"

# 3. front end modules in the image
if [ -n "$NEEDS" ]; then
  say "$(el) 3. dpmg:latest lacks $NEEDS: overlay build from the bundle's wheels (offline)"
  $SSH "rm -rf /tmp/feov && mkdir -p /tmp/feov"
  scp $DPMG_SSH_OPTS -q "$SRC"/deploy/wheels/*.whl "$SRC/deploy/g40/Dockerfile.frontend-overlay" "$RUSER@$DPMG_G40_HOST:/tmp/feov/"
  $SSH "cd /tmp/feov && mv Dockerfile.frontend-overlay Dockerfile && docker build -q -t dpmg:latest . && docker run --rm dpmg:latest python -c 'import gpiod, hid, smbus2; print(\"modules ok\")'" \
    2>&1 | tee -a "$RES/site_log.txt"
  echo "OVERLAY=yes" >> "$STATE_ENV"
else
  say "$(el) 3. image has the front end modules, no change"
  echo "OVERLAY=no" >> "$STATE_ENV"
fi

# 4. the code
$SSH "ls -1 $RBK 2>/dev/null" > "$RES/backups_before_push.txt" || true
say "$(el) 4. push_update.sh --g40-only (backups, hash check, restart)"
set +e
( cd "$SRC" && DPMG_G40_HOST="$DPMG_G40_HOST" DPMG_SSH_OPTS="$DPMG_SSH_OPTS" bash deploy/push_update.sh --g40-only $(deploy_files) ) 2>&1 | tee "$RES/push_update.txt"
PUSH=${PIPESTATUS[0]}
set -e
$SSH "ls -1 $RBK 2>/dev/null" > "$RES/backups_after_push.txt" || true
BK=$(comm -13 <(sort "$RES/backups_before_push.txt") <(sort "$RES/backups_after_push.txt") | tail -1)
echo "BACKUP_DIR=$RBK/$BK" >> "$STATE_ENV"
say "   backups of the replaced files: $RBK/$BK"
[ "$PUSH" = 0 ] || { say "push_update.sh failed ($PUSH): rollback now, site_rollback.sh files"; exit 1; }

# 5. the container, once, for the two mounts
say "$(el) 5. recreate dpmg-gpu and dpmg_svc"
$SSH "$UP" 2>&1 | tee "$RES/g40_up_r36.txt"
for i in $(seq 1 45); do
  sleep 2
  $SSH "docker logs --since 120s dpmg_svc 2>&1 | grep -q 'up: cmd_port='" && break
done
$SSH "docker logs --since 120s dpmg_svc 2>&1 | grep -E 'up: cmd_port=|Traceback|Error' | tail -5" | sed 's/^/   /' | tee -a "$RES/site_log.txt"

# 6. read back
say "$(el) 6. read back"
bash "$KIT/site_read_state.sh" after
say "$(el) deploy script done; next on the card: the RX-only init from the GUI"
