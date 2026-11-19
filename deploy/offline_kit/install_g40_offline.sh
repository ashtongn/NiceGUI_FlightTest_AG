#!/bin/bash
# install_g40_offline.sh: install the DPMG radio software on a Matchstiq G40 with no internet.
#
# Run it on the G40, from the kit folder (the folder this script is in):
#     sudo bash install_g40_offline.sh
# It needs the G40 on Epiq BSP v1.3.0 (JetPack 6, L4T R36.5). It does not reflash.
# The radio code it installs is the radio part of the NiceGui_flight repo at commit 70c0120.
# Every step looks first and changes only what is missing, so it is safe to run again.
# It starts the two containers and leaves the radio idle: no card is opened, nothing
# is received or transmitted until someone connects from the ground station.
#
# Options:
#   --check              look only, change nothing (no sudo needed)
#   --replace-code       a code folder is already there: back it up, then install the kit's code
#   --amp-safe           needed with --replace-code or --adc-readback on a running unit that has an
#                        rf_frontend.yaml: says the amplifier has been made safe (see the README)
#   --code-dir DIR       code folder on the G40 (default ~/work/NiceGui_test)
#   --laptop-ip ADDR     the ground station laptop: announce to it and take time from it
#   --serial NAME        name shown in the GUI (default: from the hostname, matchstiq-g40-AX35 gives G40-AX35)
#   --static-ip ADDR/NN  add a fixed address on the Ethernet port, for example 192.168.0.23/24
#   --max-power          set the MAXN power mode and jetson_clocks at boot
#   --adc-readback       add the smbus2 wheel to the dpmg image (only the I2C ADC switch readback needs it)
#   --no-start           install everything, do not start the containers
#   --skip-checksums     do not check the kit files against SHA256SUMS
#
# A log of the run is written to the home folder as dpmg_kit_install_<time>.log.
set -uo pipefail

KIT="$(cd "$(dirname "$0")" && pwd)"
# for testing this script on another machine: a folder that stands in for /
ROOT="${DPMG_INSTALL_ROOT:-}"

CHECK=no; REPLACE_CODE=no; CODE_DIR=""; LAPTOP_IP=""; SERIAL=""
STATIC_IP=""; MAX_POWER=no; ADC=no; DO_START=yes; SKIP_SUMS=no; AMP_SAFE=no
while [ $# -gt 0 ]; do
  case "$1" in
    --check)          CHECK=yes ;;
    --replace-code)   REPLACE_CODE=yes ;;
    --amp-safe)       AMP_SAFE=yes ;;
    --code-dir)       CODE_DIR="${2:-}"; shift ;;
    --laptop-ip)      LAPTOP_IP="${2:-}"; shift ;;
    --serial)         SERIAL="${2:-}"; shift ;;
    --static-ip)      STATIC_IP="${2:-}"; shift ;;
    --max-power)      MAX_POWER=yes ;;
    --adc-readback)   ADC=yes ;;
    --no-start)       DO_START=no ;;
    --skip-checksums) SKIP_SUMS=yes ;;
    -h|--help)        sed -n '2,/^# A log of the run/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (see --help)"; exit 2 ;;
  esac
  shift
done

CODE_TAR="code/dpmg_radio_code_70c0120.tar.gz"
ip_ok() { echo "$1" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$'; }
if [ -n "$LAPTOP_IP" ] && ! ip_ok "$LAPTOP_IP"; then echo "--laptop-ip takes an address like 192.168.0.25"; exit 2; fi
if [ -n "$STATIC_IP" ] && ! echo "$STATIC_IP" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}$'; then
  echo "--static-ip takes an address and prefix like 192.168.0.23/24"; exit 2
fi

if [ "$CHECK" = no ] && [ "$(id -u)" != 0 ]; then
  echo "Run with sudo:  sudo bash $0   (or add --check to look only)"; exit 2
fi

