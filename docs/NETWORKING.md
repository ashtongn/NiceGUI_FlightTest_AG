# DPMG networking

This page lists everything the system puts on the wire, what each host needs
open, and what tends to break when the laptop, the switch or the subnet
changes. Addresses below are the bench defaults from `dpmg/deploy_config.py`.
Override any of them with the matching `DPMG_*` environment variable or with
`.env`. Radio and console scripts are quoted where they exist, so this page
does not drift from them.

Related pages: `docs/DEPLOYMENT.md` (running the two roles),
`docs/QUICKSTART.md` (first bring-up, Windows firewall),
`docs/MISSION_PLANNER.md` (clocks appendix), `docs/WINDOWS_CONSOLE.md` (the
Windows console in detail), and in the Super Nano tree
`docs/B200_ORIN_BRINGUP.md` (USB link).

## 1. Who talks to whom

There are three kinds of host:

- The console (ground station). This is the x86 laptop running the GUI, either
  in the `dpmg` Docker container with `--network host` or natively
  (`launch_gui.sh --native`, or `launch_gui.ps1` on Windows). It serves the web
  pages and holds the UDP link to every radio. It is also the radios' time
  source (chrony) and the ssh client that deploys code.
- Radio hosts. Each radio has a Jetson running one radio service
  (`dpmg_service.py`) in a privileged `--network host` container. The G40 and
  the X40 are Jetsons with Epiq cards inside. The Super Nano boards carry a USB
  radio (B200, HackRF, Lime) and run one service per radio.
- Position source: whatever sends aircraft fixes to the multicast group. It is
  not part of the system, but the GUI listens for it.

Only the browser talks to the console over TCP. Everything between the
console and the radios is UDP, one message per datagram, under 1400 bytes
(`MAX_DATAGRAM` in `dpmg/link/proto.py`), so it crosses any Ethernet or USB
link without fragmentation.

## 2. Ports and protocols

| Flow | Proto | Port | Direction | Notes |
|---|---|---|---|---|
| GUI web page | TCP | 8080 | browser to console | NiceGUI over HTTP plus a websocket (socket.io) on the same port. `launch_gui.ps1 -Port N` or `launch_gui.sh --native --port N` change it; the Docker GUI is always 8080. Operator view `/`, engineering `/engineering`, planner `/mission`. |
| Second simulator GUI | TCP | 8082 | browser to console | A second simulated radio and GUI for development. It uses UDP 47832 (command) and 47843 (discovery), so it never collides with the live GUI. |
| ANNOUNCE | UDP | 47811 | radio to console | Every 2 s. A 20 byte payload (serial 16 bytes, host type, service version, command port 2 bytes) after the 8 byte header. Sent to the service's `--broadcast` address: `255.255.255.255`, a subnet broadcast, or the console's unicast address. The GUI binds `0.0.0.0:47811` with `SO_REUSEPORT`, so a native development GUI and the Docker GUI can overlap. `--discovery-port` and `DPMG_DISCOVERY_PORT` change it on both ends. |
| Command port | UDP | 47810 (G40, X40); on a Super Nano 47812 (B200), 47810 (HackRF), 47814 (Lime) | console to radio | `HELLO`, `CMD`, `DETECTION_REQ` and `FILE_*` arrive here. Each service on one host needs its own `--cmd-port`. ANNOUNCE carries it, so the GUI never guesses. |
| Replies | UDP | ephemeral (49152 to 65535) on the console | radio to console | `HELLO_ACK`, `STATUS` (1 Hz), `PSD_FRAME` (4 Hz), `DETECTION`, `WATCH_UPDATE`, `SEALED_FRAME`, `CMD_ACK` and `FILE_ACK` all go back to the address and port the GUI sent `HELLO` from. A stateful firewall treats them as the reply half of a flow the console opened, so Linux needs no inbound rule. Windows needs a per-program rule (section 5). |
| Position feed | UDP multicast | 239.2.1.1:5005 (default, set in the Aircraft position panel) | source to console | NMEA or the project's own fix format. The GUI joins the group on the interface picked in the panel. The console needs an inbound allow. |
| NTP | UDP | 123 | radio to console | chrony on the console serves 192.168.55.0/24, 192.168.56.0/24 and 192.168.0.0/24 (`deploy/ntp/laptop_chrony.sh`). Each Jetson runs systemd-timesyncd pointed at the console (`deploy/ntp/jetson_timesyncd.sh`). |
| set_time | UDP | the radio's command port | console to radio | The planner's Sync radio clocks button. A `CMD` that steps the radio host's clock to the console's, with no NTP involved. Needs the privileged container (CAP_SYS_TIME). |
| ssh / rsync | TCP | 22 | console to radio | Deploy (`deploy/push_update.sh`, `deploy.sh`), logs, Docker control. Users: `sidekiq` on the G40 and X40, `dpmg` on the Super Nanos. |
| Jetson USB link DHCP | UDP | 67/68 | Jetson to laptop | The Jetson's `l4tbr0` bridge gives the laptop `192.168.55.100` (or `.56.100`) when the USB-C cable is plugged in. |
| Bitpipe data plane | TCP | 47899 | console to radio | Only while a bitpipe link runs (`bitpipe_start`). The stream modem in the radio service listens and accepts only the address given in `allow_from`. The console's container dials out to it, so the console needs no inbound rule. The container's own faces (control 47898, chat 47901, file 47902) bind 127.0.0.1 on the console and are not on the wire. See docs/BITPIPE.md. |
| GPU channelizer | none | | | `dpmg-gpu` talks to the service through POSIX shared memory (`--ipc=host`), not the network. |

