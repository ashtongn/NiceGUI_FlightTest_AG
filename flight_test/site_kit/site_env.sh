# site_env.sh: settings shared by the site kit scripts (sourced, not run).
#
# DPMG_G40_HOST    payload G40 address, default 192.168.55.1 (USB-C)
# DPMG_SSH_KNOWN   known hosts file for that address, default ~/.ssh/known_hosts_usb_g40
#                  (the file the 5 November deploy to the payload G40 used; every G40 and Jetson
#                  answers on 192.168.55.1 with its own host key, so each unit has its own file)
# DPMG_SSH_KEY     optional key file; without it the operator types the password once
# SITE_BUNDLE      bundle id under bundle/, default the one in bundle/CURRENT
# SITE_RESULTS     results folder, default ~/dpmg_site_results/<date>_site
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WF="$(dirname "$KIT")"
export DPMG_G40_HOST="${DPMG_G40_HOST:-192.168.55.1}"
RUSER=sidekiq
REMOTE=/home/sidekiq/work/NiceGui_test
RBK=/home/sidekiq/work/dpmg_backups
SITE_BUNDLE="${SITE_BUNDLE:-$(cat "$KIT/bundle/CURRENT" 2>/dev/null || true)}"
BDIR="$KIT/bundle/$SITE_BUNDLE"
ARCHIVE="$BDIR/dpmg_flight_$SITE_BUNDLE.tar.gz"
RES="${SITE_RESULTS:-$HOME/dpmg_site_results/$(date +%F)_site}"
SRC="$RES/src/dpmg_flight_$SITE_BUNDLE"
# one ssh login for the whole card: every later ssh, scp and push_update.sh call reuses it
CM="$HOME/.ssh/cm-site-%r@%h"
DPMG_SSH_OPTS="-o ControlMaster=auto -o ControlPath=$CM -o ControlPersist=60m -o ServerAliveInterval=10"
DPMG_SSH_OPTS+=" -o UserKnownHostsFile=${DPMG_SSH_KNOWN:-$HOME/.ssh/known_hosts_usb_g40}"
[ -n "${DPMG_SSH_KEY:-}" ] && DPMG_SSH_OPTS+=" -i $DPMG_SSH_KEY -o IdentitiesOnly=yes"
export DPMG_SSH_OPTS
SSH="ssh $DPMG_SSH_OPTS -o ConnectTimeout=8 $RUSER@$DPMG_G40_HOST"

say() { echo "$(date -u '+%H:%M:%S') $*" | tee -a "$RES/site_log.txt"; }

open_link() {
  mkdir -p "$RES"
  if ! ssh $DPMG_SSH_OPTS -O check "$RUSER@$DPMG_G40_HOST" 2>/dev/null; then
    echo "--- logging in to $RUSER@$DPMG_G40_HOST once (the password goes here) ---"
    ssh $DPMG_SSH_OPTS -o ConnectTimeout=8 -fN "$RUSER@$DPMG_G40_HOST"
  fi
}

check_bundle() {
  [ -f "$ARCHIVE" ] || { echo "no bundle archive $ARCHIVE (run make_site_bundle.sh)"; exit 2; }
  want=$(awk '/archive sha256:/{print $3}' "$BDIR/BUNDLE_INFO.txt")
  have=$(sha256sum "$ARCHIVE" | cut -d' ' -f1)
  [ "$want" = "$have" ] || { echo "bundle archive SHA256 $have does not match BUNDLE_INFO.txt $want"; exit 2; }
  if [ ! -d "$SRC" ]; then mkdir -p "$RES/src" && tar xzf "$ARCHIVE" -C "$RES/src"; fi
}

deploy_files() { awk '{print $2}' "$BDIR/DEPLOY_SET.txt"; }
