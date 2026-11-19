# Matchstiq G20/G40 BSP Flash: Additional Steps

These notes add to the Epiq README that ships with the BSP
(`g20g40-bsp-v1.3.0-README.md`). Follow the Epiq README for the overall
procedure. The items below cover the problems that come up when flashing over a
direct Ethernet cable with no DHCP server, and a few checks that make the flash
safer.

Placeholders used below:

| Placeholder | Meaning | Example |
|---|---|---|
| `<BSP_DIR>` | Extracted BSP directory containing `epiq-flash.sh` | `~/G40_UPDATE/g20g40_61_..._flash` |
| `<HOST_IFACE>` | Host network interface cabled to the G20/G40 | `eth0`, `enx001122334455` |
| `<HOST_IP>` | Address you assign to the host on that interface | `192.168.10.1` |
| `<TARGET_IP>` | Address the G20/G40 will use during and after the flash | `192.168.10.2` |

`<HOST_IP>` and `<TARGET_IP>` must be on the same subnet (a /24 is used below).

## 1. Before flashing

1. Back up everything you need from the unit. The flash returns it to factory
   defaults, including the home directory, the Docker images and the network
   settings.
2. Verify the archive checksum as described in the Epiq README. After
   extracting, you can also check the individual images. Each line of
   `tools/kernel_flash/images/internal/flash.idx` and
   `tools/kernel_flash/images/external/flash.idx` ends with the SHA-1 of the
   image it names. `images/external/system.img` has a matching
   `system.img.sha1sum`.
3. Disconnect any other NVIDIA Jetson from the host's USB ports. The flash
   script uses NVIDIA's `--massflash 1` option, which flashes the first Jetson
   it finds in recovery mode and does not check its serial number.
   `guarded_flash.sh` (section 6) checks for this before it starts.

## 2. Connections

| Cable | Unit port | Notes |
|---|---|---|
| USB-C | Front panel USB-C | Connect directly to a host USB port, not through a dock or hub. Required for the flash. |
| Micro-USB | CONSOLE | Serial console. Shows up on the host as `/dev/ttyUSB0` or similar. |
| Ethernet | Ethernet | Flash images are transferred over this link. |

USB IDs the unit presents on the host (`lsusb | grep 0955`):

| USB ID | State |
|---|---|
| `0955:7020` | Booted normally |
| `0955:7323` (Orin NX 16GB) or `0955:7423` (Orin NX 8GB) | Forced recovery, ready to flash |
| `0955:7035` | Running the flash environment (during a flash, or after a failed one) |

## 3. Serial console

```bash
sudo screen /dev/ttyUSB0 115200
```

The first password prompt is for `sudo` on the host. Press Enter to get the
unit's login prompt. Exit `screen` with Ctrl-A, then K, then Y.

Before typing a command meant for the unit, check that the prompt shows the
unit's hostname (`sidekiq@matchstiq-g40-...`). A `reboot` typed into a host
terminal reboots the host.

## 4. Entering forced recovery

1. Confirm the USB-C cable is connected between the unit and the host. If it
   is not connected, the command below performs a normal reboot instead.
2. From the unit's serial console, logged in as `sidekiq`:
   ```bash
   sudo reboot --force forced-recovery
   ```
3. The console stops after the shutdown messages. `tegra234-cbb` /
   `TIMEOUT_ERR` messages during shutdown are expected.
4. On the host, `lsusb | grep 0955` should show `7323` or `7423`.

If a flash fails, the unit is left at `0955:7035`. Power-cycle it, log in on the
console, and repeat these steps before trying again. A failure before the image
write starts leaves the existing OS intact.

## 5. Flashing without a DHCP server

`./epiq-flash.sh` with no options requires a DHCP server on the Ethernet
segment. On a direct cable, use static addressing instead.

1. Find the host interface connected to the unit. It is the one that shows
   `LOWER_UP` in `ip -br link` while the cable is connected. USB Ethernet
   adapter names can change after a host reboot, so check this each time.