# the account that owns the code folder: the one that called sudo
RUN_USER="${SUDO_USER:-$(id -un)}"
[ "$RUN_USER" = root ] && RUN_USER=sidekiq
RUN_HOME="$ROOT$(getent passwd "$RUN_USER" | cut -d: -f6)"
[ -n "$RUN_HOME" ] || { echo "no home folder found for $RUN_USER"; exit 2; }
[ -n "$CODE_DIR" ] || CODE_DIR="$RUN_HOME/work/NiceGui_test"
STAMP="$(date +%Y%m%d_%H%M%S)"

if [ "$CHECK" = no ]; then
  LOG="$RUN_HOME/dpmg_kit_install_$STAMP.log"
  exec > >(tee -a "$LOG") 2>&1
  chown "$RUN_USER" "$LOG" 2>/dev/null || true
fi

REBOOT=no; NOTES=(); TODO=0
step() { echo; echo "=== $(date +%H:%M:%S) $*"; }
ok()   { echo "  ok    $*"; }
did()  { echo "  done  $*"; }
todo() { echo "  todo  $*"; TODO=$((TODO + 1)); }
note() { echo "  note  $*"; NOTES+=("$*"); }
die()  { echo; echo "Stopped: $*"; [ "$CHECK" = no ] && echo "Log: $LOG"; exit 1; }
as_user() { if [ "$(id -un)" = "$RUN_USER" ]; then "$@"; else sudo -u "$RUN_USER" "$@"; fi; }

# check kit files against SHA256SUMS just before they are used
verify() {
  [ "$SKIP_SUMS" = yes ] && return 0
  local f want have
  for f in "$@"; do
    [ -f "$KIT/$f" ] || die "kit file missing: $f"
    want="$(awk -v f="$f" '$2 == f {print $1}' "$KIT/SHA256SUMS")"
    [ -n "$want" ] || die "no checksum for $f in SHA256SUMS"
    have="$(sha256sum < "$KIT/$f" | cut -d' ' -f1)"
    [ "$want" = "$have" ] || die "checksum does not match for $f: the copy is damaged, copy the kit again"
  done
}

image_id() { docker image inspect -f '{{.Id}}' "$1" 2>/dev/null | sed 's/^sha256://' | cut -c1-12; }
running()  { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = true ]; }
is_arm64_lib() { [ -f "$1" ] && [ "$(od -An -tx1 -j18 -N2 "$1" | tr -d ' \n')" = b700 ]; }

step "this unit"
ARCH="$(uname -m)"
REL="$(head -1 "$ROOT/etc/nv_tegra_release" 2>/dev/null | cut -c1-40)"
HOST_NAME="$(hostname)"
echo "  host $HOST_NAME, $ARCH, ${REL:-no /etc/nv_tegra_release}"
[ "$ARCH" = aarch64 ] || die "this is not a G40 (machine type $ARCH). Run the script on the G40."
case "$REL" in
  *R36*) ;;
  *) die "this unit is not on JetPack 6 (L4T R36). Reflash it to Epiq BSP v1.3.0 first: see the reflash section of the kit README." ;;
esac
SKIQ="$(ls "$ROOT"/usr/lib/epiq/libsidekiq.so.* 2>/dev/null | sed 's/.*libsidekiq.so.//' | sort -V | tail -1)"
if [ -n "$SKIQ" ]; then echo "  libsidekiq $SKIQ"; else note "no libsidekiq in /usr/lib/epiq: the radio service will not find the cards"; fi
case "$HOST_NAME" in
  matchstiq-g40-*) NAME_FROM_HOST="G40-${HOST_NAME#matchstiq-g40-}" ;;
  *)               NAME_FROM_HOST="" ;;
