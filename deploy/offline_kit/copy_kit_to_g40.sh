#!/bin/bash
# copy_kit_to_g40.sh: copy the parts of this kit the G40 needs, from the laptop to the G40.
# Run on the laptop, from the kit folder:
#     bash copy_kit_to_g40.sh <G40 address> [login, default sidekiq]
# It copies the install script, the checksums, the radio code archive and the g40
# folder (about 6.4 GB) to ~/dpmg_offline_kit on the G40, checks the copy there, and
# prints the install command. It asks for the G40 password (twice when no SSH key is set
# up). The full repo archive stays on the laptop.
set -uo pipefail
KIT="$(cd "$(dirname "$0")" && pwd)"
ADDR="${1:-}"; LOGIN="${2:-sidekiq}"
[ -n "$ADDR" ] || { echo "usage: bash copy_kit_to_g40.sh <G40 address> [login]"; exit 2; }
command -v rsync >/dev/null || { echo "rsync is needed on this laptop"; exit 2; }
cd "$KIT" || exit 2
echo "copying to $LOGIN@$ADDR:dpmg_offline_kit, several minutes on a wired link"
rsync -a --info=progress2 --relative \
    install_g40_offline.sh SHA256SUMS README.md \
    code/dpmg_radio_code_70c0120.tar.gz code/librxdrain_aarch64.so \
    g40 "$LOGIN@$ADDR:dpmg_offline_kit/" || { echo "the copy failed"; exit 1; }
echo "checking the copy on the G40, about a minute"
ssh "$LOGIN@$ADDR" 'cd ~/dpmg_offline_kit && sha256sum -c --ignore-missing --quiet SHA256SUMS && echo "copy checked: all files match"' \
    || { echo "the copy does not match the checksums: run this script again"; exit 1; }
echo
echo "Next, on the G40 (asks for the sudo password):"
echo "  ssh -t $LOGIN@$ADDR 'cd ~/dpmg_offline_kit && sudo bash install_g40_offline.sh --check'"
echo "  ssh -t $LOGIN@$ADDR 'cd ~/dpmg_offline_kit && sudo bash install_g40_offline.sh'"
