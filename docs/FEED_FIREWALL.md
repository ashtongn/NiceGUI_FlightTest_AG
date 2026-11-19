# Opening the aircraft feed port on Linux and Windows

The ground station takes the aircraft position from a UDP multicast feed. The
laptop's firewall blocks that feed until its port is opened. This page gives
the steps for a Linux laptop and for a Windows laptop, the ground station
settings that go with them, and how to check each step.

The examples use the feed of the November 2026 flight test. Put your own
values in their place.

| What | Example | Where it comes from |
|---|---|---|
| Multicast group | 239.255.0.50 | the aircraft system's operator |
| UDP port | 12000 | the aircraft system's operator |
| Sender address | 10.167.0.100 | seen in the packets, see section 4 |
| Laptop adapter on the feed network | eth0, address 192.168.0.25 | `ip -br addr` on Linux, `ipconfig` on Windows |

Related pages: `docs/NETWORKING.md` (every port the system uses),
`docs/WINDOWS_CONSOLE.md` (the Windows ground station), `docs/MISSION_PLANNER.md`
section 7a (the Aircraft feed tab).

## 1. Linux

### 1.1 Is the firewall on?

```bash
sudo ufw status
```

"Status: active" means ufw is running and inbound traffic is blocked unless a
rule allows it. "Status: inactive" means ufw is not the cause; go to section 3.
A laptop with firewalld in place of ufw (Red Hat, Fedora, Rocky) answers
`sudo firewall-cmd --state` with "running"; use 1.3.

### 1.2 ufw (Ubuntu, Debian)

Open the port:

```bash
sudo ufw allow 12000/udp
```

The rule is kept over a reboot. To allow only the feed group on only the feed
adapter, use this in its place:

```bash
sudo ufw allow in on eth0 to 239.255.0.50 port 12000 proto udp
```

List the rules, and remove one that is no longer wanted:

```bash
sudo ufw status numbered
sudo ufw delete allow 12000/udp
```

### 1.3 firewalld (Red Hat, Fedora, Rocky)

```bash
sudo firewall-cmd --permanent --add-port=12000/udp
sudo firewall-cmd --reload
sudo firewall-cmd --list-ports
```

### 1.4 The sender is on another subnet

In the example the sender is 10.167.0.100 and the laptop's adapter is
192.168.0.25. Multicast does not need the two to be on one subnet, but Linux
drops a packet whose sender it has no route to. With Wi-Fi up the default
route covers it. With Wi-Fi off, add a route to the sender's subnet on the
feed adapter:

```bash
sudo ip route add 10.167.0.0/24 dev eth0
```

This route is lost at a reboot or when the cable is pulled. Add it again after
either.

### 1.5 Ground station in Docker

The `dpmg` container runs on the host's network, so the host's firewall rules
are the ones that count. Nothing extra is needed for the container.

## 2. Windows

Run these in a PowerShell or Command Prompt window opened with "Run as
administrator".

### 2.1 Open the port

PowerShell:

```powershell
New-NetFirewallRule -DisplayName "DPMG aircraft feed (UDP 12000 in)" -Direction Inbound -Action Allow -Protocol UDP -LocalPort 12000 -Profile Any
```

Or Command Prompt:

```bat
netsh advfirewall firewall add rule name="DPMG aircraft feed (UDP 12000 in)" dir=in action=allow protocol=UDP localport=12000 profile=any
```

`Profile Any` matters: a USB or dock Ethernet adapter is often on the Public
profile, and a rule for Private only does not cover it.

The same thing from the menus: Windows Defender Firewall with Advanced
Security, Inbound Rules, New Rule, Port, UDP, Specific local ports 12000,
Allow the connection, tick Domain, Private and Public, give it a name.

### 2.2 Check or remove the rule

```powershell
Get-NetFirewallRule -DisplayName "DPMG aircraft feed*" | Format-Table DisplayName, Enabled, Profile, Action
Remove-NetFirewallRule -DisplayName "DPMG aircraft feed (UDP 12000 in)"
```

