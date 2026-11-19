#!/bin/bash
# Pre-flight checks, then run Epiq's epiq-flash.sh in static-IP mode.
#
# epiq-flash.sh uses NVIDIA's --massflash 1, which flashes the first Jetson in
# recovery mode on the host USB without checking its serial number. This script
# stops unless exactly one NVIDIA USB device is present and it is an Orin NX in
# recovery (0955:7323 16GB, 0955:7423 8GB).
#
# Usage:
#   BSP_DIR=<extracted BSP dir> HOST_IP=<host ip> TARGET_IP=<unit ip> ./guarded_flash.sh
#   BSP_DIR=<extracted BSP dir> USB=1 ./guarded_flash.sh
# Optional: PREFIX (default 24)
#
# USB=1 flashes over the USB-C link only (epiq-flash.sh --usb), with no Ethernet
# cable. The unit is fc00:1:1:0::2 and the host fc00:1:1:0::1 during the flash, so
# the host firewall must allow TCP 20490 from fc00:1:1::/48.
set -euo pipefail

: "${BSP_DIR:?set BSP_DIR to the extracted BSP directory (contains epiq-flash.sh)}"
USB="${USB:-0}"
if [ "${USB}" != "1" ]; then
    : "${HOST_IP:?set HOST_IP to the host address on the interface cabled to the unit}"
    : "${TARGET_IP:?set TARGET_IP to the address the unit should use}"
fi
PREFIX="${PREFIX:-24}"

[ -x "${BSP_DIR}/epiq-flash.sh" ] || { echo "ABORT: ${BSP_DIR}/epiq-flash.sh not found or not executable"; exit 1; }

mapfile -t nv < <(lsusb | grep -i 'ID 0955:' || true)
echo "NVIDIA USB devices: ${#nv[@]}"
printf '  %s\n' "${nv[@]}"
if [ "${#nv[@]}" -ne 1 ]; then
    echo "ABORT: expected exactly 1 NVIDIA USB device"
    exit 2
fi
if ! grep -qE 'ID 0955:(7323|7423)' <<<"${nv[0]}"; then
    echo "ABORT: device is not an Orin NX in recovery mode (expected 0955:7323 or 0955:7423)"
    exit 3
fi

if [ "${USB}" != "1" ] && ! ip -br addr | grep -q " ${HOST_IP}/${PREFIX}"; then
    echo "ABORT: no host interface has ${HOST_IP}/${PREFIX}"
    exit 4
fi

for img in docker.epiq.rocks/jetson-linux-bsp-flash:l4t-R36-5-0-v6 docker.epiq.rocks/epiq-nfs-ganesha:v2; do
    docker image inspect "${img}" >/dev/null 2>&1 || { echo "ABORT: missing Docker image ${img} (run build-jetson-linux-containers.sh)"; exit 5; }
done

cd "${BSP_DIR}"
if [ "${USB}" = "1" ]; then
    echo "Checks passed. Flashing over the USB link."
    exec ./epiq-flash.sh --usb
fi
echo "Checks passed. Flashing with static IP ${TARGET_IP}/${PREFIX}, gateway ${HOST_IP}."
exec ./epiq-flash.sh --static-ip="${TARGET_IP}/${PREFIX}" --gateway-ip="${HOST_IP}"
