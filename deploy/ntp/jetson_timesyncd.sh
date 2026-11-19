#!/bin/bash
# Point this radio host's clock at the ground station laptop. Works on the
# Orin Nano Super boards (USB link) and on the G40 (give the laptop's LAN
# address). The internet pool stays as the fallback for when the laptop is
# not there. Run with sudo on the radio host; safe to rerun.
#   sudo deploy/ntp/jetson_timesyncd.sh [laptop ip]
# Default: the router of the USB link (192.168.55.100 or 192.168.56.100).
set -e
LAPTOP="${1:-$(ip route show dev l4tbr0 2>/dev/null | awk '/default/ {print $3; exit}')}"
if [ -z "$LAPTOP" ]; then
  echo "no laptop address found on the USB link: pass it as the argument"
  exit 1
fi
mkdir -p /etc/systemd/timesyncd.conf.d
cat > /etc/systemd/timesyncd.conf.d/dpmg.conf <<CONF
# DPMG: the ground station laptop is the time source, the pool the fallback
# (written by deploy/ntp/jetson_timesyncd.sh)
[Time]
NTP=$LAPTOP
FallbackNTP=ntp.ubuntu.com
PollIntervalMinSec=32
PollIntervalMaxSec=256
CONF
systemctl restart systemd-timesyncd
sleep 4
timedatectl timesync-status | sed -n '1,6p'
echo "time source: $LAPTOP"
