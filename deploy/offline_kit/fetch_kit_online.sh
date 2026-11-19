#!/bin/bash
# fetch_kit_online.sh: build the DPMG offline kit on a laptop that has internet.
# Run on an x86 Linux laptop with Docker, from a checkout of this repo:
#     bash deploy/offline_kit/fetch_kit_online.sh [--out <kit folder>]
# It makes the kit folder, DPMG_G40_offline_kit beside the repo folder unless --out names
# another, and fills it. The three scripts beside this one (install_g40_offline.sh,
# copy_kit_to_g40.sh, install_laptop_offline.sh) are copied into the kit with README.md,
# and from there they work on a G40 and a laptop with no internet.
#
# Where each part comes from:
#   code      this repo at the commit that is checked out, packed two ways
#   host      small G40 files, copied from deploy/g40 and deploy/wheels
#   debs      Docker from Ubuntu 22.04 for arm64, and the NVIDIA container toolkit 1.16.2
#   g40image  the radio service image, built here for arm64 from the repo's Dockerfile
#   gpuimage  the GPU image, built here for arm64 on NVIDIA's l4t-jetpack r36.4.0
#   guiimage  the ground station GUI image for the laptop, built here
#   maps      the offline map store: renderer pieces, world base map, Arizona and Idaho
# Not downloaded: g40/firmware/sidekiq_hardware_updater_for_v4.25.0.sh. Epiq gives it out
# through its support site. Put it in g40/firmware by hand if the cards need the update.
#
# The kit that was install tested on a G40 in November 2026 carried images saved from a
# working G40. This script builds the images new from the same Dockerfiles, and takes the
# Ubuntu packages at today's versions, so its kit is not that one file for file.
#
# Options:
#   --out DIR     the kit folder to fill
#   --only LIST   do only these parts, comma separated
#   --skip LIST   leave these parts out
#   --check       list what is here and what would be fetched, change nothing
#   --again       fetch a part again even when its files are already here
#   --no-areas    world base map only, without the Arizona and Idaho areas
# A part whose files are already here is left alone, so run the script again after a
# failed download. Downloads: about 0.1 GB for debs, 1 GB for the two small images, 1 GB
# for maps, and about 6 GB for the GPU image, which also needs 35 GB of free disk.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(git -C "$HERE" rev-parse --show-toplevel 2>/dev/null)" || { echo "Stopped: run this from a git checkout of the repo"; exit 1; }
REV="$(git -C "$REPO" rev-parse --short=7 HEAD)"
# the commit label the kit scripts carry in the repo; the kit copies get REV in its place
LABEL=70c0120
ALL="code host debs g40image gpuimage guiimage maps"
ARM_BASE=arm64v8/ubuntu:22.04
TOOLKIT=1.16.2-1
GUI_TAG="dpmg:kit_$REV"

KIT="$(dirname "$REPO")/DPMG_G40_offline_kit"
ONLY=""; SKIP=""; CHECK=no; AGAIN=no; AREAS=yes
while [ $# -gt 0 ]; do
  case "$1" in
    --out) KIT="${2:-}"; shift ;;
    --only) ONLY="${2:-}"; shift ;;
    --skip) SKIP="${2:-}"; shift ;;
    --check) CHECK=yes ;;
    --again) AGAIN=yes ;;
    --no-areas) AREAS=no ;;
    -h|--help) sed -n '2,/^# for maps/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1"; exit 2 ;;
  esac
  shift
done

FAILED=""
step() { echo; echo "== $*"; }
ok()   { echo "  ok    $*"; }
did()  { echo "  done  $*"; }
todo() { echo "  todo  $*"; }
note() { echo "  note  $*"; }
die()  { echo "Stopped: $*"; exit 1; }
failed() { echo "  failed  $1: $2"; FAILED="$FAILED $1"; }
wanted() {
  case ",$SKIP," in *",$1,"*) return 1 ;; esac
  [ -z "$ONLY" ] && return 0
  case ",$ONLY," in *",$1,"*) return 0 ;; esac
  return 1
}
for p in $(echo "$ONLY,$SKIP" | tr ',' ' '); do
  case " $ALL " in *" $p "*) ;; *) die "no part named $p. The parts are: $ALL" ;; esac
