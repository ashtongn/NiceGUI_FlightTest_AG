#!/usr/bin/env bash
# site_read_state.sh: read the payload G40's code, container and front end state, change
# nothing. Run it before the deploy, after it, and after a rollback.
#
#   bash site_read_state.sh before|after|final|rollback
#
# Writes $RES/<phase>/: radio facts, docker inspect of both containers, the Python modules
# the front end needs (container and image), rf_frontend.yaml and its SHA256, the SHA256 of
# every radio-side file compared with the 5 November reference and with the bundle, the
# backups on the radio, the service log tail, the native receive drain (library hash and the
# service's "[rx] drain engine" line), and the service read (site_query.py).
# Every phase but "before" compares with "before" and prints ok / NOT OK lines ("final"
# is the same check as "after", read again at the end into its own folder, and
# also needs the drain engine line to say native, which the service prints at its first RX
# start after a restart).
set -uo pipefail
source "$(dirname "$0")/site_env.sh"
PHASE="${1:-}"
case "$PHASE" in before|after|final|rollback) ;; *) sed -n 2,12p "$0"; exit 2 ;; esac
OUT="$RES/$PHASE"; mkdir -p "$OUT"
check_bundle
open_link
say "read state: $PHASE, bundle $SITE_BUNDLE, radio $DPMG_G40_HOST, results $RES"

$SSH "hostname; echo radio_utc \$(date -u '+%F %T'); uptime; nvpmodel -q 2>/dev/null | head -2; df -h /home | tail -1" \
  > "$OUT/radio_facts.txt" 2>&1
echo "laptop_utc $(date -u '+%F %T')" >> "$OUT/radio_facts.txt"
$SSH "docker ps -a --format '{{.Names}} {{.Image}} {{.Status}}'; docker images --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}'" \
  > "$OUT/docker_ps_images.txt" 2>&1
$SSH "docker inspect dpmg_svc" > "$OUT/inspect_dpmg_svc.json" 2>/dev/null
$SSH "docker inspect dpmg-gpu" > "$OUT/inspect_dpmg_gpu.json" 2>/dev/null
$SSH "docker image inspect -f '{{.Id}}' dpmg:latest dpmg-gpu:r36" > "$OUT/image_ids.txt" 2>&1
MODS="import importlib.util as u; print(' '.join(m + '=' + ('yes' if u.find_spec(m) else 'NO') for m in ('gpiod', 'hid', 'smbus2')))"
{ echo "container: $($SSH "docker exec dpmg_svc python -c \"$MODS\"" 2>&1)"
  echo "image dpmg:latest: $($SSH "docker run --rm dpmg:latest python -c \"$MODS\"" 2>&1)"; } > "$OUT/python_modules.txt"
$SSH "cat $REMOTE/rf_frontend.yaml" > "$OUT/rf_frontend.yaml" 2>/dev/null
$SSH "sha256sum $REMOTE/rf_frontend.yaml; ls -l $REMOTE/rf_frontend.yaml" > "$OUT/rf_frontend.sha256" 2>&1
$SSH "cd $REMOTE && sha256sum $(tr '\n' ' ' < "$BDIR/RADIO_FILES.txt")" > "$OUT/radio_hashes.txt" 2>"$OUT/radio_hashes_missing.txt"
$SSH "ls -1 $RBK 2>/dev/null; ls -1d ~/predeploy_* 2>/dev/null" > "$OUT/radio_backups.txt" 2>&1
$SSH "docker inspect -f 'restarts={{.RestartCount}} started={{.State.StartedAt}} image={{.Image}}' dpmg_svc; docker logs --tail 300 dpmg_svc 2>&1" \
  > "$OUT/svc_log_tail.txt" 2>&1
# the drain engine line of this container run (since its last start) and the last one of any run
$SSH 's=$(docker inspect -f "{{.State.StartedAt}}" dpmg_svc); echo "started $s";
  echo "this run: $(docker logs --since "$s" dpmg_svc 2>&1 | grep -F "[rx] drain engine" | tail -1)";
  echo "any run: $(docker logs dpmg_svc 2>&1 | grep -F "[rx] drain engine" | tail -1)"' \
  > "$OUT/drain_engine.txt" 2>&1