esac
if [ -n "$SERIAL" ]; then echo "  name in the GUI: $SERIAL (from --serial)"
elif [ -n "$NAME_FROM_HOST" ]; then echo "  name in the GUI: $NAME_FROM_HOST (from the hostname)"
else note "the hostname is not matchstiq-g40-<serial>: give the unit a name with --serial G40-xxxx"; fi
echo "  account $RUN_USER, code folder $CODE_DIR, kit $KIT"
[ -f "$KIT/SHA256SUMS" ] || die "SHA256SUMS is not in the kit folder: copy the whole kit"

HAVE_GPU="$(command -v docker >/dev/null 2>&1 && image_id dpmg-gpu:r36 || true)"
FREE_GB=$(( $(df -Pk "${ROOT:-/}" | awk 'NR==2 {print $4}') / 1048576 ))
NEED_GB=2; [ -n "$HAVE_GPU" ] || NEED_GB=25
echo "  free disk ${FREE_GB} GB (this install needs about ${NEED_GB} GB)"
if [ "$FREE_GB" -lt "$NEED_GB" ]; then
  [ "$CHECK" = yes ] || die "not enough free disk: ${FREE_GB} GB free, about ${NEED_GB} GB needed"
  note "not enough free disk for the install: ${FREE_GB} GB free, about ${NEED_GB} GB needed"
fi

# Replacing the code or the image restarts the service. While it is down nothing holds the
# GPIO pins of a unit with an external amplifier or switch, and the amplifier can come on.
if { [ "$REPLACE_CODE" = yes ] || [ "$ADC" = yes ]; } && [ -f "$CODE_DIR/rf_frontend.yaml" ] && running dpmg_svc; then
  MSG="this unit has an rf_frontend.yaml (amplifier or switch on the GPIO pins) and its service is running. The service stops for a short time during this install, the pins are let go, and an amplifier can switch on."
  if [ "$AMP_SAFE" = yes ]; then echo "  --amp-safe given: $MSG"
  elif [ "$CHECK" = yes ]; then note "$MSG An install would ask for --amp-safe."
  else die "$MSG Make the amplifier safe first (a rated load on its output), then run again with --amp-safe."; fi
fi

step "Docker and the NVIDIA container runtime"
if command -v docker >/dev/null 2>&1; then
  ok "docker is installed: $(docker --version 2>/dev/null | cut -c1-60)"
elif [ "$CHECK" = yes ]; then
  todo "docker is not installed: the kit's debs would be installed"