### 2.3 The rule for the ground station program

`docs/WINDOWS_CONSOLE.md` section 4 sets up one rule by program for the radio
link. That rule lists the radio subnets in `remoteip`, so it does not pass a
feed whose sender is on another subnet (10.167.0.x in the example). The port
rule in 2.1 covers the feed whatever the sender's address is. Keep both.

### 2.4 Other things that block it on Windows

- Security or VPN software with its own firewall. Allow inbound UDP 12000 in
  that product as well, or the Windows rule changes nothing.
- A laptop managed by an organization may not let a local rule take effect.
  The rule is listed but the feed stays blocked; the administrator has to add
  it.

## 3. Ground station settings

Engineering view, tab Aircraft feed. Set the aircraft's fields and press
Listen:

| Field | Value | Note |
|---|---|---|
| group | 239.255.0.50 | |
| port | 12000 | |
| interface | 192.168.0.25 | the laptop's own address on the feed network |
| format | auto | reads JSON, NMEA and Cursor on Target XML; `xml` reads only the XML |

Two settings cause most of the trouble:

- interface `127.0.0.1` listens inside the laptop only. It is right for the
  test sender on the same laptop and wrong for a feed from the network.
  `0.0.0.0` joins on the adapter of the default route, which is often Wi-Fi
  and not the wired feed network. Give the adapter's address.
- The planner, the operator console and the header follow the first aircraft
  in the list unless a plan radio is bound to another. If the live feed is the
  second aircraft, the Aircraft feed tab shows it and the other screens do
  not. Remove the test aircraft, or make the live feed the only one.

## 4. Check it

1. Aircraft feed tab: the rx count goes up about once a second and the parsed
   fix shows the position. The header shows `ACFT`, the position and `(mcast)`.
2. Linux, with the tab listening, see the packets on the wire:

   ```bash
   sudo tcpdump -ni eth0 udp port 12000 -c 3
   ```

   Each line gives the sender and the group, as
   `10.167.0.100.54250 > 239.255.0.50.12000`. tcpdump reads the adapter before
   the firewall does. Packets here and none in the tab means the firewall rule
   or the interface field.
3. Linux, see what the firewall dropped in the last five minutes:

   ```bash
   journalctl -k --since "-5 min" | grep "UFW BLOCK" | grep "DPT=12000"
   ```

   A line with `DST=239.255.0.50` and `DPT=12000` is the feed being blocked.
   The line also gives the real port, which is how a wrong port number is
   found. Nothing is logged until something on the laptop has joined the
   group, so press Listen first.
4. Windows: Wireshark on the feed adapter with the filter `udp.port == 12000`
   shows the same packets.

## 5. If it still does not show

| What is seen | Likely cause | What to do |
|---|---|---|
| No packets in tcpdump or Wireshark | wrong adapter, cable, or the feed is not being sent | check the adapter's link, ask the aircraft system's operator |
| Packets on the wire, rx count stays at 0 | firewall rule missing, wrong port, or interface field | sections 1 or 2, then section 3 |
| rx count goes up, no fix | format field set to the wrong format | set format to auto |
| Fix in the Aircraft feed tab only | the live feed is not the first aircraft | section 3 |
| Works with Wi-Fi on, stops with Wi-Fi off (Linux) | no route to the sender's subnet | section 1.4 |
| Shows for a few minutes, then stops | the switch stopped sending the group because the laptop did not answer its membership query | on ufw add `sudo ufw allow in proto igmp`, on firewalld `sudo firewall-cmd --permanent --add-protocol=igmp` and reload |
| Feed time far from the console clock | the aircraft system's clock is not on UTC | no action for position; the feed time in the logs is the aircraft system's time |

## 6. What has been run

The ufw steps, the interface and first aircraft settings, and the checks in
section 4 were run on an Ubuntu ground station laptop on 2026-11-17 with the
239.255.0.50 port 12000 feed. The firewalld, Windows and membership query
commands are the standard ones for those systems and have not been run
against this feed.
