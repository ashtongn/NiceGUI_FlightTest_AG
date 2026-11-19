# DPMG console on a Windows laptop

The console side of DPMG (the MOTHBALL FURY GUI, with the mission planner as
its /mission view) runs natively on a Windows x86 laptop with Python. The
radios stay where they are. The radio services run on the Jetson Orin Nano
Super boards (mothball and mothball2, reached over their USB-C links) and on
the G40 (Ethernet). Only the operator console moves, and nothing in this
document touches the radio hosts.

The console uses native Python, not Docker Desktop. Docker Desktop puts
containers in a Linux VM with no host networking, so UDP from the radios
never reaches a container. The Linux console uses Docker with host
networking; Windows runs the same source tree directly in a virtual
environment.

The console is one program. `run_gui_link.py` serves the operator view (/),
the engineering view (/engineering) and the mission planner (/mission) from
one process on one port. The planner works through the GUI's own radio link.

These files in the NiceGui repo make it work:

| File | What it does |
|---|---|
| `start_console.bat` | creates `.venv`, installs, starts the GUI, opens / and /mission |
| `requirements-windows.txt` | the console's Python packages (wheels only) |
| `launch_gui.bat` | the GUI alone (older script, still works) |
| `run_gui_link.py` | the GUI entry point the scripts call |
| `tools\maps\` | `vendor_map_assets.py` and `build_base_map.py` (the map store) |
| `tools\sim_position.py` | a test sender for the multicast position feed |

## 1. Layout on the laptop

Unpack the repo, for example to `C:\DPMG\NiceGui`. Nothing else is needed
beside it.

Per user state goes under the profile, in the same relative places as on
Linux:

| Path | Contents |
|---|---|
| `%USERPROFILE%\.dpmg\maps` | map store (`DPMG_MAP_STORE` overrides) |
| `%USERPROFILE%\.dpmg\mission_autosave.json` | the planner's autosaved plan |
| `C:\DPMG\NiceGui\output\saved_radios.json` | the GUI's saved radio addresses |
| `C:\DPMG\NiceGui\output\scenarios` | scenario files ("Disk" storage) |

## 2. First run on Windows

1. Install Python 3.11 or newer from python.org. Tick "Add python.exe to
   PATH" and leave "tcl/tk and IDLE" ticked. The GUI imports tkinter at load,
   and the start script warns when it is missing.
2. Open a Command Prompt in `C:\DPMG\NiceGui` and run:

   ```bat
   start_console.bat
   ```

   The first run creates `.venv` and installs `requirements-windows.txt`
   (NiceGUI 3.13, numpy, scipy, pyyaml). This needs internet or a wheel cache
   once. It then opens one window running the GUI on http://localhost:8080.
   When the GUI answers, the browser opens the operator view and the planner
   at http://localhost:8080/mission. The header links move between the
   operator, engineering and mission views. To stop the GUI, close the window
   or press Ctrl-C in it.
3. The "Windows Security Alert" for python.exe appears the first time the GUI
   binds a UDP socket. Tick Private networks and click Allow access. If the
   alert was dismissed, add the firewall rule by hand (section 4).
4. In the SDR tab, open the Remote Radios (Ground Link) section and use
   Connect by IP for each radio service (section 3 has the addresses). The
   address is saved in `output\saved_radios.json` and dialled again at every
   start. The planner lists the GUI's radios, so a radio connected once shows
   up in the SDR tab and in the planner's radio list.
5. Maps: from `C:\DPMG\NiceGui`, run these once with the venv's python:

   ```bat
   .venv\Scripts\python.exe tools\maps\vendor_map_assets.py
   .venv\Scripts\python.exe tools\maps\build_base_map.py
   ```

   The first fetches the renderer pieces and the Windows build of the pmtiles
   tool, `bin\pmtiles.exe`, into the map store. The second builds the world
   base. A store copied from the Linux box works too. Run
   `vendor_map_assets.py` after copying, so the Windows pmtiles.exe is fetched
   beside the Linux binary; it downloads only what is missing. Area downloads
   from the planner need internet on the laptop.

To run a second GUI beside the first, on web port 8081 and discovery port
47841 (planner on 8081/mission):

```bat
launch_gui.bat 8081 47841
```

Only the GUI on discovery port 47811 saves Connect by IP addresses, unless
`DPMG_SAVED_RADIOS` names a file, so the second GUI does not dial the main
GUI's radios.

## 3. Networking

### The Jetson USB link

In USB device mode, each Orin Nano Super presents two network functions over
one USB-C cable, so Windows shows two network adapters for one board: an
RNDIS one and a CDC-NCM one. Windows 11 24H2 no longer ships an RNDIS
driver, so the RNDIS adapter stays unconfigured. It shows a yellow mark in
Device Manager, which is harmless; disable it to keep the adapter list short.
The NCM adapter is the live link. The board's DHCP server gives the laptop:

| Laptop address | From | Board address |
|---|---|---|
| 192.168.55.100 | mothball | 192.168.55.1 |
| 192.168.56.100 | mothball2 | 192.168.56.1 |

Both boards can be plugged in at once because they use different subnets.
Check with `ipconfig`: the "USB NCM" or "UsbNcm Host Device" adapter carries
the .100 address. If it shows 169.254.x.x, the board's DHCP did not answer.
Either the board has not booted yet, or it has the dhcpd problem described in
docs/SECOND_SUPER_NANO.md in the Super Nano repo. Unplug and replug once the
board is up.

Set the USB adapter's network profile to Private (Settings, Network, the
adapter, Network profile type). Otherwise the firewall rule has to cover the
Public profile.

Internet sharing to the boards: on the Linux console the x86 does NAT for
the boards. Windows Internet Connection Sharing can do the same but is not
covered here. The radios do not need internet to run.

### Radio services and ports

The console reaches the radios by address, not by UDP discovery. The service
ports on a Jetson board are 47810 (HackRF), 47812 (B200), 47814 (LimeSDR)
and 47816 (Pluto). The G40 service listens on 47810 at its Ethernet address
(192.168.0.x, with the laptop on the same subnet).

Connect by IP sends a HELLO from the GUI's own ephemeral UDP port, called
its link port. The service replies to that port, and the telemetry and PSD
stream follow the same flow, so the firewall rule is by program, not by port
(section 4). Discovery, the service's ANNOUNCE to UDP 47811 on the laptop,
also works natively on Windows if the service is started with
`--broadcast <laptop address>` and 47811 is allowed. Nothing here depends on
it.

The planner opens no radio port of its own. The /mission view uses the GUI's
radio link, the same one the SDR tab uses. The GUI holds discovery port 47811
and its link port, and the planner keys and polls the radios through
them.

### The multicast position feed

The planner's location triggers, and the GUI header, listen to the aircraft
position feed on a multicast group (239.2.1.1:5005 by default, JSON or
NMEA). On Linux the listener binds all interfaces and joins the group on the
chosen interface. Windows delivers a multicast group to a socket bound to
INADDR_ANY only on the default route adapter, usually Wi-Fi, so a feed
arriving on the USB link or the G40 Ethernet would be missed. On Windows the
listener therefore binds the chosen interface address and joins the group on
it.

That address is the `iface` field on the planner's map bar. The GUI header's
feed settings have no interface field and use the default adapter. Pick the
laptop's own address on the adapter the feed arrives on:

- 192.168.55.100 or 192.168.56.100 for a feed from a board;
- the laptop's LAN address for a feed on the G40 network;
- 0.0.0.0 for the default adapter;
- 127.0.0.1 for `tools\sim_position.py` running on the laptop.

The field offers the laptop's addresses in a drop-down.

`tools\sim_position.py` plays a position along a path on that feed, to test
the triggers without an aircraft feed. From `C:\DPMG\NiceGui`:

```bat
.venv\Scripts\python.exe tools\sim_position.py --plan --iface 127.0.0.1
```

`--plan` follows the autosaved plan's flight path. `--points "lat,lon
lat,lon"` or `--geojson file` give a path directly, and `--help` lists the
rest.