Not on the network at all: GPIO front end control, the USB attenuator, the
GPS antenna bias (i2c on the Jetson) and libsidekiq.

### How a radio appears in the GUI

1. The service sends ANNOUNCE every 2 s to its `--broadcast` address.
2. The GUI hears it on 47811, keys the radio by serial (never by IP), and
   sends `HELLO` to the announced address and command port.
3. `HELLO_ACK` checks the protocol version (`proto.VERSION`, 3). The GUI
   refuses a mismatch and logs it, so the console and the radios must run the
   same `dpmg/link/proto.py`.
4. From then on STATUS at 1 Hz is the heartbeat. After 3.5 s without STATUS
   the GUI marks the link stale. The radio's next ANNOUNCE reopens it.

Connect by IP (engineering view, SDR tab, Remote Radios section) skips step 1:
the GUI sends HELLO to an address you type. While a radio added this way is
stale, the GUI sends it a HELLO every 2 s. The address is saved in
`output/saved_radios.json` beside the code (in the container,
`/app/output`, bind-mounted from the repo's `output/`) and dialled again at
every GUI start. Only the main GUI on discovery port 47811 saves the list;
`DPMG_SAVED_RADIOS` sets another file, or an empty value turns saving off.
Use this path when ANNOUNCE cannot reach the console: a different subnet, a
firewall, or a switch that drops broadcast. Both paths end in the same link.

More than one console can hold a link to one radio at the same time.
Telemetry fans out to every address that sent a valid HELLO. Commands are
absolute setpoints with a `cmd_id`, so a duplicate is harmless. Two operators
changing the same radio see each other's settings.

## 3. The bench layout

| Host | Interface | Address | Notes |
|---|---|---|---|
| Laptop (console) | eth0 | 192.168.0.25/24, static | The G40 announces only to this address, so the laptop must be .25 or the service's `--broadcast` must change. |
| Laptop | Wi-Fi | DHCP from the building network | Default route and internet. DPMG does not use it. |
| Laptop | USB-C NCM (`enx...`) | 192.168.55.100/24, from the Nano's DHCP | Present only while the Nano's USB-C cable is in. |
| G40 | enP8p1s0 | 192.168.0.22/24, static | The service announces to 192.168.0.25, command port 47810. |
| G40 | l4tbr0 | 192.168.55.1/24 | The Jetson's own USB device mode bridge, normally down. It has the same address as the Nano's USB link, so only one Jetson may be on the laptop's USB at a time. |
| X40 | | 192.168.0.26/24 | Command port 47810, when it is on the switch. |
| Super Nano "mothball" | enP8p1s0 | 192.168.0.30/24, static (NM profile `lab-switch`, no gateway) | B200 service command port 47812, announces to 192.168.55.100. |
| Super Nano "mothball" | l4tbr0 (USB) | 192.168.55.1/24 | ssh `dpmg@192.168.55.1`. |
| Super Nano "mothball2" | l4tbr0 (USB) | 192.168.56.1/24 | ssh `mothball2`. |

The lab switch has no DHCP server. Every wired host is static, so a new laptop
or a re-imaged Jetson shows up with no address until one is set (section 4).

Multicast position feed: 239.2.1.1:5005 on the wired LAN, with the GUI's
Interface selector set to 192.168.0.25.

## 4. Setting up a new console or a new network

### A new Linux laptop

1. Address. Give the wired port a static address on the radio subnet, for
   example 192.168.0.25/24 with no gateway, so the Wi-Fi keeps the default
   route. If you pick a different address, every radio that announces by
   unicast must be told (see the radio host steps below).

   ```bash
   nmcli con add type ethernet ifname eth0 con-name lab-switch ipv4.method manual ipv4.addresses 192.168.0.25/24
   nmcli con up lab-switch
   ```

2. Docker and the GUI image. Build the image once with
   `docker build -t dpmg .`. Then `./launch_gui.sh` creates and runs the `dpmg`
   container with `--network host` and mounts `~/.dpmg`. Host networking is
   required: in the default bridge mode the GUI cannot hear ANNOUNCE and the
   radios cannot reach its reply port.
3. Firewall. With ufw active (the default on Ubuntu desktops), allow:

   ```bash
   sudo ufw allow from 192.168.0.0/24 to any port 47811 proto udp comment "dpmg announce"
   sudo ufw allow in on eth0 to any port 5005 proto udp comment "dpmg position feed"
   ```

   Connection tracking lets in the replies to the GUI's own HELLO, so the
   reply port needs nothing. If a second console or simulator uses another
   discovery port, allow that one too. `sudo ufw status numbered` shows the
   rules and `sudo ufw delete <n>` removes one.
4. Time. `sudo deploy/ntp/laptop_chrony.sh` installs chrony, allows the radio
   subnets, opens UDP 123 in ufw for them, and sets `local stratum 10` so the
   laptop serves its own clock when it has no internet.
5. ssh to the radios. Put the deploy key in `~/.ssh` and add a Host block per
   radio to `~/.ssh/config` with `IdentitiesOnly yes`, for example:

   ```text
   Host 192.168.0.22 g40
       HostName 192.168.0.22
       User sidekiq
       IdentityFile ~/.ssh/jetson_b200
       IdentitiesOnly yes
   ```

   Without `IdentitiesOnly`, an agent holding other keys offers them first.
   Each connection can then take a minute, and `deploy/push_update.sh` looks
   hung.
6. Check. `ping` each radio, open http://localhost:8080, and watch the radios
   strip fill within a few seconds. If a radio is missing, use Connect by IP
   with its address and command port. If that connects but STATUS stays
   stale, a firewall is dropping the replies (section 6).

### A new Windows laptop

`launch_gui.ps1` (or `start_console.bat` for the GUI plus the planner) runs
the GUI natively. Windows Defender Firewall must pass two inbound UDP flows to
the venv's `python.exe`: ANNOUNCE on 47811 and the replies on the ephemeral
port. Allow by program, from an administrator prompt:

```bat
netsh advfirewall firewall add rule name="DPMG GUI (UDP in)" dir=in action=allow protocol=UDP program="C:\DPMG\NiceGui\.venv\Scripts\python.exe" remoteip=192.168.0.0/24
```

Allowing the "Windows Security Alert" at first start on Private networks does
the same. Public profile rules are usually blocked by policy, so make sure
the wired adapter is classed Private. Windows is not the radios' NTP source;
use Sync radio clocks (or Set time by hand) from the planner. The Jetson USB
link shows up as two adapters on Windows, and the NCM one is live. Full
detail: `docs/WINDOWS_CONSOLE.md`.

### A new or re-addressed radio host

- Static address on the switch, since the switch has no DHCP. On a Jetson with
  NetworkManager, use the line below, with no gateway (the USB link, or
  nothing, is the default route):

  ```bash
  sudo nmcli con add type ethernet ifname enP8p1s0 con-name lab-switch ipv4.method manual ipv4.addresses 192.168.0.30/24
  ```

- The service arguments decide how the console finds the radio
  (`docs/DEPLOYMENT.md` has the full `docker run`):
  - `--serial`: the radio's permanent identity. The GUI keys on it, so the IP
    may change freely.
  - `--cmd-port`: unique per service on one host.
  - `--discovery-port 47811`: must match the console.
  - `--broadcast`: `255.255.255.255` survives any subnet change, but some
    switches and all routers drop it. The console's unicast address is the
    dependable choice on a managed network, and the G40 uses it. The arguments
    are fixed when the container is created, so changing them means
    recreating it. On the G40 (JetPack 6) run `deploy/radio/g40_up_r36.sh`
    again; no RT grant is needed. On the X40 (JetPack 5) the RT budget must be
    granted again in the same step or TX dies (`docs/DEPLOYMENT.md`).
  - `DPMG_ALLOW`: optional comma separated IPs or CIDRs. When it is set, the
    service drops every control datagram from anywhere else. A new console
    address must be added, or the radio ignores it without any error.
- `--network host` and `--privileged` on the container, as on the bench
  radios.
- Time: `sudo deploy/ntp/jetson_timesyncd.sh <console ip>` points the host at
  the console, with the internet pool as the fallback.
- ssh: the console's public key in the radio user's `authorized_keys`.
- A Super Nano reached over USB-C needs nothing. The Jetson runs DHCP on
  `l4tbr0` and the laptop gets `.100`. Only one Jetson at a time can be on the
  laptop's USB, because every Jetson uses 192.168.55.1 for that bridge. The
  second Super Nano uses 192.168.56.1 for this reason.

### Changing the subnet

Change these, in this order:

1. each radio's static address;
2. each service's `--broadcast`, if it is unicast;
3. the console's address;
4. the `dpmg/deploy_config.py` defaults, or the `DPMG_G40_HOST`,
   `DPMG_X40_HOST` and `DPMG_GUI_HOST` variables (the deploy scripts and bench
   tools read them);
5. the ufw rules that name the old subnet (`ufw status numbered`);
6. the `allow` lines in `/etc/chrony/conf.d/dpmg.conf` (or run
   `laptop_chrony.sh` again with `SUBNETS=...`);
7. the saved Connect by IP entries (`output/saved_radios.json`) and any
   mission plan whose radios carry an `address`. The plan binder maps a
   planned radio to a live serial, but only if the console can see the live
   radio.

## 5. Firewalls, in one place

Console, Linux (ufw): inbound UDP 47811 from the radio subnet; inbound UDP
5005 (or the feed's port) on the wired interface; UDP 123 from the radio
subnets (the chrony script adds it); TCP 8080 only if a browser on another
machine should reach the GUI. Nothing for the reply port.

Console, Windows: a per-program inbound UDP rule for the venv's `python.exe`
(or port rules for 47811 plus 49152 to 65535), Private profile.

Radio hosts: the Jetson images ship without a host firewall. The service
only needs its command port reachable, plus ssh. If a firewall is added, allow
inbound UDP on the command port from the console and TCP 22, and outbound UDP
to 47811, 123 and the console's ephemeral range.

Docker: host networking on both roles means the host firewall is the only
one that matters. Do not add `-p` port maps. They do nothing with
`--network host`, and they are a sign the container was started the wrong
way.

## 6. What blocks it, and how it shows

| Symptom | Usual cause | Check or fix |
|---|---|---|
| Radio never appears in the strip, but `ping` works | ANNOUNCE is not arriving: ufw or the Windows firewall on the console; the service `--broadcast` points at another address; a different `--discovery-port`; a managed switch or a router between console and radio dropping broadcast; the GUI container in bridge mode | `sudo tcpdump -ni eth0 udp port 47811` on the console shows whether the packets arrive at all. Fix the rule or the `--broadcast` target, or use Connect by IP. |
| Connect by IP connects, STATUS and PSD stay stale | Replies on the ephemeral port are blocked (Windows per-program rule missing; a VPN client that drops traffic outside the tunnel) | Add the program rule; pause the VPN or exclude the radio subnet. |
| Radio shows, then goes stale every few seconds | Two consoles on one discovery port is fine, but two services on one host with the same `--cmd-port` is not; or packet loss on a bad cable or a Wi-Fi hop | `ss -ulnp` on the radio host; keep radios on wired links. |
| Radio shows but refuses every command, with no error | `DPMG_ALLOW` is set on the service and does not include this console's address | Add the address and restart the service. |
| HELLO refused, "version" in the log | `dpmg/link/proto.py` differs between console and radio | Deploy the same tree to both. |
| Everything worked yesterday, nothing today | The console's wired address changed (DHCP appeared, or someone picked Wi-Fi only). The G40 announces by unicast to .25. | `ip -4 addr`; put the static address back. |
| Position feed never shows a fix | Inbound UDP 5005 blocked on the console; the GUI listening on the wrong interface (the panel defaults to 0.0.0.0, pick the wired address); the source on another VLAN; a managed switch with IGMP snooping and no querier | A plain receiver that joins the group on the wired address, with a sender on another host, proves the path without the GUI. Multicast sent from the console itself shows only on 127.0.0.1, never on its real interfaces, so a local test proves nothing about the rule. |
| Planner strip shows a clock skew that grows daily | The radio host has no reachable NTP source (it points at the internet pool with no internet) | Sync radio clocks now; `jetson_timesyncd.sh <console ip>` for a lasting fix. |
| Sync radio clocks says "no permission to set the clock" | The service container is not privileged (no CAP_SYS_TIME) | Run it as in DEPLOYMENT.md. |
| ssh to a radio takes a minute per connection | The agent offers other keys before the right one | `IdentitiesOnly yes` with the right `IdentityFile` in `~/.ssh/config`. |
| Deploy scripts talk to the wrong host | The `dpmg/deploy_config.py` defaults or `DPMG_*_HOST` were not updated for this network | Set the variables or `.env`. |
| Two Jetsons on the laptop's USB at once, one unreachable | Both use 192.168.55.1 on `l4tbr0` | One at a time, or re-address one bridge (mothball2 is on .56.1). |
| The G40 and the Nano see each other's ANNOUNCE | Harmless; only consoles act on ANNOUNCE | Nothing to do. |
| Laptop reachable over USB-C but the wired address is unreachable | Linux reverse path filtering (`rp_filter`) with two routes to the same box, or the Jetson's wired port has no address | One path per host; set the static address. |
| Telemetry fine, large command replies lost (file listings) | Receive buffer overrun while PSD streams; a network with an MTU under 1400 | The GUI already sets a 4 MB receive buffer; keep the MTU at 1500 on every hop. |

### Things that are fine and look wrong

- The laptop's default route is the Wi-Fi. DPMG never needs a gateway; all
  radio traffic stays on the wired subnet or the USB link.
- The G40's `l4tbr0` shows `192.168.55.1` with the link down. That is the
  Jetson's USB device mode bridge, idle unless a USB-C cable is in.
- The planner's radio chip says "clk internal" for a G40 that is on the GPSDO.
  That field is the reference clock as `gps_status` reports it. "ref LOCKED"
  on the same chip is the one that matters.
- "no GPS receiver" on a B200: it has none. Its OS clock is still shown and
  still syncs.

## 7. Quick checks from the console

Each line answers one question: is the console on the radio subnet, are the radios up,
are the 47811, 5005 and 123 rules present, is ANNOUNCE arriving, which radios
take time from the console, and is the GUI container on host networking (it must say
`host`).

```bash
ip -4 -br addr
ping -c1 192.168.0.22; ping -c1 192.168.55.1
sudo ufw status numbered
sudo tcpdump -ni eth0 udp port 47811 -c 3
sudo chronyc clients
docker inspect dpmg --format '{{.HostConfig.NetworkMode}}'
```

On a radio host, the first line shows the network mode and the service
arguments, and the second should name the console as the server:

```bash
docker inspect dpmg_svc --format '{{.HostConfig.NetworkMode}} {{join .Args " "}}'
timedatectl timesync-status | head -3
```