done
# true when every named kit file is here and --again was not given
have() {
  [ "$AGAIN" = yes ] && return 1
  local f
  for f in "$@"; do [ -s "$KIT/$f" ] || return 1; done
  return 0
}
size_of() { du -sh --apparent-size "$KIT/$1" 2>/dev/null | cut -f1; }

CODE_FULL="code/NiceGui_flight_$REV.tar.gz"
CODE_RADIO="code/dpmg_radio_code_$REV.tar.gz"
DRAIN="code/librxdrain_aarch64.so"
IMG_SVC="g40/images/dpmg_latest.tar.zst"
IMG_GPU="g40/images/dpmg-gpu_r36.tar.zst"
IMG_GUI="laptop/dpmg_gui_x86_$REV.tar.gz"
MAPS_TAR="laptop/dpmg_maps.tar"
FIRMWARE="g40/firmware/sidekiq_hardware_updater_for_v4.25.0.sh"
FIRMWARE_SHA=c8b3f49374758d5200ee65994f37b41a6940e57d888154d375fac4455030c025
EXAMPLES="bpsk_msg_1m92.iq.bin fsk_pair_2400k.iq.bin fsk_pair_2m4.cdif fsk_pair_2m4.iq.bin fsk_pair_nomark.iq.bin qpsk_msg_5m.c64"
# the Docker package set, by name: what a bare arm64 Ubuntu 22.04 needs for docker.io.
# The toolkit packages are pinned below.
DEB_NAMES="apparmor bridge-utils containerd dbus dnsmasq-base dns-root-data docker.io git git-man
  iproute2 iptables less libapparmor1 libatm1 libbpf0 libbrotli1 libbsd0 libcap2-bin libcbor0.8
  libcurl3-gnutls libdbus-1-3 libedit2 libelf1 liberror-perl libexpat1 libfido2-1 libgdbm6
  libgdbm-compat4 libip4tc2 libip6tc2 libjansson4 libldap-2.5-0 libldap-common libmd0 libmnl0
  libnetfilter-conntrack3 libnfnetlink0 libnftables1 libnftnl11 libnghttp2-14 libpam-cap
  libperl5.34 libpsl5 librtmp1 libsasl2-2 libsasl2-modules libsasl2-modules-db libssh-4 libx11-6
  libx11-data libxau6 libxcb1 libxdmcp6 libxext6 libxmuu1 libxtables12 netbase netcat
  netcat-openbsd openssh-client patch perl perl-modules-5.34 pigz publicsuffix runc ubuntu-fan
  xauth xz-utils"
DEB_PINNED="libnvidia-container1=$TOOLKIT libnvidia-container-tools=$TOOLKIT nvidia-container-toolkit-base=$TOOLKIT nvidia-container-toolkit=$TOOLKIT"

[ -n "$KIT" ] || die "--out needs a folder"
if [ "$CHECK" = yes ]; then
  step "what is in $KIT (repo at commit $REV)"
  report() {   # part, files
    local part="$1"; shift
    wanted "$part" || { echo "  skip  $part"; return; }
    if AGAIN=no have "$@"; then ok "$part is here ($(for f in "$@"; do size_of "$f"; done | tr '\n' ' '))"
    else todo "$part would be fetched"; fi
  }
  report code "$CODE_FULL" "$CODE_RADIO" "$DRAIN"
  report host g40/host/driver_config g40/host/daemon.json g40/host/60-minicircuits.rules
  if wanted debs; then
    n="$(ls "$KIT"/g40/debs/*.deb 2>/dev/null | wc -l)"
    if [ "$n" -ge 70 ]; then ok "debs is here ($n packages)"; else todo "debs would be fetched ($n packages here now)"; fi
  fi
  report g40image "$IMG_SVC"
  report gpuimage "$IMG_GPU"
  report guiimage "$IMG_GUI"
  report maps "$MAPS_TAR"
  if [ -s "$KIT/$FIRMWARE" ]; then ok "card firmware updater is here"; else note "card firmware updater is not here: it comes from Epiq, not from this script"; fi
  exit 0
fi

