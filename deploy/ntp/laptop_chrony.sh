#!/bin/bash
# Make this laptop (the ground station) the time source for the radios on its
# USB and LAN links. chrony follows internet NTP when the laptop has it and
# hands out its own clock (stratum 10) when it does not, so a time set by hand
# on the laptop still reaches the radios. systemd-timesyncd cannot serve time,
# which is why chrony replaces it here. Run with sudo; safe to rerun.
#   sudo deploy/ntp/laptop_chrony.sh
# SUBNETS: the radio links to serve (USB boards and the G40 LAN by default).
set -e
SUBNETS="${SUBNETS:-192.168.55.0/24 192.168.56.0/24 192.168.0.0/24}"
export DEBIAN_FRONTEND=noninteractive
apt-get install -y chrony
mkdir -p /etc/chrony/conf.d
{
  echo "# DPMG: serve time to the radios (written by deploy/ntp/laptop_chrony.sh)"
  for s in $SUBNETS; do echo "allow $s"; done
  echo "local stratum 10"
  echo "makestep 1 3"
} > /etc/chrony/conf.d/dpmg.conf
systemctl enable --now chrony >/dev/null 2>&1 || systemctl enable --now chronyd
systemctl restart chrony 2>/dev/null || systemctl restart chronyd
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  for s in $SUBNETS; do
    ufw allow from "$s" to any port 123 proto udp comment "dpmg ntp" >/dev/null
  done
fi
sleep 2
chronyc tracking | sed -n '1,4p'
echo "chrony is serving $SUBNETS"
