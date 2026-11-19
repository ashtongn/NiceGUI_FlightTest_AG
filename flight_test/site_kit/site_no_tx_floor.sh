#!/usr/bin/env bash
# site_no_tx_floor.sh: take the 13 dB TX attenuation floor off the payload G40.
#
#   bash site_no_tx_floor.sh plan    show the payload line now and what it becomes; changes nothing
#   bash site_no_tx_floor.sh go      do it
#
# The 20 dB pad in front of the HPA is the protection,
# and the G40 must be able to go to 0 dB TX attenuation. The floor comes from the payload line
# of rf_frontend.yaml on the radio: load_power_dbm 47, load_margin_db 3, load_point switch, which
# makes the payload guard refuse any TX attenuation that would put more than 44 dBm on the T/R
# switch (13 dB for P2). This script removes those three keys from that line and nothing else:
# the RX attenuator guard (120 dB while keyed), the port map, the HPA and switch lines and the
# profile stay as they are. Run it with the radio closed and the HPA not keyed.
#
# In order: read the file and check the radio state; keep a dated copy beside it
# (rf_frontend.yaml.before_no_floor_<date>); rewrite the payload line; restart dpmg_svc; read the
# guard back from the service and show that no load limit and no TX floor are left.
# Undo: copy the dated file back over rf_frontend.yaml and restart dpmg_svc.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/site_env.sh"
MODE="${1:-plan}"
REMOTE="${DPMG_RADIO_DIR:-/home/sidekiq/work/NiceGui_test}"
STAMP="$(date +%Y%m%d_%H%M%S)"

echo "== payload line now on $DPMG_G40_HOST"
$SSH "grep -n '^payload:' $REMOTE/rf_frontend.yaml" || { echo "no payload line in rf_frontend.yaml, nothing to change"; exit 1; }

NEW_LINE_PY='
import re, sys
p = sys.argv[1]
t = open(p).read().split("\n")
out = []
for line in t:
    if line.startswith("payload:"):
        for k in ("load_power_dbm", "load_margin_db", "load_point"):
            line = re.sub(r",\s*" + k + r":\s*[^,}]+", "", line)
            line = re.sub(k + r":\s*[^,}]+,\s*", "", line)
    out.append(line)
sys.stdout.write("\n".join(out))
'
echo "== payload line after"
$SSH "python3 -c '$NEW_LINE_PY' $REMOTE/rf_frontend.yaml | grep '^payload:'"

if [ "$MODE" != "go" ]; then echo "== plan only, nothing changed"; exit 0; fi

Q="$(mktemp)"; DPMG_SRC="$SRC" python3 "$HERE/tools/site_query.py" read "$Q" >/dev/null
python3 - "$Q" <<'PY' || { echo "radio is not closed with TX off and the HPA not keyed; stop first"; exit 1; }
import json, sys
d = json.load(open(sys.argv[1]))
hb = d.get("heartbeat") or {}
cfg = (d.get("get_config") or {}).get("result") or {}
fe = (d.get("frontend_status") or {}).get("result") or {}
ok = int(hb.get("radio_state", cfg.get("state", 9))) == 0 and not cfg.get("tx_active") and not fe.get("hpa_keyed")
print("radio state", hb.get("radio_state", cfg.get("state")), "tx_active", cfg.get("tx_active"), "hpa_keyed", fe.get("hpa_keyed"))
sys.exit(0 if ok else 1)
PY

echo "== backup and rewrite"
$SSH "cp -p $REMOTE/rf_frontend.yaml $REMOTE/rf_frontend.yaml.before_no_floor_$STAMP && \
      python3 -c '$NEW_LINE_PY' $REMOTE/rf_frontend.yaml.before_no_floor_$STAMP > $REMOTE/rf_frontend.yaml && \
      grep '^payload:' $REMOTE/rf_frontend.yaml && ls -l $REMOTE/rf_frontend.yaml.before_no_floor_$STAMP"

echo "== restart dpmg_svc"
$SSH "docker restart -t 60 dpmg_svc >/dev/null; for i in \$(seq 1 40); do sleep 1; docker logs --since 60s dpmg_svc 2>&1 | grep -q 'up: cmd_port=' && break; done; docker logs --since 60s dpmg_svc 2>&1 | grep -E 'up: cmd_port=|Traceback|payload' | tail -4"

echo "== guard read back"
DPMG_SRC="$SRC" python3 "$HERE/tools/site_query.py" read "$Q" >/dev/null
python3 - "$Q" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
ps = (d.get("payload_status") or {}).get("result") or {}
load = ps.get("load")
lna = ps.get("lna_min_tx_atten_db")
print("profile", ps.get("profile"), "| load limit", load, "| TX floor", (load or {}).get("min_tx_atten_db"),
      "| LNA floor", lna, "| RX protect", ps.get("protect_db"))
print("ok: no load limit and no TX floor" if not load and lna is None else "NOT OK: a TX floor is still set")
PY
echo "== done. Undo: cp $REMOTE/rf_frontend.yaml.before_no_floor_$STAMP $REMOTE/rf_frontend.yaml, then docker restart dpmg_svc"