2. Give it an address (temporary, cleared on reboot):
   ```bash
   sudo ip addr add <HOST_IP>/24 dev <HOST_IFACE>
   ```
3. Apply the fix in section 5.1, open the firewall as in section 5.2, then
   flash:
   ```bash
   cd <BSP_DIR>
   ./epiq-flash.sh --static-ip=<TARGET_IP>/24 --gateway-ip=<HOST_IP>
   ```

### 5.1 Fix: static-IP mode stops at a `ping` error

Symptom in the flash log, right after `Configuring eth0 for static IP`:

```
ping: error while loading shared libraries: libidn2.so.0: cannot open shared object file: No such file or directory
Cleaning up...
```

After it sets the static address, `tools/kernel_flash/l4t_network_flash.func`
pings the gateway. The comment above that line says ping failures should be
ignored, but the command is not guarded, and `ping` in the flash environment is
missing a library. DHCP mode does not run this line.

Make the ping non-fatal. The original is kept as `.orig-epiq`:

```bash
cd <BSP_DIR>/tools/kernel_flash
cp l4t_network_flash.func l4t_network_flash.func.orig-epiq
sed -i 's|^\(\s*sshpass .* ping -c 5 "\${gateway_ip}"\)$|\1 \|\| true|' l4t_network_flash.func
diff l4t_network_flash.func.orig-epiq l4t_network_flash.func
```

The diff should show exactly one changed line, ending in `|| true`.

### 5.2 Fix: NFS mount times out (host firewall)

Symptom in the flash log:

```
mount.nfs: trying text-based options 'port=20490,nolock,vers=4.2,addr=<HOST_IP>,...'
mount.nfs: Connection timed out
Error: Flash failure.
```

During the flash, the unit connects back to an NFS server on the host, on TCP
port 20490, and a host firewall blocks this. The flash script's own firewall
check runs inside a container and reports "UFW is not installed" even when the
host has UFW enabled, so do not rely on that message.

Open the port for the unit's address only, and close it after the flash.

Linux, UFW (Ubuntu):

```bash
# before flashing
sudo ufw allow from <TARGET_IP> to any port 20490 proto tcp comment 'BSP flash NFS'
# after flashing
sudo ufw delete allow from <TARGET_IP> to any port 20490 proto tcp
sudo ufw status numbered
```

Linux, firewalld (RHEL, Rocky, Fedora):

```bash
# before flashing (runtime rule, removed on reload or reboot)
sudo firewall-cmd --add-rich-rule='rule family="ipv4" source address="<TARGET_IP>" port port="20490" protocol="tcp" accept'
# after flashing
sudo firewall-cmd --remove-rich-rule='rule family="ipv4" source address="<TARGET_IP>" port port="20490" protocol="tcp" accept'
```

Linux, iptables:

```bash
sudo iptables -I INPUT -p tcp -s <TARGET_IP> --dport 20490 -j ACCEPT   # before
sudo iptables -D INPUT -p tcp -s <TARGET_IP> --dport 20490 -j ACCEPT   # after
```

Windows: the flash tools need a Linux host with Docker. If that Linux host is
WSL2 or a virtual machine on a Windows PC, Windows Defender Firewall must also
allow the connection. From an administrator PowerShell:

```powershell
# before flashing
New-NetFirewallRule -DisplayName "BSP flash NFS" -Direction Inbound -Protocol TCP `
  -LocalPort 20490 -RemoteAddress <TARGET_IP> -Action Allow -Profile Any