# hashes: radio against the 5 November reference and against the bundle
python3 - "$OUT/radio_hashes.txt" "$BDIR/REFERENCE_PAYLOAD.sha256" "$SRC" "$BDIR/RADIO_FILES.txt" "$BDIR/DEPLOY_SET.txt" > "$OUT/hash_compare.txt" <<'EOF'
import hashlib, os, sys
radio = {l.split()[1]: l.split()[0] for l in open(sys.argv[1]) if len(l.split()) == 2}
ref = {l.split()[1]: l.split()[0] for l in open(sys.argv[2]) if len(l.split()) == 2}
src, files = sys.argv[3], [l.strip() for l in open(sys.argv[4]) if l.strip()]
dep = {l.split()[1] for l in open(sys.argv[5]) if l.strip()}
def h(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest() if os.path.isfile(p) else None
n = {"same as reference": 0, "same as bundle": 0, "other": 0, "missing": 0}
rows = []
for f in files:
    r, b = radio.get(f), h(os.path.join(src, f))
    rel = os.path.relpath(f) if False else f
    if r is None: st = "missing on radio"; n["missing"] += 1
    elif r == b and r == ref.get(f): st = "same as reference and bundle"; n["same as reference"] += 1
    elif r == ref.get(f): st = "same as reference"; n["same as reference"] += 1
    elif r == b: st = "same as bundle"; n["same as bundle"] += 1
    else: st = "OTHER (neither reference nor bundle)"; n["other"] += 1
    rows.append(f"{f:46s} {(r or '-')[:8]:9s} {'deploy' if f in dep else '':7s} {st}")
print(f"radio files {len(files)}: " + ", ".join(f"{k} {v}" for k, v in n.items()))
print("\n".join(rows))
EOF
head -1 "$OUT/hash_compare.txt" | sed 's/^/   /'
grep -E 'OTHER|missing on radio' "$OUT/hash_compare.txt" | sed 's/^/   /'

DPMG_SRC="$SRC" python3 "$KIT/tools/site_query.py" read "$OUT/query.json" | tee "$OUT/query_summary.txt" | sed 's/^/   /'
sed 's/^/   /' "$OUT/python_modules.txt"
echo "   rf_frontend.yaml: $(head -1 "$OUT/rf_frontend.sha256" | cut -c1-16)  $(wc -l < "$OUT/rf_frontend.yaml") lines"
echo "   dpmg_svc $(head -1 "$OUT/svc_log_tail.txt" | cut -c1-60)"
LIBF=native/lib/librxdrain.so
LIB_R=$(awk -v f="$LIBF" '$2==f{print $1}' "$OUT/radio_hashes.txt")
LIB_B=$(awk -v f="$LIBF" '$2==f{print $1}' "$BDIR/SHA256SUMS")
DRAIN=$(sed -n 's/^this run: //p' "$OUT/drain_engine.txt")
LR16=${LIB_R:0:16}; echo "   $LIBF: radio ${LR16:-missing} (bundle ${LIB_B:0:16})"
echo "   drain engine, this run: ${DRAIN:-no line yet (printed at the first RX start)}"
echo "   drain engine, any run:  $(sed -n 's/^any run: //p' "$OUT/drain_engine.txt")"
echo "   dpmg_svc mounts: $(python3 -c "import json,sys; print(', '.join(m['Destination'] for m in json.load(open(sys.argv[1]))[0]['Mounts']))" "$OUT/inspect_dpmg_svc.json" 2>/dev/null)"

if [ "$PHASE" = before ]; then
  TAG="dpmg:payload-pre-site-$(date +%Y%m%d)"
  python3 "$KIT/tools/old_run_cmd.py" "$OUT/inspect_dpmg_svc.json" "$TAG" > "$OUT/old_run_cmd.sh" 2>/dev/null \
    && echo "   rollback run line for the old container (image $TAG): $OUT/old_run_cmd.sh"
  python3 - "$OUT/query.json" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1]))
hb = d.get("heartbeat") or {}
pay = (d.get("payload_status") or {}).get("result") or {}
pa = pay.get("pa") or {}
ok = hb.get("radio_state") == 0 and pay.get("tx_running") is False and pa.get("keyed") is False
print(f"   ready for the deploy: {'yes' if ok else 'NO'} (radio state {hb.get('radio_state')}, "
      f"tx_running {pay.get('tx_running')}, HPA keyed {pa.get('keyed')})")