else
  ls "$KIT"/g40/debs/*.deb >/dev/null 2>&1 || die "kit folder g40/debs is missing"
  verify $(cd "$KIT" && ls g40/debs/*.deb)
  # one pass can leave packages unconfigured when dpkg meets them out of order; a second pass finishes them.
  # dpkg's exit code also counts packages nobody needs here, so the result is judged by the three that matter.
  dpkg -i "$KIT"/g40/debs/*.deb || dpkg -i "$KIT"/g40/debs/*.deb || true
  for p in docker.io containerd runc; do
    [ "$(dpkg-query -W -f '${db:Status-Abbrev}' "$p" 2>/dev/null | cut -c1-2)" = ii ] || die "package $p did not install: see the dpkg messages above"
  done
  command -v docker >/dev/null 2>&1 || die "docker is still not there after installing the packages"
  LEFT="$(dpkg --audit 2>/dev/null | grep -c '^ ' || true)"
  [ "${LEFT:-0}" = 0 ] || note "$LEFT other package(s) from g40/debs are not fully set up (dpkg --audit lists them); Docker itself is installed"
  did "installed docker: $(docker --version | cut -c1-60)"
fi

if command -v nvidia-ctk >/dev/null 2>&1; then
  ok "NVIDIA container toolkit is installed"
elif [ "$CHECK" = yes ]; then
  todo "NVIDIA container toolkit is not installed: the kit's debs would be installed"
else
  NV_DEBS="$(cd "$KIT" && ls g40/debs/libnvidia-container1_*.deb g40/debs/libnvidia-container-tools_*.deb g40/debs/nvidia-container-toolkit-base_*.deb g40/debs/nvidia-container-toolkit_*.deb)"
  verify $NV_DEBS
  (cd "$KIT" && dpkg -i $NV_DEBS) || true
  command -v nvidia-ctk >/dev/null 2>&1 || die "the NVIDIA container toolkit packages did not install: see the dpkg messages above"
  did "installed the NVIDIA container toolkit"
fi

if docker info 2>/dev/null | grep -i 'runtimes' | grep -qw nvidia; then
  ok "docker has the nvidia runtime"
elif [ "$CHECK" = yes ]; then
  todo "docker has no nvidia runtime: daemon.json would be set and docker restarted"
else
  verify g40/host/daemon.json
  mkdir -p "$ROOT/etc/docker"
  if [ -f "$ROOT/etc/docker/daemon.json" ] && ! cmp -s "$KIT/g40/host/daemon.json" "$ROOT/etc/docker/daemon.json"; then
    cp -p "$ROOT/etc/docker/daemon.json" "$ROOT/etc/docker/daemon.json.before_dpmg_$STAMP"
    echo "  the old daemon.json is kept as daemon.json.before_dpmg_$STAMP"
  fi
  cp "$KIT/g40/host/daemon.json" "$ROOT/etc/docker/daemon.json"
  nvidia-ctk runtime configure --runtime=docker || die "nvidia-ctk could not configure docker"
  systemctl restart docker || die "docker did not restart"
  for _ in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 1; done
  docker info 2>/dev/null | grep -i 'runtimes' | grep -qw nvidia || die "docker still has no nvidia runtime"
  did "docker restarted with the nvidia runtime"
fi

if id -nG "$RUN_USER" | tr ' ' '\n' | grep -qx docker; then
  ok "$RUN_USER is in the docker group"
elif [ "$CHECK" = yes ]; then
  todo "$RUN_USER is not in the docker group: it would be added"
else
  usermod -aG docker "$RUN_USER" || die "could not add $RUN_USER to the docker group"
  did "added $RUN_USER to the docker group (counts from the next login)"
fi

step "receive ring fix (64k DMA ring) and attenuator rule"
DRV="$(ls -d "$RUN_HOME"/sidekiq_image_current/driver/r36* 2>/dev/null | tail -1)"
if [ -z "$DRV" ]; then
  note "no Epiq driver folder at $RUN_HOME/sidekiq_image_current/driver/r36*: the ring fix was not installed. Without it the receiver can go stale while transmitting."
elif cmp -s "$KIT/g40/host/driver_config" "$DRV/driver_config"; then
  ok "driver_config is in $DRV"
elif [ "$CHECK" = yes ]; then
  todo "driver_config would be copied to $DRV (takes effect at the next boot)"
else
  verify g40/host/driver_config
  cp "$KIT/g40/host/driver_config" "$DRV/driver_config" && chown "$RUN_USER": "$DRV/driver_config" || die "could not write $DRV/driver_config"
  did "driver_config copied to $DRV"
fi
P="$ROOT/sys/module/dmadriver/parameters"
D1="$(cat "$P/DMADescriptorsPerEngine" 2>/dev/null || true)"; D2="$(cat "$P/RingBufferPacketCount" 2>/dev/null || true)"
if [ "$D1" = 65536 ] && [ "$D2" = 16384 ]; then
  ok "the driver is running with the 64k ring"
elif [ -z "$D1" ] && [ "$(id -u)" != 0 ]; then
  echo "  the running ring size is readable only with sudo"
elif [ -n "$DRV" ]; then
  REBOOT=yes
  echo "  the driver is running with ring ${D1:-unknown}/${D2:-unknown}: the 64k ring loads at the next boot"
fi

RULE=60-minicircuits.rules
if cmp -s "$KIT/g40/host/$RULE" "$ROOT/etc/udev/rules.d/$RULE"; then
  ok "attenuator udev rule is installed"
elif [ "$CHECK" = yes ]; then
  todo "attenuator udev rule would be installed"
else
  verify g40/host/$RULE
  mkdir -p "$ROOT/etc/udev/rules.d"
  cp "$KIT/g40/host/$RULE" "$ROOT/etc/udev/rules.d/" && udevadm control --reload || die "could not install the udev rule"
  did "attenuator udev rule installed"
fi

if [ -n "$STATIC_IP" ]; then
  step "fixed address $STATIC_IP on the Ethernet port"
  IFACE=enP8p1s0
  if nmcli -t -f NAME con show 2>/dev/null | grep -qx static-eth; then
    ok "connection static-eth is already there: $(nmcli -g ipv4.addresses con show static-eth 2>/dev/null)"
  elif [ "$CHECK" = yes ]; then
    todo "connection static-eth would be added on $IFACE"
  else
    ip link show "$IFACE" >/dev/null 2>&1 || die "no Ethernet port named $IFACE on this unit"
    nmcli con add type ethernet ifname "$IFACE" con-name static-eth ipv4.method manual \
      ipv4.addresses "$STATIC_IP" ipv6.method link-local connection.autoconnect yes connection.autoconnect-priority 10 \
      || die "could not add the connection"
    did "connection static-eth added"
  fi
fi

if [ "$MAX_POWER" = yes ]; then
  step "power mode"
  MODE="$(nvpmodel -q 2>/dev/null | head -1)"
  UNIT="$ROOT/etc/systemd/system/jetson_clocks.service"
  if echo "$MODE" | grep -q MAXN && [ -f "$UNIT" ]; then
    ok "$MODE, jetson_clocks at boot"
  elif [ "$CHECK" = yes ]; then
    todo "now: ${MODE:-unknown}, jetson_clocks at boot: $([ -f "$UNIT" ] && echo yes || echo no). MAXN and jetson_clocks at boot would be set"
  else
    mkdir -p "$ROOT/etc/systemd/system"
    cat > "$UNIT" <<'U'
[Unit]
Description=Maximize Jetson Performance
After=nvpmodel.service

[Service]
Type=oneshot
ExecStart=/usr/bin/jetson_clocks

[Install]
WantedBy=multi-user.target
U
    systemctl daemon-reload; systemctl enable jetson_clocks.service || die "could not enable jetson_clocks"
    if ! echo "$MODE" | grep -q MAXN; then
      # nvpmodel asks to reboot now; answer no, the reboot comes at the end
      echo NO | nvpmodel -m 0 || true
      REBOOT=yes
    fi
    did "MAXN and jetson_clocks at boot set"
  fi
fi

if [ -n "$LAPTOP_IP" ]; then
  step "clock: take time from the laptop at $LAPTOP_IP"
  CONF="$ROOT/etc/systemd/timesyncd.conf.d/dpmg.conf"
  if grep -qx "NTP=$LAPTOP_IP" "$CONF" 2>/dev/null; then
    ok "timesyncd already points at $LAPTOP_IP"
  elif [ "$CHECK" = yes ]; then
    todo "timesyncd would be pointed at $LAPTOP_IP"
  else
    mkdir -p "$(dirname "$CONF")"
    cat > "$CONF" <<CONF
# DPMG: the ground station laptop is the time source, the pool the fallback
# (written by install_g40_offline.sh, same as deploy/ntp/jetson_timesyncd.sh)
[Time]
NTP=$LAPTOP_IP
FallbackNTP=ntp.ubuntu.com
PollIntervalMinSec=32
PollIntervalMaxSec=256
CONF
    systemctl restart systemd-timesyncd || die "systemd-timesyncd did not restart"
    did "timesyncd points at $LAPTOP_IP (the laptop must serve time: deploy/ntp/laptop_chrony.sh on it)"
  fi
fi

step "container images"
LOADED=no
load_image() {   # name, file in the kit, the id of the kit image when it is known
  local name="$1" file="$2" want="${3:-}" have
  have="$(image_id "$name")"
  if [ -n "$have" ]; then
    if [ -z "$want" ]; then ok "$name is loaded (id $have): left as it is"
    elif [ "$have" = "$want" ]; then ok "$name is loaded (id $have, same as the kit)"
    else ok "$name is loaded with another id ($have, the kit has $want): left as it is"; fi
    return 0
  fi
  if [ "$CHECK" = yes ]; then todo "$name would be loaded from $file"; return 0; fi
  echo "  loading $name, this takes a few minutes"
  verify "$file"
  if command -v zstd >/dev/null 2>&1; then zstd -dc "$KIT/$file" | docker load
  else docker load -i "$KIT/$file"; fi
  have="$(image_id "$name")"
  [ -n "$have" ] || die "$name did not load"
  did "$name loaded (id $have)"; LOADED=yes
}
load_image dpmg:latest  g40/images/dpmg_latest.tar.zst  9d43b051aa5b
load_image dpmg-gpu:r36 g40/images/dpmg-gpu_r36.tar.zst a93753121839

if [ -n "$(image_id dpmg:latest)" ]; then
  if docker run --rm dpmg:latest python -c "import gpiod, hid, smbus2" >/dev/null 2>&1; then
    ok "the dpmg image has gpiod, hid and smbus2"
  elif [ "$ADC" = no ]; then
    echo "  the dpmg image has no smbus2. Only the I2C ADC switch readback needs it (--adc-readback adds it)."
  elif [ "$CHECK" = yes ]; then
    todo "the wheels in g40/host/wheels would be added to the dpmg image"
  else
    verify g40/host/Dockerfile.frontend-overlay $(cd "$KIT" && ls g40/host/wheels/*.whl)
    OV="$(mktemp -d)"
    cp "$KIT"/g40/host/wheels/*.whl "$OV/" && cp "$KIT/g40/host/Dockerfile.frontend-overlay" "$OV/Dockerfile"
    docker build -t dpmg:latest "$OV" || die "the image overlay did not build"
    rm -rf "$OV"
    docker run --rm dpmg:latest python -c "import gpiod, hid, smbus2" || die "the wheels did not import after the overlay"
    did "wheels added to the dpmg image (id now $(image_id dpmg:latest))"; LOADED=yes
  fi
fi

step "radio code in $CODE_DIR"
NEW_CODE=no
unpack_code() {
  verify "$CODE_TAR"
  as_user mkdir -p "$CODE_DIR" || die "could not make $CODE_DIR"
  as_user tar xzf "$KIT/$CODE_TAR" -C "$CODE_DIR" --no-same-owner || die "the code did not unpack"
  NEW_CODE=yes
}
if [ ! -f "$CODE_DIR/dpmg/radio/dpmg_service.py" ]; then
  if [ "$CHECK" = yes ]; then todo "the code would be unpacked from $CODE_TAR"
  else unpack_code; did "code unpacked from $CODE_TAR"; fi
elif [ "$REPLACE_CODE" = no ]; then
  ok "a code tree is already there: left as it is (--replace-code backs it up and installs the kit's code)"
elif [ "$CHECK" = yes ]; then
  todo "the code folder would be backed up and the kit's code unpacked over it"
else
  if running dpmg_svc || running dpmg-gpu; then
    echo "  stopping the containers first"
    docker stop -t 30 dpmg_svc dpmg-gpu >/dev/null 2>&1 || true
  fi
  BK="$RUN_HOME/work/dpmg_backups/code_before_kit_$STAMP.tgz"
  as_user mkdir -p "$(dirname "$BK")"
  as_user tar czf "$BK" --exclude=./data --exclude=./output --exclude=__pycache__ -C "$CODE_DIR" . || die "the backup of the old code failed, nothing replaced"
  echo "  old code saved as $BK (without data and output)"
  unpack_code; did "code unpacked from $CODE_TAR over the old tree"
fi
if [ -f "$CODE_DIR/rf_frontend.yaml" ]; then echo "  rf_frontend.yaml is in the code folder and was not touched"
else echo "  no rf_frontend.yaml: the service runs with no amplifier, switch or attenuator settings"; fi

LIB="$CODE_DIR/native/lib/librxdrain.so"
if is_arm64_lib "$LIB"; then
  ok "receive drain library is in place"
elif [ ! -f "$CODE_DIR/native/build.sh" ]; then
  [ "$CHECK" = yes ] || note "no native/build.sh in the code folder: receive drain library not built"
elif [ "$CHECK" = yes ]; then
  todo "the receive drain library would be built"
else
  UG="$(id -u "$RUN_USER"):$(id -g "$RUN_USER")"
  if docker run --rm --user "$UG" -v "$CODE_DIR":/app_svc -w /app_svc dpmg:latest bash native/build.sh && is_arm64_lib "$LIB"; then
    did "receive drain library built"
  else
    verify code/librxdrain_aarch64.so
    as_user mkdir -p "$CODE_DIR/native/lib" && as_user cp "$KIT/code/librxdrain_aarch64.so" "$LIB" || die "could not place the receive drain library"
    did "the build failed, so the kit's ready built library was copied in"
  fi
fi

step "start"
UP="$CODE_DIR/deploy/radio/g40_up_r36.sh"
if [ "$CHECK" = yes ]; then
  for c in dpmg-gpu dpmg_svc; do
    if running "$c"; then ok "$c is running"; else todo "$c is not running"; fi
  done
elif [ "$REBOOT" = yes ]; then
  echo "  not started: this unit needs a restart first (see below)"
elif [ "$DO_START" = no ]; then
  echo "  not started (--no-start). To start:  cd $CODE_DIR && bash deploy/radio/g40_up_r36.sh"
elif running dpmg_svc && running dpmg-gpu && [ "$NEW_CODE" = no ] && [ "$LOADED" = no ]; then
  ok "both containers are already running and nothing changed: left as they are"
else
  [ -f "$UP" ] || die "no launcher at $UP"
  ( cd "$CODE_DIR" && DPMG_BROADCAST="${LAPTOP_IP:-255.255.255.255}" DPMG_SERIAL="$SERIAL" bash "$UP" ) || die "the launcher failed"
  sleep 8
  docker ps --filter name=dpmg --format '  {{.Names}}  {{.Status}}'
  running dpmg_svc && running dpmg-gpu || die "a container is not running: look at  docker logs dpmg_svc  and  docker logs dpmg-gpu"
  docker logs dpmg_svc 2>&1 | grep -E 'up: cmd_port|Traceback' | tail -3 | cut -c1-140 | sed 's/^/  /'
  docker logs dpmg_svc 2>&1 | grep -q 'up: cmd_port' || note "the service has not printed its 'up: cmd_port' line yet: look at  docker logs dpmg_svc"
  did "containers started, radio idle"
fi

step "summary"
ADDRS="$(ip -4 -br addr 2>/dev/null | grep -vE '^(lo|docker|l4t|veth)' | awk '{print $1, $3}' | tr '\n' ';')"
echo "  addresses: ${ADDRS:-none read}"
if [ ${#NOTES[@]} -gt 0 ]; then for n in "${NOTES[@]}"; do echo "  note: $n"; done; fi
if [ "$CHECK" = yes ]; then
  echo "  look only: nothing was changed. $TODO item(s) would be done by an install."
  exit 0
fi
if [ "$REBOOT" = yes ]; then
  echo "  Restart needed. Run:  sudo reboot"
  echo "  When it is back, run the same install command again: it checks the ring and starts the containers."
else
  echo "  Installed. In the ground station GUI use Connect by IP with this unit's address, port 47810."
fi
echo "  Log: $LOG"
exit 0