### Clock

On the Linux console, chrony is the radios' time source
(`deploy/ntp/laptop_chrony.sh`). It serves the 192.168.55, 192.168.56 and
192.168.0 subnets at stratum 10 and follows internet NTP when there is one.
Each radio host points systemd-timesyncd at the laptop
(`deploy/ntp/jetson_timesyncd.sh`). A Windows console does not serve NTP in
this setup. Windows can act as an NTP server through the w32time NtpServer
provider, but that is not configured or tested here. The radios keep their
own time sources (GPS, or internet through the Linux box). The planner's "Set
the time by hand" dialog pushes a UTC time to every connected radio (the
service's set_time command), which does not depend on the console's
operating system.

"Set this console" in that dialog sets the Windows clock with PowerShell
Set-Date. PowerShell parses the time as UTC and converts it to local time,
so the laptop's time zone does not matter. Setting the clock needs an
administrator. When the console was started from a normal prompt, the
Windows administrator prompt (UAC) appears on the laptop's screen; click Yes.
The status line says when that happened. If the prompt is declined, the line
shows the command to paste into a PowerShell opened as administrator. To
resync the laptop with its configured internet time source instead, run this
as administrator:

```bat
w32tm /resync
```

`w32tm` cannot set an arbitrary time, which is why the dialog uses Set-Date.

## 4. Firewall rules