EOF
else
  B="$RES/before"
  [ -d "$B" ] || { echo "   no $B to compare with"; exit 0; }
  echo "--- $PHASE against before ---"
  [ "$PHASE" = rollback ] && export SITE_EXPECT_CLOCKS=3
  DPMG_SRC="$SRC" python3 "$KIT/tools/site_query.py" compare "$B/query.json" "$OUT/query.json" | sed 's/^/   /'
  a=$(head -1 "$B/rf_frontend.sha256" | cut -d' ' -f1); b=$(head -1 "$OUT/rf_frontend.sha256" | cut -d' ' -f1)
  [ -n "$a" ] && [ "$a" = "$b" ] && echo "   ok      rf_frontend.yaml unchanged ($(echo $a | cut -c1-16))" \
                                 || echo "   NOT OK  rf_frontend.yaml before $a after $b"
  if [ "$PHASE" = after ] || [ "$PHASE" = final ]; then
    bad=0
    while read -r want f; do
      have=$(awk -v f="$f" '$2==f{print $1}' "$OUT/radio_hashes.txt")
      [ "$have" = "$want" ] || { bad=1; echo "   NOT OK  $f on the radio $(echo $have | cut -c1-8), bundle $(echo $want | cut -c1-8)"; }
    done < "$BDIR/DEPLOY_SET.txt"
    [ $bad = 0 ] && echo "   ok      the $(wc -l < "$BDIR/DEPLOY_SET.txt") deploy files match the bundle"
    m=$(python3 -c "import json,sys; print(' '.join(m['Destination'] for m in json.load(open(sys.argv[1]))[0]['Mounts']))" "$OUT/inspect_dpmg_svc.json" 2>/dev/null)
    [ -n "$LIB_R" ] && [ "$LIB_R" = "$LIB_B" ] && echo "   ok      $LIBF matches the bundle (${LIB_R:0:16})" \
                                         || echo "   NOT OK  $LIBF on the radio ${LIB_R:-missing}, bundle ${LIB_B:0:16}"
    case "$DRAIN" in
      *"drain engine: native"*) echo "   ok      drain engine native ($DRAIN)" ;;
      *"drain engine: python"*) echo "   NOT OK  drain engine python, not native ($DRAIN)" ;;
      *) if [ "$PHASE" = final ]; then echo "   NOT OK  no drain engine line since the restart (was RX started in step 6?)"
         else echo "   later   no drain engine line yet; it comes at the first RX start (step 6), checked by final"; fi ;;
    esac
    case " $m " in *" /epiq "*) case " $m " in *" /usr/bin/hmc_from_file "*) echo "   ok      mounts /epiq and /usr/bin/hmc_from_file present";; *) echo "   NOT OK  no /usr/bin/hmc_from_file mount";; esac;; *) echo "   NOT OK  no /epiq mount";; esac
  else
    bad=0
    while read -r want f; do
      ref=$(awk -v f="$f" '$2==f{print $1}' "$BDIR/REFERENCE_PAYLOAD.sha256")
      have=$(awk -v f="$f" '$2==f{print $1}' "$OUT/radio_hashes.txt")
      bh=$(awk -v f="$f" '$2==f{print $1}' "$B/radio_hashes.txt")
      if [ -z "$bh" ] && [ "$f" = "$LIBF" ]; then
        echo "   note    $f was missing before the deploy and is kept (the old code then uses the native drain)"; continue
      fi
      [ "$have" = "$bh" ] || { bad=1; echo "   NOT OK  $f now $(echo $have | cut -c1-8), before the deploy $(echo $bh | cut -c1-8) (reference $(echo $ref | cut -c1-8))"; }
    done < "$BDIR/DEPLOY_SET.txt"
    [ $bad = 0 ] && echo "   ok      the deploy files are back to their before-deploy hashes"
  fi
  grep -q 'smbus2=NO' "$OUT/python_modules.txt" && ! grep -q 'container:.*smbus2=NO' "$B/python_modules.txt" \
    && echo "   NOT OK  smbus2 was in the container before and is not now (HPA monitor ADC)"
  grep -q 'container:.*gpiod=NO' "$OUT/python_modules.txt" && echo "   NOT OK  gpiod missing in the container (HPA enable and T/R lines)"
fi
say "read state $PHASE done: $OUT"
