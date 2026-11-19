#!/bin/bash
# Pre-flight checks, then run Epiq's epiq-flash-jetson-orin-nano-devkit.sh for the
# Matchstiq X40 Jetson in static IP mode.
#
# The Epiq script uses NVIDIA's --massflash 1, which flashes the first Jetson it
# finds in recovery mode on the host USB without checking which board it is. The
# X40 Jetson is an Orin NX 16GB (0955:7323 in recovery mode), the same module and
# the same USB id as the G40s, so the id alone cannot tell an X40 from a G40.
# This script refuses to run unless all of these hold:
#   1. exactly one NVIDIA USB device is present on the host, so no G40 and no
#      Jetson Orin Nano Super may be on the host USB at all, not even booted
#      normally (0955:7020);
#   2. that device is 0955:7323, an Orin NX 16GB in recovery mode (the Orin Nano
#      Super shows 0955:7523 or 0955:7623 and is refused);
#   3. its USB serial string equals APX_SERIAL, which the operator records with
#      --probe while this X40 alone is in recovery mode, and it is not one of the
#      serials in OTHER_SERIALS (known G40 and Nano modules). A device with no
#      serial string is accepted only with APX_SERIAL=none.
#
# Usage:
#   ./guarded_flash.sh --probe
#       list the NVIDIA USB devices with their serial strings and exit
#   BSP_DIR=<extracted BSP dir> HOST_IP=<host ip> TARGET_IP=<x40 ip> APX_SERIAL=<serial> ./guarded_flash.sh
#       run the checks, then flash with --static-ip=TARGET_IP/PREFIX --gateway-ip=HOST_IP
# Optional: PREFIX (default 24), OTHER_SERIALS (space separated, default: the G40-0001 module serial)
#
# The host must allow TCP 20490 from TARGET_IP (the flash initrd mounts an NFS
# share on the host). Epiq's script builds its two Docker images itself and
# needs internet for that the first time.
set -euo pipefail

OTHER_SERIALS="${OTHER_SERIALS:-1612725641707}"

list_nvidia() {
    local d
    for d in /sys/bus/usb/devices/*; do
        [ -f "$d/idVendor" ] || continue
        [ "$(cat "$d/idVendor")" = "0955" ] || continue
        printf '%s %s:%s serial=%s product=%s\n' "$(basename "$d")" "$(cat "$d/idVendor")" \
            "$(cat "$d/idProduct")" "$(cat "$d/serial" 2>/dev/null || echo none)" \
            "$(cat "$d/product" 2>/dev/null || echo '?')"
    done
}

if [ "${1:-}" = "--probe" ]; then
    echo "NVIDIA USB devices on this host:"
    list_nvidia | sed 's/^/  /'
    echo "lsusb:"
    lsusb | grep -i 'ID 0955:' | sed 's/^/  /' || echo "  (none)"
    exit 0
fi

: "${BSP_DIR:?set BSP_DIR to the extracted BSP directory (contains epiq-flash-jetson-orin-nano-devkit.sh)}"
: "${HOST_IP:?set HOST_IP to the host address on the interface cabled to the X40}"
: "${TARGET_IP:?set TARGET_IP to the address the X40 Jetson should use}"
: "${APX_SERIAL:?set APX_SERIAL to the serial string shown by --probe for the X40 in recovery mode (or none)}"
PREFIX="${PREFIX:-24}"

[ -x "${BSP_DIR}/epiq-flash-jetson-orin-nano-devkit.sh" ] || {
    echo "ABORT: ${BSP_DIR}/epiq-flash-jetson-orin-nano-devkit.sh not found or not executable"; exit 1; }

mapfile -t nv < <(list_nvidia)
echo "NVIDIA USB devices: ${#nv[@]}"
printf '  %s\n' "${nv[@]}"
if [ "${#nv[@]}" -ne 1 ]; then
    echo "ABORT: expected exactly 1 NVIDIA USB device. Unplug every other Jetson (G40 USB-C, Orin Nano Super) from this host."
    exit 2
fi
dev="${nv[0]}"
if ! grep -q ' 0955:7323 ' <<<"${dev}"; then
    echo "ABORT: the device is not an Orin NX 16GB in recovery mode (expected 0955:7323)"
    exit 3
fi
serial="$(sed -n 's/.*serial=\([^ ]*\).*/\1/p' <<<"${dev}")"
for s in ${OTHER_SERIALS}; do
    if [ "${serial}" = "${s}" ]; then
        echo "ABORT: USB serial ${serial} is a known other Jetson (OTHER_SERIALS), not the X40"
        exit 6
    fi
done
if [ "${serial}" != "${APX_SERIAL}" ]; then
    echo "ABORT: USB serial '${serial}' does not match APX_SERIAL '${APX_SERIAL}'"
    exit 7
fi

if ! ip -br addr | grep -q " ${HOST_IP}/${PREFIX}"; then
    echo "ABORT: no host interface has ${HOST_IP}/${PREFIX}"
    exit 4
fi
if (exec 3<>"/dev/tcp/localhost/20490") >/dev/null 2>&1; then
    echo "ABORT: TCP 20490 on this host is in use (the flash NFS server needs it)"
    exit 5
fi

cd "${BSP_DIR}"
echo "Checks passed. Flashing the X40 Jetson (serial ${serial}) with static IP ${TARGET_IP}/${PREFIX}, gateway ${HOST_IP}."
exec "${BSP_DIR}/epiq-flash-jetson-orin-nano-devkit.sh" --static-ip="${TARGET_IP}/${PREFIX}" --gateway-ip="${HOST_IP}"