Windows Firewall blocks inbound UDP to python.exe until told otherwise. One
rule by program covers the service replies on the GUI's link port, the
ANNOUNCE on 47811 and the multicast feed on 5005. Run this as administrator,
with the laptop's own paths and subnets:

```bat
netsh advfirewall firewall add rule name="DPMG console (UDP in)" dir=in action=allow protocol=UDP program="C:\DPMG\NiceGui\.venv\Scripts\python.exe" remoteip=192.168.55.0/24,192.168.56.0/24,192.168.0.0/24 profile=any
```

For a feed whose sender is on another subnet, add that subnet to `remoteip`,
or drop `remoteip` for a rule that allows UDP from anywhere to this program.
If program rules are not allowed, open inbound UDP 47811, 5005 and 49152 to
65535 from the radio subnets instead. The planner runs inside the GUI
process, so the one program rule covers it. The default outbound policy
allows outbound traffic: the HELLO and commands to 47810 to 47816, NTP and
the multicast joins.

The multicast join itself (IGMP) needs no rule. If the feed never shows a
fix, check `iface` first (section 3), then the rule's profile. A USB adapter
on the Public profile is covered only by `profile=any` or by switching the
adapter to Private.

## 5. What is different from the Linux console

- No Docker and no container restart: the start script runs the tree in
  place. Edit a file, close the window, run the script again.
- Scenario storage: "Disk" is `output\scenarios`. The RAM (tmpfs) option is
  hidden unless `DPMG_SCENARIO_RAM_DIR` points at a folder that exists.
- The waveform deployment card's default .bin path is under `output\`.
- The console clock is set with Set-Date through the administrator prompt,
  not with passwordless sudo and chrony.
- The pmtiles tool is `bin\pmtiles.exe` in the map store. The lookup also
  accepts a Linux `pmtiles` beside it, and checks PATH first.

## 6. Check list for the first real Windows run

On Linux the platform branches could only be checked with sys.platform
patched (the tests in `tests/test_windows_console.py`). These steps confirm
them on a real laptop. Tick each one.

Install and start
1. [ ] `py -3 --version` prints 3.11 or newer; `py -3 -c "import tkinter"` prints nothing.
2. [ ] `start_console.bat` creates `.venv`, installs without error and opens
   one GUI window.
3. [ ] The browser opens http://localhost:8080 (the operator view) and
   http://localhost:8080/mission (the planner).
4. [ ] The header links move between the operator, engineering and mission
   views.

USB link and radios
5. [ ] With a board plugged in, `ipconfig` shows 192.168.55.100 (or
   .56.100) on the NCM adapter; the RNDIS adapter is unconfigured and
   ignored.
6. [ ] `ping 192.168.55.1` answers.
7. [ ] Connect by IP 192.168.55.1 port 47812 (B200, or the port of the
   service that is running) brings the radio card up with live STATUS, and a
   PSD once RX runs.
8. [ ] Close the GUI window and run `start_console.bat` again. The radio
   reconnects by itself from `output\saved_radios.json`, in the GUI and in
   the planner's radio list on /mission.
9. [ ] G40 over Ethernet: Connect by IP 192.168.0.x port 47810 works the
   same way.
10. [ ] No "Windows Security Alert" is still pending, and `netsh advfirewall
    firewall show rule name="DPMG console (UDP in)"` lists the rule (or the
    first start allow).

Multicast feed
11. [ ] Put the feed sender on a board, or run `tools\sim_position.py` on
    the laptop with `--iface 127.0.0.1`. Set `iface` on the planner's map bar
    to the laptop's address on that adapter (127.0.0.1 for the local sender)
    and switch "listen to the aircraft feed" on. The position line fills
    within a few seconds.
12. [ ] Switch `iface` to 0.0.0.0 with the sender on the USB link. If the
    position goes stale, that is the Windows bind rule (expected when
    Wi-Fi is the default route). Set it back.

Clock
13. [ ] From a start without elevation, use Set the time by hand, Set this
    console. The UAC prompt appears; click Yes. The line says "console clock
    set ... (the Windows administrator prompt was shown for it)" and the
    console clock chip updates.
14. [ ] Decline the prompt once. The line shows the Set-Date command to
    paste into an administrator PowerShell, and the clock is unchanged.
15. [ ] Set the radios. The radio clock chips go green against the console
    within a poll.

Maps
16. [ ] `tools\maps\vendor_map_assets.py` on the laptop reports
    `bin/pmtiles.exe` fetched (17 MB) and nothing missing.
17. [ ] `tools\maps\build_base_map.py --dry-run` finds the tool and prints
    the planet size.
18. [ ] The planner map draws the base. An area download from the planner
    completes and the area appears in Map data.

Leave the radios idle afterwards: stop RX and TX from the GUI, as on Linux.