step "this laptop"
[ "$(uname -m)" = x86_64 ] || die "this script is for an x86 laptop (this machine is $(uname -m))"
for t in docker tar gzip sha256sum; do command -v $t >/dev/null 2>&1 || die "$t is not installed on this laptop"; done
docker info >/dev/null 2>&1 || die "Docker is installed but this account cannot use it (add it to the docker group, or start Docker)"
docker buildx version >/dev/null 2>&1 || die "the Docker buildx plugin is not installed (package docker-buildx)"
if wanted g40image || wanted gpuimage; then command -v zstd >/dev/null 2>&1 || die "zstd is not installed (sudo apt install zstd)"; fi
if command -v curl >/dev/null 2>&1; then
  curl -fsS -m 15 -o /dev/null http://ports.ubuntu.com/ubuntu-ports/dists/jammy/Release || die "no answer from ports.ubuntu.com: this laptop needs internet"
fi
mkdir -p "$KIT" && KIT="$(cd "$KIT" && pwd)" || die "could not make the kit folder $KIT"
case "$KIT/" in "$REPO"/*) die "the kit folder must be outside the repo folder" ;; esac
ok "x86 laptop, Docker usable, $(df -h --output=avail "$KIT" | tail -1 | tr -d ' ') free in $KIT"

WORK="$(mktemp -d "$KIT/.fetch_work.XXXXXX")" || die "could not make a work folder in $KIT"
trap 'rm -rf "$WORK"' EXIT
SRC="$WORK/src/NiceGui_flight"
mkdir -p "$KIT/code" "$KIT/g40/images" "$KIT/g40/debs" "$KIT/g40/host/wheels" "$KIT/g40/firmware" "$KIT/laptop"

# arm64 programs run on this laptop through emulation; checked once, when first needed
ARM_OK=""
need_arm64() {
  [ -n "$ARM_OK" ] && { [ "$ARM_OK" = yes ]; return; }
  if [ "$(docker run --rm --platform linux/arm64 $ARM_BASE uname -m 2>/dev/null)" = aarch64 ]; then ARM_OK=yes; return 0; fi
  ARM_OK=no
  echo "  arm64 emulation is not set up in Docker on this laptop. Set it up once with:"
  echo "      docker run --privileged --rm tonistiigi/binfmt --install arm64"
  return 1
}

step "code and kit scripts"
mkdir -p "$WORK/src"
git -C "$REPO" archive --format=tar --prefix=NiceGui_flight/ HEAD | tar -x -C "$WORK/src" || die "could not read the code from $REPO"
[ -f "$SRC/Dockerfile" ] && [ -f "$SRC/deploy/docker/Dockerfile.gpu.r36" ] || die "$REPO does not look like the DPMG repo"
ok "code is $REPO at commit $REV"
[ -z "$(git -C "$REPO" status --porcelain --untracked-files=no)" ] || note "changes in $REPO that are not committed are not in the kit"
# the kit scripts name the code archives by commit, and the G40 one names the image ids of
# the November kit: the copies get this commit, and no image ids
FROM="$SRC/deploy/offline_kit"; [ -f "$FROM/install_g40_offline.sh" ] || FROM="$HERE"
for f in install_g40_offline.sh copy_kit_to_g40.sh install_laptop_offline.sh README.md; do
  sed -e "s/$LABEL/$REV/g" -e 's/^\(load_image .*[^ ]\) *[0-9a-f]\{12\}$/\1/' "$FROM/$f" > "$KIT/$f" || die "could not copy $f into the kit"
done
chmod +x "$KIT"/*.sh
did "install scripts and README.md copied into the kit"

if wanted guiimage; then
  step "GUI image for the laptop"
  if have "$IMG_GUI"; then ok "$IMG_GUI is here ($(size_of "$IMG_GUI"))"
  else
    echo "  building, a few minutes the first time"
    # the repo's Dockerfile, plus the library the secure messaging needs on the ground station
    { cat "$SRC/Dockerfile"; echo; echo "RUN pip install --no-cache-dir -r requirements-crypto.txt"; } > "$WORK/Dockerfile.gui"
    # the field cards are not part of the image
    rm -f "$SRC"/flight_test/DPMG_Flight_Test_Field_Cards.*
    if docker buildx build --quiet --load --platform linux/amd64 -t "$GUI_TAG" -f "$WORK/Dockerfile.gui" "$SRC" >/dev/null \
       && docker save "$GUI_TAG" | gzip -3 > "$KIT/$IMG_GUI.part" && mv "$KIT/$IMG_GUI.part" "$KIT/$IMG_GUI"; then
      did "$IMG_GUI ($(size_of "$IMG_GUI")), also loaded on this laptop as $GUI_TAG"
    else rm -f "$KIT/$IMG_GUI.part"; failed guiimage "the build or the save did not finish"; fi
  fi
fi

if wanted code; then
  step "code archives"
  if have "$CODE_FULL" "$CODE_RADIO" "$DRAIN"; then ok "code archives are here"
  else
    # radio part first, from the tracked files only: what deploy.sh sends to a JetPack 6 radio
    cp -a "$SRC" "$WORK/radio"
    ( cd "$WORK/radio" && rm -rf bench tests docs tools deploy/g40 deploy/offline_kit deploy/push_update.sh deploy.sh update.sh \
        launch_gui.sh launch_gui.bat launch_gui.ps1 .gitignore .env.example requirements-dev.txt \
        deploy/radio/dpmg_doctor.sh deploy/radio/x40_tx_up.sh deploy/docker/Dockerfile.gpu \
        flight_test/DPMG_Flight_Test_Field_Cards.* \
      && find . -name '*.md' -delete \
      && find . \( -name '*.png' -o -name '*.docx' -o -name '*.bin' -o -name '*.npy' -o -name '*.cdif' -o -name '*.c64' \) -delete )
    # example IQ files and the field cards are not tracked in git: taken from the folder when there
    for f in $EXAMPLES; do [ -f "$REPO/flight_test/files/$f" ] && cp -p "$REPO/flight_test/files/$f" "$SRC/flight_test/files/"; done
    for f in "$REPO"/flight_test/DPMG_Flight_Test_Field_Cards.*; do [ -f "$f" ] && cp -p "$f" "$SRC/flight_test/"; done
    MISSING=""; for f in $EXAMPLES; do [ -f "$SRC/flight_test/files/$f" ] || MISSING="$MISSING $f"; done
    if [ -n "$MISSING" ] && docker image inspect "$GUI_TAG" >/dev/null 2>&1; then
      echo "  example IQ files are not in the folder: making them with flight_test/files/make_example_files.py"
      docker run --rm --user "$(id -u):$(id -g)" -e PYTHONDONTWRITEBYTECODE=1 -e HOME=/tmp -v "$SRC":/work -w /work "$GUI_TAG" \
        python flight_test/files/make_example_files.py --repo /work >/dev/null 2>&1 || true
      MISSING=""; for f in $EXAMPLES; do [ -f "$SRC/flight_test/files/$f" ] || MISSING="$MISSING $f"; done
    fi
    [ -z "$MISSING" ] || note "example IQ files for the file playback card are not in the kit:$MISSING"
    mkdir -p "$WORK/radio/data/waveforms/flight"
    cp -p "$SRC"/flight_test/files/*.bin "$SRC"/flight_test/files/*.cdif "$SRC"/flight_test/files/*.c64 \
          "$SRC"/flight_test/files/*.json "$WORK/radio/data/waveforms/flight/" 2>/dev/null
    tar czf "$KIT/$CODE_RADIO" --owner=0 --group=0 --numeric-owner -C "$WORK/radio" . || die "could not write $CODE_RADIO"
    tar cf - --owner=0 --group=0 --numeric-owner -C "$WORK/src" NiceGui_flight | gzip -9 -n > "$KIT/$CODE_FULL" || die "could not write $CODE_FULL"
    did "$CODE_FULL ($(size_of "$CODE_FULL")) and $CODE_RADIO ($(size_of "$CODE_RADIO"))"
    # receive drain library for the G40, a fallback: the install builds it on the G40 when it can
    if have "$DRAIN"; then ok "$DRAIN is here"
    elif need_arm64; then
      mkdir -p "$WORK/drain"
      if docker run --rm --platform linux/arm64 -v "$SRC/native":/native:ro -v "$WORK/drain":/out $ARM_BASE bash -c \
           "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq gcc libc6-dev >/dev/null 2>&1 && bash /native/build.sh /out >/dev/null && chown $(id -u):$(id -g) /out/librxdrain.so" \
         && cp "$WORK/drain/librxdrain.so" "$KIT/$DRAIN"; then did "$DRAIN built for arm64"
      else failed code "the receive drain library did not build"; fi
    else failed code "the receive drain library needs arm64 emulation"; fi
  fi
fi

if wanted host; then
  step "G40 host files"
  H="$KIT/g40/host"
  cp -p "$SRC"/deploy/g40/driver_config "$SRC"/deploy/g40/60-minicircuits.rules "$SRC"/deploy/g40/epiq-gps-ant-bias.service \
        "$SRC"/deploy/g40/Dockerfile.frontend-overlay "$H/" && cp -p "$SRC"/deploy/wheels/*.whl "$H/wheels/" \
    || failed host "files are missing under deploy/g40 or deploy/wheels"
  cat > "$H/daemon.json" <<'JSON'
{
    "runtimes": {
        "nvidia": {
            "args": [],
            "path": "nvidia-container-runtime"
        }
    }
}
JSON
  did "g40/host: ring fix, udev rule, Docker runtime file, $(ls "$H"/wheels/*.whl 2>/dev/null | wc -l) wheels"
fi

if wanted debs; then
  step "Docker and NVIDIA toolkit packages for the G40"
  N="$(ls "$KIT"/g40/debs/*.deb 2>/dev/null | wc -l)"
  if [ "$AGAIN" = no ] && [ "$N" -ge 70 ]; then ok "g40/debs holds $N packages"
  elif need_arm64; then
    rm -rf "$WORK/debs"; mkdir -p "$WORK/debs"
    echo "  downloading in an arm64 Ubuntu 22.04 container, a few minutes"
    if docker run --rm --platform linux/arm64 -v "$WORK/debs":/out $ARM_BASE bash -c "
         set -e; export DEBIAN_FRONTEND=noninteractive
         apt-get update -qq; apt-get install -y -qq ca-certificates curl gnupg >/dev/null 2>&1
         curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
         curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
           | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' > /etc/apt/sources.list.d/nvidia-container-toolkit.list
         apt-get update -qq
         cd /out; apt-get download -qq $(echo $DEB_NAMES) $DEB_PINNED 2>&1 | grep -v 'unsandboxed' || true
         chown -R $(id -u):$(id -g) /out"; then
      GOT="$(ls "$WORK"/debs/*.deb 2>/dev/null | wc -l)"
      echo "  $GOT packages downloaded. Trying them in a clean arm64 system with the network off"
      # the same two passes install_g40_offline.sh makes
      RES="$(docker run --rm --platform linux/arm64 --network none -v "$WORK/debs":/debs:ro $ARM_BASE bash -c '
               export DEBIAN_FRONTEND=noninteractive
               dpkg -i /debs/*.deb >/dev/null 2>&1; dpkg -i /debs/*.deb >/dev/null 2>&1
               for p in docker.io containerd runc nvidia-container-toolkit; do printf "%s " "$(dpkg-query -W -f "\${db:Status-Abbrev}" $p 2>/dev/null | cut -c1-2)"; done
               printf "left=%s " "$(dpkg --audit | grep -c "^ ")"; docker --version 2>/dev/null | cut -d, -f1' 2>/dev/null)"
      case "$RES" in
        "ii ii ii ii left=0 Docker version"*)
          rm -f "$KIT"/g40/debs/*.deb; mv "$WORK"/debs/*.deb "$KIT/g40/debs/"
          did "g40/debs: $GOT packages, install checked (${RES#*left=0 })" ;;
        *) failed debs "the packages did not install cleanly in the trial ($RES)" ;;
      esac
    else failed debs "the download did not finish"; fi
  else failed debs "needs arm64 emulation"; fi
fi

# build one arm64 image straight into a compressed archive, without loading it on this laptop
build_arm_image() {   # part, image name, Dockerfile, archive in the kit
  local part="$1" name="$2" file="$3" out="$4" cfg
  if have "$out"; then ok "$out is here ($(size_of "$out"))"; return; fi
  need_arm64 || { failed "$part" "needs arm64 emulation"; return; }
  if docker buildx build --quiet --platform linux/arm64 -t "$name" -f "$SRC/$file" -o type=docker,dest=- "$SRC" \
       | zstd -T0 -10 -q -o "$KIT/$out.part" -f; then
    cfg="$(zstd -dc "$KIT/$out.part" | tar -xO manifest.json 2>/dev/null)"
    case "$cfg" in
      *"\"$name\""*) mv "$KIT/$out.part" "$KIT/$out"; did "$out ($(size_of "$out")), carries the name $name" ;;
      *) rm -f "$KIT/$out.part"; failed "$part" "the archive does not carry the name $name" ;;
    esac
  else rm -f "$KIT/$out.part"; failed "$part" "the build did not finish"; fi
}

if wanted g40image; then
  step "radio service image for the G40 (arm64)"
  have "$IMG_SVC" || echo "  building under emulation, 10 to 20 minutes"
  build_arm_image g40image dpmg:latest Dockerfile "$IMG_SVC"
fi

if wanted gpuimage; then
  step "GPU image for the G40 (arm64)"
  if have "$IMG_GPU"; then ok "$IMG_GPU is here ($(size_of "$IMG_GPU"))"
  else
    ROOT="$(docker info -f '{{.DockerRootDir}}' 2>/dev/null)"
    FREE_KIT="$(df --output=avail -BG "$KIT" | tail -1 | tr -dc 0-9)"
    FREE_DOCKER="$(df --output=avail -BG "${ROOT:-/var/lib/docker}" 2>/dev/null | tail -1 | tr -dc 0-9)"
    if [ "${FREE_DOCKER:-0}" -lt 35 ] || [ "$FREE_KIT" -lt 8 ]; then
      failed gpuimage "not enough free disk: Docker has ${FREE_DOCKER:-0} GB and needs 35, the kit folder has $FREE_KIT GB and needs 8"
    else
      echo "  pulling NVIDIA's l4t-jetpack base and building, an hour or more on a slow link"
      build_arm_image gpuimage dpmg-gpu:r36 deploy/docker/Dockerfile.gpu.r36 "$IMG_GPU"
    fi
  fi
fi

if wanted maps; then
  step "offline map store"
  if have "$MAPS_TAR"; then ok "$MAPS_TAR is here ($(size_of "$MAPS_TAR"))"
  elif ! command -v python3 >/dev/null 2>&1; then failed maps "python3 is not installed on this laptop"
  else
    export DPMG_MAP_STORE="$WORK/maps" PYTHONDONTWRITEBYTECODE=1
    echo "  renderer pieces and the world base map, about 0.3 GB"
    if python3 "$SRC/tools/maps/vendor_map_assets.py" >"$WORK/maps.log" 2>&1 \
       && python3 "$SRC/tools/maps/build_base_map.py" >>"$WORK/maps.log" 2>&1; then
      AREA_OK=yes
      if [ "$AREAS" = yes ]; then
        echo "  Arizona and Idaho in detail, about 0.2 GB each"
        python3 - "$SRC" >>"$WORK/maps.log" 2>&1 <<'PY' || AREA_OK=no
import sys
sys.path.insert(0, sys.argv[1])
from dpmg.mission.base_map import download_area
download_area("arizona", [-114.82, 31.33, -109.04, 37.0])
download_area("idaho", [-117.25, 41.98, -111.03, 49.01])
PY
      fi
      if [ "$AREA_OK" = yes ] && tar cf "$KIT/$MAPS_TAR.part" -C "$WORK" maps && mv "$KIT/$MAPS_TAR.part" "$KIT/$MAPS_TAR"; then
        did "$MAPS_TAR ($(size_of "$MAPS_TAR"))"
      else rm -f "$KIT/$MAPS_TAR.part"; tail -5 "$WORK/maps.log"; failed maps "an area did not download"; fi
    else tail -5 "$WORK/maps.log"; failed maps "the renderer pieces or the base map did not download"; fi
  fi
fi

step "checksums"
if [ -s "$KIT/$FIRMWARE" ]; then
  if [ "$(sha256sum < "$KIT/$FIRMWARE" | cut -d' ' -f1)" = "$FIRMWARE_SHA" ]; then ok "card firmware updater is here (the v4.25.0 file)"
  else note "the card firmware updater here is not the v4.25.0 file this kit was made for"; fi
else note "card firmware updater is not here: it comes from Epiq, not from this script"; fi
( cd "$KIT" && find code g40 laptop -type f ! -name '*.part' | LC_ALL=C sort | tr '\n' '\0' | xargs -0 sha256sum > SHA256SUMS ) || die "could not write SHA256SUMS"
did "SHA256SUMS lists $(wc -l < "$KIT/SHA256SUMS") files, kit folder is $(du -sh --apparent-size "$KIT" | cut -f1)"

echo
if [ -n "$FAILED" ]; then
  echo "Not finished:$FAILED. Fix what the lines above say and run the script again; finished parts are kept."
  exit 1
fi
echo "Finished. The kit is $KIT. Next, from that folder, as its README.md says:"
echo "  laptop:  bash install_laptop_offline.sh"
echo "  G40:     bash copy_kit_to_g40.sh <G40 address>"