# after flashing
Remove-NetFirewallRule -DisplayName "BSP flash NFS"
```

WSL2 and virtual machines also need the unit's USB device passed through (it
changes ID during the flash: 7323, then 7035, then 7020), and bridged or
mirrored networking so the unit can reach the host. This setup has not been
tested with this BSP. Use a native Linux host.

### 5.3 Flashing over USB only

With no Ethernet cable at all, `./epiq-flash.sh --usb` runs the normal NVIDIA flash
over the USB-C link. The unit is `fc00:1:1:0::2` and the host `fc00:1:1:0::1` during
the flash, and the unit mounts the host's NFS server on TCP 20490 over that link, so
open the port for that prefix instead of a target address:

```bash
# before flashing
sudo ufw allow from fc00:1:1::/48 to any port 20490 proto tcp comment 'BSP flash NFS'
# after flashing
sudo ufw delete allow from fc00:1:1::/48 to any port 20490 proto tcp
```

It is slower than a gigabit cable. The ping fix in 5.1 does not apply (USB mode skips
the Ethernet setup). After the flash the unit has no static address; set it as in
section 8, logged in over USB at `192.168.55.1`.

## 6. Optional: guarded_flash.sh

`deploy/g40/guarded_flash.sh` runs these checks, then calls `epiq-flash.sh` in
static-IP mode. It stops at the first check that fails.

- `<BSP_DIR>/epiq-flash.sh` exists and is executable.
- Exactly one NVIDIA device is on the host USB, and it is an Orin NX in
  recovery (`7323` or `7423`).
- The host has `<HOST_IP>/24` on some interface (set `PREFIX` for another
  prefix length).
- Both flash Docker images are present:
  `docker.epiq.rocks/jetson-linux-bsp-flash:l4t-R36-5-0-v6` and
  `docker.epiq.rocks/epiq-nfs-ganesha:v2`.

Run it from `deploy/g40/`:

```bash
BSP_DIR=<BSP_DIR> HOST_IP=<HOST_IP> TARGET_IP=<TARGET_IP> ./guarded_flash.sh
```

For a USB-only flash (section 5.3), `USB=1` replaces `HOST_IP` and `TARGET_IP` and
skips the host address check:

```bash
BSP_DIR=<BSP_DIR> USB=1 ./guarded_flash.sh
```

## 7. Expected output

A successful run ends with:

```
SSH ready
Configuring eth0 for static IP <TARGET_IP>/24 via <HOST_IP>
Jetson eth0 IP: <TARGET_IP>
...
Info: Successfully flashed the external device.
Flash is successful
***** Done flashing Jetson BSP *****
```

The image write takes about 7 minutes on a gigabit link. The unit then reboots,
the console shows `System firmware version 36.5.0`, and it boots to an
`Ubuntu 22.04.5 LTS` login prompt.

## 8. After flashing

The unit comes up with factory settings: user `sidekiq` (password in the Epiq
README), DHCP plus the fallback address `192.168.40.101/24` on `enP8p1s0`, and
the 15W power mode. While the USB-C cable is connected, the unit can also be
reached at `192.168.55.1` over USB, with the host at `192.168.55.100`.

1. Set a static address, if you are not using DHCP (unit console):
   ```bash
   sudo nmcli con add type ethernet ifname enP8p1s0 con-name static-eth \
     ipv4.method manual ipv4.addresses <TARGET_IP>/24 ipv6.method link-local \
     connection.autoconnect yes connection.autoconnect-priority 10
   sudo nmcli con up static-eth
   ```
2. If you had SSH access to the unit before the flash, the host key has changed.
   On the host:
   ```bash
   ssh-keygen -R <TARGET_IP>
   ssh-copy-id sidekiq@<TARGET_IP>
   ```
3. Verify the install:
   ```bash
   head -n 1 /etc/nv_tegra_release
   systemctl --failed --no-pager
   ~/sidekiq_image_current/prebuilt_apps/test_apps/version_test --full
   ```
4. FPGA updater (Epiq README step 6): check which NV100 image the updater
   carries, and compare it with what `version_test` reports for each card. If
   they match, the updater has nothing to change.
   ```bash
   sh ./sidekiq_hardware_updater_for_v4.25.0.sh --list | grep nv100 | grep -v golden
   ```
5. Optional: MAXN power mode, `sudo nvpmodel -m 0`, then reboot when prompted.
6. Optional, for GPU containers, register the NVIDIA runtime with Docker:
   ```bash
   sudo nvidia-ctk runtime configure --runtime=docker
   sudo systemctl restart docker
   ```
7. Remove the firewall rule added in section 5.2.
