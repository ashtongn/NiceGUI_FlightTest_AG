#!/bin/bash
# install_laptop_offline.sh: set up the ground station GUI on an x86 Linux laptop with no internet.
# Run on the laptop, from the kit folder:
#     bash install_laptop_offline.sh [folder to unpack the code into, default ~/work]
# It needs Docker already on the laptop, usable without sudo. It loads the GUI image from
# the kit, unpacks the offline map store and the NiceGui_flight folder, and starts the GUI
# with ./launch_gui.sh.
# An image named dpmg, a map store or a NiceGui_flight folder that is already there is left
# as it is.
#   --no-start   do everything except start the GUI
set -uo pipefail
KIT="$(cd "$(dirname "$0")" && pwd)"
START=yes; DEST="$HOME/work"
for a in "$@"; do
  case "$a" in
    --no-start) START=no ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "unknown option: $a"; exit 2 ;;
    *) DEST="$a" ;;
  esac
done
IMG="laptop/dpmg_gui_x86_70c0120.tar.gz"; CODE="code/NiceGui_flight_70c0120.tar.gz"
die() { echo "Stopped: $*"; exit 1; }
check() {
  local want have
  [ -f "$KIT/$1" ] || die "kit file missing: $1"
  want="$(awk -v f="$1" '$2 == f {print $1}' "$KIT/SHA256SUMS")"
  have="$(sha256sum < "$KIT/$1" | cut -d' ' -f1)"
  [ -n "$want" ] && [ "$want" = "$have" ] || die "checksum does not match for $1: copy the kit again"
}

[ "$(uname -m)" = x86_64 ] || die "this script is for the x86 laptop (this machine is $(uname -m))"
command -v docker >/dev/null 2>&1 || die "Docker is not installed on this laptop. It is not in the kit: install it first."
docker info >/dev/null 2>&1 || die "Docker is installed but this account cannot use it (add it to the docker group, or start Docker)."

if docker image inspect dpmg:latest >/dev/null 2>&1; then
  echo "ok    an image named dpmg is already loaded: left as it is"
else
  echo "loading the GUI image, about a minute"
  check "$IMG"
  docker load -i "$KIT/$IMG" || die "the image did not load"
  docker tag dpmg:kit_70c0120 dpmg:latest || die "the loaded image is not dpmg:kit_70c0120"
  echo "done  GUI image loaded as dpmg"
fi

MAPS="$HOME/.dpmg/maps"
if [ -f "$MAPS/index.json" ]; then
  echo "ok    a map store is already in $MAPS: left as it is"
elif [ -f "$KIT/laptop/dpmg_maps.tar" ]; then
  check laptop/dpmg_maps.tar
  mkdir -p "$HOME/.dpmg" && tar xf "$KIT/laptop/dpmg_maps.tar" -C "$HOME/.dpmg" || die "the map store did not unpack"
  echo "done  offline map store unpacked to $MAPS"
else
  echo "no map store in the kit: the mission map will have no background without internet"
fi

REPO="$DEST/NiceGui_flight"
if [ -f "$REPO/launch_gui.sh" ]; then
  echo "ok    $REPO is already there: left as it is"
else
  check "$CODE"
  mkdir -p "$DEST" && tar xzf "$KIT/$CODE" -C "$DEST" || die "the code did not unpack into $DEST"
  echo "done  code unpacked to $REPO"
fi

if [ "$START" = no ]; then
  echo "To start the GUI:  cd $REPO && ./launch_gui.sh"
  exit 0
fi
cd "$REPO" && ./launch_gui.sh || die "launch_gui.sh failed"
echo "The GUI is at http://localhost:8080"
