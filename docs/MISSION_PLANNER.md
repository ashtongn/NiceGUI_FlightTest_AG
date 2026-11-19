# DPMG Mission Planner, user manual

This manual covers the mission view of the DPMG ground station and plan files
in the dpmg-mission/1 format. It is for the operators who build and run a
plan, and for whoever reviews it.

## 1. What it is

The mission planner lays out what each radio does and when, checks the plan
for problems, and runs it. A plan is a list of steps. A step is one technique
from a fixed catalog, on one radio, with its own settings, placed either at a
time on the timeline or at a place on the map. The catalog holds jammers, test
tones, comm waveforms from a scenario JSON, IQ playback, recordings, a
transponder and sample rate changes.

There are two kinds of steps:

- Timeline steps. When you press Run mission and confirm, each one starts at
  its start time and stops at its stop time.
- Location tasks. These have no time. A location task keys when the platform
  (the aircraft or vehicle carrying the radio) enters an area you drew on the
  map. It stops when the platform leaves or after a set length.

Run mission asks for one confirm for the whole timeline. A location task asks
when its area is entered, unless it is automatic or was confirmed once since
arming: those key with no question once the triggers are armed. STOP ALL TX, on
either page, stops every radio, cancels the run and disarms the triggers. Stop
this step, on the planner page, stops one keyed step and leaves the rest of the
plan going. After either button, a step the platform is inside stays held: it
does not key again until a position reading puts the platform outside its area,
even if the position drops out or the triggers are turned off and on.

## 2. Starting it

The planner is the mission view of the DPMG ground station, a page of its own
beside the operator and engineering views. Start the app the usual way on the
x86 laptop, from the repo root:

```bash
./launch_gui.sh
```

Then open http://localhost:8080/mission in a browser, or click "mission view"
in the header of the operator or engineering view. The mission view's header
links back to both. Nothing else has to be started.

The planner has two views of the one plan, built from the same code with a
mode flag:

- The flight view, at /mission. This is the page for a flight or a ground run.
  Under the map it shows the live position feed (section 7, "Live feed") and
  has no simulated motion controls, so nothing on the page can stand in for
  the aircraft by mistake.
- The engineering view, the tab "Mission planner (engineering)" on
  /engineering. The same planner with everything on, the Simulate platform
  motion row included (section 10). It is the archive of the full tool and the
  place for checkouts without an aircraft.

Both views edit the same plan, share the same audit log and run state, the
same selected radio and Show on map choice, and
STOP ALL TX on either stops the same run. The engineering view also has the
tab "Aircraft feed" (section 7a), where the feed is set up and checked.

The view shares the app's radio link with the SDR tab. Every radio the ground
station has discovered, or that you added with Connect by IP, is listed here
too. The Super Nano's B200 over USB is an example of the second kind. Add
radio (section 5) adds one more by address, for both views.

The view keeps its plan in ~/.dpmg/mission_autosave.json and the map store in
~/.dpmg/maps. launch_gui.sh bind-mounts the laptop's ~/.dpmg into the
container, so closing the browser, restarting the container or rebuilding the
image loses nothing. To keep the folder somewhere else, set DPMG_HOME before
running launch_gui.sh.

The map needs the map store at ~/.dpmg/maps, which holds the world base map
and the areas you downloaded (section 9). Without the store the map is blank
and says so.

Open file loads a plan. Each plan names the radio it ran on, so bind it to the unit
under test first: Add radio with that unit, then move the steps to its lane.
Location tasks are drawn in one lane per radio, and every block and map label
ends with the radio it keys on.

For development the planner needs nothing of its own either, because any GUI
started from the repo serves it. It is the same dpmg/mission code, in the same
process as the other two views. Open http://localhost:8080/mission on the app.
To use a second GUI beside the live one, start it as below, add a simulated
radio with Connect by IP, and open http://localhost:8081/mission:

```bash
python3 run_gui_link.py 8081 47841
```

## 3. The page

From the top down:

- Top bar. The mission name and the authorization reference. Every radiating
  step must have a reference, from the plan or from the step. Then Check plan,
  Open file, Save plan and Delete all steps. On the right are the run state
  (idle, STARTING, RUNNING, KEYED, RUN FAILED), Run mission and STOP ALL TX.
- Status strip. The console clock, each radio's clock and reference state,
  and chips for errors, warnings and "needs input". Click a chip to see the
  list. Errors stop Run mission; warnings and needs input do not.

  The console UTC chip is this laptop's clock, and every step is keyed from
  it. Each radio chip shows how far that radio host's own clock is from the
  console. Sync radio clocks in the strip steps them all to the console. For
  a lasting fix, give the radios an NTP source (see the appendix).
- Left column. Mission radios (how the plan starts, the date, each radio with
  its state and start sample rate, and Add radio), and the Catalog (the
  techniques, grouped, with a search box).
- Middle. Two tabs, Timeline (one lane per radio and a lane for location
  tasks) and Map. Each takes the whole width so the operator can see it.
  Under them, a second pair of tabs holds the Step editor and the Audit log.
  Pick or Draw in the step editor opens the Map tab for you.

## 4. Building a plan

1. Give the plan a name and an authorization reference in the top bar.
2. Under Mission radios, pick how the plan starts:
   - Start all together on Run mission (the default for a new plan). Step
     times are elapsed from the moment you confirm Run mission, read T+ m:ss;
     0:30 means thirty seconds after the confirm.
   - Start all at planned UTC times. Step times are clock times on the date
     shown. Run mission arms the plan, and each step keys at its UTC time.
3. Add steps. Either click a catalog entry, which puts it on the radio chosen
   in "add to radio" a minute ahead on the UTC clock, or drag the entry onto a
   radio's lane at the time you want. Entries marked with the radio icon
   radiate.

   Comm waveform from a scenario JSON (Comms group) plays a set of comm
   signals described by a scenario JSON. It is the same file that
   dpmg/scenario/gen_scenario_json.py and the playback panel's "Scenario
   JSON" source read: a "signals" list with modulation, symbol rate, offset,
   message, FEC and so on, or a DSSS "users" list.

   When you add the entry, a chooser asks where the JSON is:
   - Upload it from this console. The file is read here, checked (it must be
     a scenario) and kept inside the step. The plan file carries it, so the
     radio does not need a copy.
   - Pick one already on the radio under scenarios/json, or type its path
     there.

   The editor then shows the file name and one line about it: how many
   signals, which modulations, and how wide the set is around the TX center.
   When the radio is online the line also says whether the set fits at the
   radio's rate. That is only a check; nothing is built or keyed until the
   step runs. Other settings:
   - "build at the radio's rate" (on by default) rebuilds the scenario at the
     radio's sample rate when the step runs, so the offsets land where the
     JSON says. Off, a JSON written for another rate plays as it is, with
     every frequency scaled.
   - "peak (FS)" (0.45 by default) is the peak of the built waveform as a
     fraction of DAC full scale. The signals keep their levels relative to
     each other. It overrides a peak_fs in the JSON.
   - Choose scenario JSON swaps the file later.

   A spec too big for one command datagram (about 1200 bytes) goes to the
   radio over the file transfer when the step runs. You do not have to do
   anything.

   IQ playback plays an IQ file already on the radio. Older plans that played
   a scenario JSON through IQ playback open as Comm waveform steps.
4. Click a step to edit it. The editor has these sections:
   - Timing. Start and stop. On the UTC clock type HH:MM or HH:MM:SS; on a
     relative plan type a number and a unit. Priority runs from 1 (highest)
     to 9, default 5.
   - RF. RF center (RX and TX) retunes the radio before the step. TX
     frequency puts TX on its own frequency; blank means the RX frequency. TX
     atten and RX gain set the radio before keying. Blank keeps whatever the
     radio is set to.
   - Ports. The RX and TX SMA jack this step uses on its radio: TRx1A, TRx2A,
     TRx1B or TRx2B on a G40; the RX handles and TXA1 on an X40; RX2 and
     TX/RX on a B200. Blank keeps whatever the radio is on. Ports are init
     parameters on the Epiq cards, so a step on ports other than the radio's
     current ones re-initializes the radio first. That takes about 50 s on a
     G40, and RX stops. Check plan warns at every step where this will
     happen. It also flags a pair the radio cannot stream together: RX on a 2
     jack with TX on the 1 jack of the same G40 card. The ports show on the
     step's block in the lane.
     RX can also be "none (TX only)" on a step that only transmits (not on a
     record, RX dwell or transponder step). The radio still needs some RX
     port, so the runner keeps the one it is on when that can stay while it
     transmits and otherwise moves RX first: on a G40 to the 1 jack of the
     other card from the TX jack, on an X40 to RXB1. A run of such steps
     re-initializes at most once, and the same card rule never refuses them.
     Check plan says where RX will move, and says nothing once the connected
     radio's RX port can stay. The plan file stores it as
     "ports": {"rx": "none", "tx": "TRx1B"}. A re-init for such a step leaves
     RX off, and a re-init on a radio brought up with RX off keeps it off. For
     a plan with no RX at all, bring the radio up with Receive (monitor) on
     START unticked: the runner does not stop an RX stream already running, and
     the Run mission confirm says so when one is. The radio cards read "RX off"
     when no stream is running, whatever the RX port dropdown shows.
   - Options. The technique's own settings (power, loop length, PRF, tone
     offsets, bandwidths and so on), as the operator panels offer them.
   - Location. An optional start area and stop area for a timeline step
     (section 7).

   Edited fields turn amber until you press Apply changes. Delete step removes
   the step.
5. Move steps by dragging them along a lane or onto another radio's lane.
   Ctrl-click selects several, dragging one of them moves them all, and the
   Delete key removes the selection. The timeline zooms with the wheel over a
   lane and with the zoom buttons. Show whole day and fit all steps reset the
   view.
6. Sample rate. A radio keeps the rate it was brought up at, unless you set a
   start sample rate on its row (applied before the timeline runs) or add a
   "Set sample rate" step. With that step the radio re-initializes at that
   time, so leave a few seconds before the next step. Picking an IQ file sets
   the rate to match the file when it can.
7. Press Check plan. It flags a radio booked twice at once, a step on a radio
   that cannot run it, a radiating step with no authorization, times out of
   order, and the limits (24 h per plan, 1 h per step). It shows a playback
   with no file or a comm waveform with no scenario JSON as "needs input". It
   also flags location tasks that share a radio with timeline steps (section
   8). Section 15 has the full list.

## 5. Radios

The plan lists its radios under Mission radios. A radio connected to the
ground station shows up by itself. A radio that is not connected yet can be
added with Add radio: give its serial, its type and, if you have it, its
address as ip:port. With an address the planner connects at once, and again
whenever the plan is reopened. The link icon on the row retries. When you
press Run mission, a planned radio that is offline is left out: its steps are
skipped and the confirm says so.

Each row shows the radio's state: not set up, ready, RX running or offline.
A radio that is connected but not set up is initialized by Run mission before
the timeline starts, and the confirm shows the settings first. It uses the
row's start rate (30.72 MSps if blank), the row's clock (internal by default),
and the frequency, ports and TX attenuation of the radio's first steps. RX is
left off if all its steps say RX none. Nothing keys until a step does. A
location task does the same when it fires, so its first key is late by the
init time. Bringing the radio up first in the DPMG app is
quicker, and a radio that is already up keeps its own clock. RX already
streaming stays on through a TX only plan, and the confirm says so; stop RX first
if you do not want it.

A TX already running is different. The row says TX ON and the confirm says it
will be stopped: Run mission stops it before the plan starts, and so does a
location task when it fires on a radio no step is using.

The clock matters. A G40 row offers five choices: internal; gpsdo (GPS antenna);
external 10 MHz and 1 PPS; external 10 MHz only; 1 PPS only (own oscillator). On an
external 10 MHz the radio service switches the carrier's HMC7044 to the REF jack
itself and refuses the init, with the reason, when the chip does not lock to it; the
radio's chip in the strip then shows "10 MHz in use" with the measured clock error,
or "10 MHz LOST" in red when the 10 MHz goes away (LOGGING.md, "What ref_locked tells
you"). 1 PPS only is for the aircraft: its pulse on the 1PPS jack, the radio's own
oscillator for the clock. An X40 row takes internal, external and x40_gpsdo. gpsdo is
not an X40 choice: it is the card's FPGA GPSDO, which an X40 does not have
(libsidekiq returns -95 there by design), so Check plan fails an X40 row on gpsdo
and the X40 service refuses it before it touches the running radio. x40_gpsdo selects
the X40's own GPSDO, the one in its GPS module, as the host reference clock; see the
appendix, "Clocks: GPSDO lock and GPS fix". An init
that fails on the reference can stop the radio service. In a run a failed bring-up is
not tried again: that
radio's steps are skipped, the others carry on, and when no radio is left the run
ends and shows RUN FAILED with the reason. Outside a run, for a location task, a
failed init is not repeated for 60 s, so the tasks in that time are skipped and
the audit log says why.

The sweep icon on a row removes that radio's steps. Delete all steps in the
top bar removes every step. Both ask first. Radios and the start settings
stay.

## 6. Running

1. Press Check plan and fix the errors.
2. Press Run mission. The confirm lists every radiating step with its time
   and radio, says which radios are offline (their steps are skipped) and how
   many location tasks are armed separately, and asks you to confirm that the
   load or chamber is connected. KEY THE MISSION NOW runs the plan.
3. The run state in the top bar shows what is keyed. The audit log records
   every command and its result.
4. STOP ALL TX sends stop to every connected radio together, cancels the run,
   drops any waiting location task, disarms the triggers and sets an RX attenuator
   a Decode step opened back to 120 dB. It works at any time. A radio that does not
   answer takes up to 5 s to time out, and the message says how many answered.
5. Stop this step appears beside STOP ALL TX for each keyed step. It stops that step
   only: later timed steps and other location tasks still key, and a location task
   waiting for that radio starts. The step's planned stop then sends nothing. A
   task that asks for a confirm asks again on its next entry. An automatic task
   keys on its next entry with no question.
6. A run keeps the steps and times it started with. Editing the plan during a run
   does not change that: a removed step still keys at its old time, an added step
   does not key, and a moved step keeps its old time. Changed settings on a step
   that has not started are used. A warning under the run controls names each step
   that differs. Location tasks are not part of the run and follow the open plan.

To change a plan during a mission, press STOP ALL TX, load or edit the plan, press
Run mission and arm the triggers again if the plan has location tasks. A relative
plan counts from the new Run mission. A UTC plan skips any step whose start time
has passed.

A run keys each step through the same radio commands the operator panels use.
It re-initializes a radio only for a step on other jacks or a Set sample rate
step (section 4), or to bring up a radio that is not set up (section 5). A
radio that is already up keeps its clock.

## 7. Map and location triggers

The map under the timeline shows the flight path, the step areas of the radio
you have selected, the platform position and the areas you downloaded. Zoom
with the wheel and pan by dragging. The controls over the map are:

- Draw flight path: click along the route, double-click to finish, Esc
  cancels.
- Import flight path: GeoJSON, GPX, KML, KMZ, CSV or a zipped shapefile.
- Overlays, Clear flight path and Fit map to plan.
- Stored map areas (section 9) and New location task (section 8).

Map files can also be dropped anywhere on the map (section 9, "Dropping map
files").

Which radio the map shows. Every radio in the plan has its own colour, the
same on its row under Mission radios, on the left edge of its timeline lane
and as a band on each of its blocks, on its areas on the map and in the map
labels, which start with the radio's name. Click a radio (its row, its lane
name or empty lane space, or its name in the page header) to select it: the
map then shows that radio's steps and areas only, and the line over the map
says so, for example "X40-0001: no steps in this plan" for a radio with
nothing planned. The Show on map bar over the map has two one-click choices,
"selected only" (the map follows the radio you click) and "all" (every
radio at once), and one chip per radio in its colour: click a chip to show
or hide that radio, which makes a set of your own. Clicking a step block or a
map marker selects that step's radio as well. The selection and the show
choice are shared like the plan itself: a reload or a second browser shows
the same.

The colours are the Okabe and Ito set (blue, orange, green, purple, then
vermilion, sky blue, dark yellow, black), chosen because they stay apart for
colour-blind viewers and read against the light base map. A radio's colour is
its place in the plan's radios list, so nothing about colour is saved in the
plan file; adding a radio at the end never recolours the others. The block
fill on the timeline follows the catalog group (what the step does);
the radio's colour is the band on its left.

The flight path belongs to the plan, not to a radio, so it always shows, in
neutral grey, whichever radio is selected; dropped overlays are the plan's
too and always show. The location triggers watch every radio's areas whatever
the map shows: hiding a radio on the map hides nothing from the triggers.

A timeline step can carry a start area and a stop area, set in the Location
section of the editor. An area is a point with a radius, typed or picked on
the map, or a polygon drawn on the map. Areas are drawn in the radio's
colour: a start area has a solid edge and a filled marker, a stop area a
dashed edge and a hollow marker. Each marker can be dragged to move the area,
and clicking a marker selects the step.

With the triggers armed, entering a start area opens a prompt to key that step
now; it then runs for its planned length. Entering its stop area opens a
prompt to stop it. The prompt shows the radio's state, the authorization and
any validation errors. KEY IT NOW stays disabled while any of them fails.

Position. Under the map, "listen to the aircraft feed" takes the platform
position from the multicast feed. It has group, port, interface and format
fields: use the network interface for a real feed and 127.0.0.1 for the lab
player; the format is auto (each datagram is sniffed), json or nmea. The
values are saved in ~/.dpmg/aircraft_feed.json and are the same ones the
operator header's Aircraft position panel and the engineering Aircraft feed
panel use, and there is one listener behind all three, so turning the feed on
in any of them turns it on everywhere. Without the feed, the first radio with
a GPS fix is used. The status line shows the position, its age and its
source. When the position is older than 10 s, or there is none, the triggers
turn off and the line says so.

More than one aircraft. The feed settings hold a list of named aircraft, each
with its own group, port, interface, format and combine window (add, rename and
remove them on the engineering Aircraft feed tab). An old one feed settings file
loads as one aircraft named after its group and port, and nothing on the page
changes while there is only one. With two or more:

- each plan radio row has an "aircraft:" choice and its status chip ends with
  "aircraft N1". The location triggers of that radio's steps run on that
  aircraft's position. The choice is saved in the mission file as the radio's
  "aircraft" field; the first aircraft is the default and is not written, so a
  plan from before the field, or one naming an aircraft that is no longer on
  the list, follows the first aircraft.
- an "aircraft" picker sits before the listen switch; the switch and the group,
  port, iface and format fields act on the picked aircraft.
- the Live feed has one line per aircraft, each starting with its name, with
  its own red or amber warning line.
- the map draws one marker per aircraft, labelled with its name, in its own
  colour (purple, then teal, pink, ...), grey when that aircraft's fix is stale.
- the audit log names the aircraft on a trigger entry ("aircraft N2").
- two aircraft may listen on the same group and port with different formats;
  the Aircraft feed tab and the operator console say when two share a socket.

The platform marker on the map is a purple dot, grey when the fix is stale.
When the feed carries a heading (an NMEA VTG or RMC course, or a JSON heading
key) the dot becomes an arrow turned to that heading, with "hdg 087" in its
label. A feed with no heading keeps the dot; nothing else changes, and the
triggers never use the heading: they take latitude, longitude and time only.

Live feed (flight view). The row under the position row reads, in one line,
the position source (aircraft feed, a radio's GPS, or simulated motion), the
group, port and interface being listened on and whether the listener is
running, the time and age of the last fix, the fix rate and the datagram
count, then, when the feed carries them, the heading (degrees true, as
"hdg 087"), the ground speed in knots and the fix mode with the satellites
used ("3D 8 sats"). A red line under it says when something is wrong: no feed and no GPS,
nothing received yet, a stale fix, or a simulated position (the engineering
view's motion player is running, which the flight view cannot stop; stop it
there). An amber line warns when the feed's own time stamps are more than
2 s from this console's clock, or when the position comes from a radio GPS
because the feed is not listening.

## 7a. Aircraft feed panel (engineering view)

The top row is the aircraft list: a picker, Add (name, group, port, interface,
format), Rename and Remove (the last aircraft stays). The fields, the raw
packets, the recorder and the test sender below work on the picked aircraft.
`tools/sim_position.py --aircraft N2` sends on that aircraft's saved group and
port.

The tab "Aircraft feed" on /engineering is where the feed is set up and
checked before a flight, with nothing else on the page:

- Listener: group, port, interface, format, Save, Listen and Stop. Save keeps
  the values for every view; Listen saves them and starts the shared
  listener.
- Parsed fix: the one line summary (position, altitude, heading, speed), the
  time stamp inside the datagram, the arrival time, what fed the fix (json, or
  nmea: followed by the sentences combined, as nmea:GPGGA+GPRMC+GPVTG), the
  age, the fix rate over the last 10 s, and the rx, parsed, bad datagram and
  bad sentence counts. Under it a line says which NMEA sentences arrive and
  how often, as "GPGGA 1.0 Hz, GPRMC 1.0 Hz, GPVTG 1.0 Hz, GPGSA 0.20 Hz,
  GPGSV 0.20 Hz, GLL none", so a sentence the aircraft does not send (and
  the fields that go with it) is obvious at a glance: no VTG and no RMC course
  means no heading. Then a table with every field of the fix: latitude,
  longitude, altitude, heading true and magnetic, ground speed (knots and
  m/s), fix mode, fix quality, PDOP, HDOP, VDOP, satellites used, satellites
  in view, the feed time against this console's clock and the arrival time,
  each with the sentence (or json) that carried it and the seconds since that
  sentence arrived. The same table is on the operator page under Aircraft
  position (multicast).
- Combine window: a receiver sends a burst of sentences each second, often
  split over several datagrams. The fix keeps the newest value of each field
  from any sentence inside the window (1.5 s unless changed here), so the
  planner sees one complete picture: position and time from GGA, RMC or GLL,
  altitude from GGA, heading and speed from VTG or RMC, fix mode and DOP from
  GSA, satellites from GGA, GSA and GSV. When two sentences in one burst
  disagree on the position, the one with the later time of day wins, then
  GGA over RMC over GLL. A sentence with a wrong checksum is dropped and
  counted. A sentence from any talker is read (GP, GN, GL, GA, ...). A
  receiver that sends GSA and GSV every 5 s needs a window of 5 s or more
  for the fix mode, DOP and satellite count to stay in the fix between
  them; the sentence line shows each rate, so set the window from it.
- JSON feeds: an object, an array of objects, or several objects in one
  datagram (one per line, back to back, or pretty printed). Key names are read
  in any case. The position may sit in a sub-object (position, gps, nav,
  location and the like, up to three levels down); the top level wins, and a
  home, target or waypoint sub-object is never read. Keys: lat/latitude/lat_deg and
  lon/lng/long/longitude/lon_deg; alt/altitude/alt_m/altitude_m/elevation in
  metres, or alt_ft/altitude_ft and the like in feet; a time as in the next item; heading/hdg/course/cog/track (degrees
  true) and heading_mag/mag_heading (magnetic); speed/spd/sog/gs/ground_speed
  in metres per second, speed_kt/speed_kts/speed_knots/sog_kt/gs_kt in knots,
  speed_kmh/speed_kph in km/h; fix_mode/mode/fix_type/fix as "2D", "3D",
  "none" or 1, 2, 3; fix_quality/quality as 0 to 8; hdop, pdop, vdop;
  sats/satellites/num_sats/numsv/sats_used and sats_in_view/satellites_in_view.
  Only the position is required for a fix. An object with no position (a
  heading and speed message, say) adds its fields to the fix held.
- Feed time against this console: the datagram time stamps minus the console
  clock at arrival, averaged over the last fixes with the spread. Steps are
  keyed from the console clock, so a large offset means one of the two clocks
  needs a look before a UTC plan. GGA sentences carry a time of day only; the
  date is taken as the one nearest the console's clock. JSON feeds may carry
  time, time_utc, timestamp, t or epoch (seconds, milliseconds, or ISO text).
- Raw packets: the last datagrams as received, newest first, with arrival
  time, sender address, size, what parsed and the datagram text. Record to
  file writes every datagram with its arrival time to
  ~/.dpmg/feed_records/feed_<date>.jsonl until pressed again.
- Test sender: plays a path onto the group as the aircraft would, as JSON or
  NMEA at a set rate, speed and altitude, with a time offset to try a feed
  whose clock is off, or replays a recording with its original spacing. The
  heading is the direction of the leg being flown and the speed the one set.
  As NMEA it sends the full burst GGA, RMC, VTG, GSA, GSV in one datagram
  each tick; the NMEA burst row drops chosen sentences (drop VTG to see the
  heading come from RMC alone, drop RMC and VTG to see the GUI with no
  heading), turns the heading off altogether, sends GSA and GSV on every nth
  burst only, or sends one sentence per datagram to try the combine window. The
  path comes from the autosaved plan's flight path, typed lat,lon points, a
  GeoJSON file on the console or a recording. Interface 127.0.0.1 with the
  listener on 127.0.0.1 keeps it on this console. The planner sees the test
  sender as the aircraft feed: disarm the location triggers of any plan with
  radios connected before pressing Start.

From a terminal the same thing is `python3 tools/sim_position.py`, which
reads the saved group, port and format and stamps each datagram with the time.

"location triggers armed" is the switch that lets areas prompt or key at all.
Disarming forgets any "confirm once" confirms.

## 8. Location tasks

A location task is a step keyed by place instead of time. To make one:

1. Press New location task.
2. Choose Draw a box, Draw a polygon or Mark a point, and draw it on the map.
3. Fill in the dialog: the task from the catalog, the radio, when it runs, its
   length, the priority and the confirm rule.

The task shows as a chip in the location tasks lane and as an area on the
map. Click the chip to edit it.

Areas and points can also come from a file. Drop a KML, GeoJSON, shapefile or
CSV of sites on the map and answer the prompt (section 9, "Dropping map
files"). Each area or point becomes its own location task, with the task,
radio and rules you pick there. These are the same choices as for a drawn
area.

When it runs:

- While inside the area. Keys when the platform enters. Stops when it leaves,
  or after "at most" seconds (default 600).
- At the point, for a set time. Keys when the platform comes within the
  point's radius and runs for "run for" seconds (default 60), even if the
  platform leaves earlier.

Confirm rule:

- Confirm the first entry, then automatic (the default). The first entry
  asks. Later entries key without asking while the triggers stay armed.
- Confirm every entry.
- Automatic. Keys on every entry with no prompt. Arming the triggers is your
  go-ahead for these, and the arming note in the audit log says how many the
  plan has. The radio and authorization checks still apply.

Priority. Every step has a priority, from 1 (highest) to 9. Location tasks
default to 3 and timeline steps to 5. When a task and a timeline step want the
same radio, the higher priority wins:

- A task takes the radio from a keyed step of lower priority. The step is
  stopped and logged as pre-empted.
- Behind a step of equal or higher priority, the task waits. It starts when
  the radio frees, if the platform is still in the area.
- A timeline step that comes due while a task is keyed is skipped when its
  priority is equal or lower, and takes the radio when it is higher.

Check plan warns about every task that shares a radio with timeline steps. It
gives both priorities and the outcome, so you see it before the run. The entry
prompt says it too.

Run mission does not start location tasks. They key from their areas while
the triggers are armed.

## 9. Stored map areas

The map works fully offline. The store at ~/.dpmg/maps holds a world base map
(country and region detail everywhere, up to zoom 7) and downloaded areas with
street-level detail (up to zoom 14).

Stored map areas, in the top right of the map, lists what is stored with its
zoom range, size and age. It shows the total against the store budget (20 GB)
and lets you remove an area. To add one, Download a box draws a box on the
map, Check download size reports the size before anything is fetched, and
Download tiles fetches it. That needs internet on the ground station.

A warning over the map tells you when the view is zoomed past the stored
detail, when the data is older than 180 days, or when the renderer pieces are
missing.

To build the store on a new ground station, which needs internet once, run
these from the repo root. The first fetches the renderer pieces (about 70 MB)
and the second the world base (about 190 MB):

```bash
python3 tools/maps/vendor_map_assets.py
python3 tools/maps/build_base_map.py
```

Then download areas from the page, or copy ~/.dpmg/maps from another station.
The container serves the same ~/.dpmg/maps folder, because launch_gui.sh
mounts it. A store built on the laptop shows up in the 8080 app on its next
page load.

### Dropping map files

Drag files from the desktop or a file manager and drop them anywhere on the
map. The map gets a dashed blue edge while files are over it. One drop takes
up to 10 files. The planner works out what each file is from its extension
and, when that is missing or wrong, from its content:

- GeoJSON (.geojson, .json): points, lines and polygons, in Features or bare.
  A Feature's name comes from its name, label or title property. A point with
  a radius_m, radius, radius_km or radius_nm property is a circle.
- KML and KMZ (a zip holding a doc.kml): Placemarks with points, lines,
  polygons, gx:Track and MultiGeometry, named by the Placemark.
- GPX: each track (its segments joined) and each route is a line, and each
  waypoint a point.
- CSV (comma, semicolon, tab or pipe separated): one point per row. The header
  has to name the columns. The planner finds these columns by itself: lat,
  latitude or y; lon, lng, long, longitude or x; a name column (name, label,
  title, callsign, site, id); and a radius column (radius_m, radius,
  radius_km, radius_nm). Rows without numbers are skipped and counted.
- A zipped shapefile (a .zip with the .shp, .shx and .dbf, and the .prj if
  there is one): points, lines and polygons, named from a name-like field. It
  has to be in WGS84 latitude and longitude; a projected one is refused with a
  message saying so. This needs the pyshp package, which ships as a wheel in
  deploy/wheels. The image build installs it. On a console without it, run:

  ```bash
  pip install deploy/wheels/pyshp-3.1.6-py3-none-any.whl
  ```
- PMTiles and MBTiles: map tiles, see below.

Altitudes are dropped and polygon holes are ignored. A data file over 50 MB is
refused. A mission plan dropped on the map is refused with a pointer to Open
file. Anything else gets a message listing what the map takes.

All the data files in one drop are read together, and one prompt asks what
they become. The defaults are:

- Lines become the flight path. The longest line is used and any others are
  kept as an overlay. The prompt warns when this replaces a path the plan
  already has.
- Areas become task areas, one location task each.
- Points become task points, one location task each, with the radius from the
  file or the "point radius" in the prompt. When the drop has no lines,
  points can instead become the flight path, joined in file order.

Any of the three can be made "an overlay layer" or left out. For task areas
and points the prompt has the same fields as New location task: the task from
the catalog, the radio, when it runs, run for or at most, priority and
confirm. A drop with more than 20 areas or points defaults to an overlay, so a
large file does not turn into a pile of tasks by accident. "zoom the map to
it" frames what was dropped. Everything imported goes in the audit log.

An overlay layer is map data you only want to see. It is drawn dashed in its
own colour with its names, and takes no part in triggers or plan checks. It is
saved in the plan file (map, overlays), so it comes back when the plan is
loaded. Overlays, over the map, lists them with a button to zoom to each and
one to remove it.

A dropped .pmtiles or .mbtiles file goes into the map store as a new area. The
prompt shows the tile type, zoom range, size and the area it covers (read from
the file), the store total against the budget, and whether it replaces a
stored area of the same name. The area name defaults to the file name.

When the file would put the store over its budget, the add button is disabled
and nothing is copied. Vector tiles draw with the same style as the rest of
the map. Raster tiles (png, jpeg, webp) draw as an image layer. MBTiles are
converted to PMTiles with the go-pmtiles tool from the store, which
tools/maps/vendor_map_assets.py fetches. Without the tool the drop says so.
The new area is listed under Stored map areas like a downloaded one.

## 10. Simulated motion

Engineering view only: the row is on the "Mission planner (engineering)" tab,
not on /mission. While it plays, the flight view's Live feed row turns red and
says the position is simulated.

To check location tasks without flying, use the Simulate platform motion row
under the map:

1. Make a flight path: draw or import one, or press Path through the task
   areas, which builds a route through every task area in plan order.
2. Set a speed and press Play motion. The platform flies along the flight
   path.

Pause and Stop motion do what they say, and loop turns around at the ends.
While it plays, the simulated position replaces the feed and the triggers
treat it as real. You can arm the triggers, press Run mission and watch a task
and the timeline meet. Every play, pause and stop is in the audit log.

For a position from outside the page, such as a second screen or a script, the
lab player sends positions on the aircraft feed. The first line follows the
autosaved plan's flight path back and forth; the second flies between the
points given:

```bash
python3 tools/sim_position.py --plan --loop
python3 tools/sim_position.py --points "33.40,-112.07 33.45,-112.07"
```

Set the page's interface field to 127.0.0.1 and turn "listen to the aircraft
feed" on.

## 11. Files

- Save plan writes the plan as a dpmg-mission/1 JSON file, and Open file reads
  one back. Plans move between ground stations unchanged.
- Open file also takes CFE files. In an autoSchedule.csv the Freq,Gain,Time
  rows become RX dwell steps back to back. In a CFE role XML each service
  becomes a step seeded from its method and settings. A dialog asks which radio to put the steps on, and whether to append or
  replace. The audit log notes anything in the file that the plan could not
  take.
- Import flight path (over the map) takes GeoJSON, GPX, KML, KMZ, CSV and a
  zipped shapefile. Dropping files on the map takes the same, plus PMTiles and
  MBTiles (section 9).
- Overlay layers from dropped files are kept in the plan file under map,
  overlays, each with its name, colour, lines, polygons and points.
- The audit log is in the right-hand tab. It lists every edit, import,
  confirm, trigger, run and stop with a time.

## 12. Safety rules

- Nothing radiates without a confirm or an automatic rule you set yourself.
  Location tasks radiate only while the triggers are armed.
- STOP ALL TX is always available and stops every connected radio.
- A radiating step needs an authorization reference, on the plan or the step.
- Keep the TX attenuation and amplitude inside what the load can take. On the
  bench loopback with a 20 dB pad that means TX attenuation of 40 dB or more
  and amplitude of 0.5 of full scale or less.
- Location triggers are an aid. A stale or missing position switches them
  off, and every trigger and confirm is logged.

## 13. Troubleshooting

- A radio shows offline. It is not connected to the ground station. Use the
  link icon on its row (if it has an address), or connect it in the DPMG app.
- A step says "radio not initialized and cannot bring it up". The plan does not
  say enough, for example no frequency or no TX port on a radiating step. Set
  that on the step, or initialize the radio in the DPMG app first.
- Run mission is refused. Fix the red errors in the status strip.
- The map is grey. The map store is missing or incomplete; see section 9.
- No position. The feed is not running, its interface is wrong, or the
  firewall drops the multicast group. The lab player uses 127.0.0.1.
- The platform is inside an area but nothing happens. Check the audit log for
  the reason: the triggers are not armed, the position is stale, the task
  already ran or was stopped by hand (it re-arms when the platform leaves), or
  the radio is busy with a step of higher priority. The position line and the
  timeline name a held task.
- Two browsers on the page. Both see the same plan, and a trigger prompt
  appears on one of them.

## 14. Appendix

Plan file. JSON in the dpmg-mission/1 format, with name, authorization,
anchor (mode on_tx, or clock with a date), radios (id, type, address, start
sample_rate), steps (id, radio, technique, params, schedule or trigger,
location, priority) and map (flight_path).

Times are in seconds. An area is a point (lat, lon, radius_m) or a polygon of
[lon, lat] vertices. A comm waveform step (technique scenario_json) either
keeps an uploaded scenario inside params.spec (with spec_name, source
"console") or names a radio file in params.spec_path (source "radio").
match_radio_rate is its build option. When an older plan loads, an IQ
playback step whose source was "scenario" is moved to scenario_json.

Code. The dpmg/mission package: catalog, mission_file, mission_runner,
mission_import, scenario_spec, ports, payload_sweep, triggers, position_feed,
motion_sim, map_element with static/mission_map.js, map_store, base_map,
map_import and mission_tab.
dpmg/web/nicegui_app.py mounts it as the /mission page (marker MISSION_VIEW).
The tests are the test_mission_*, test_scenario_spec, test_map_*,
test_triggers, test_motion_sim and test_position_feed files in tests/. Run
each under pytest, for example:

```bash
python3 -m pytest tests/test_mission_file.py
```

Comm waveform on the radio. The step sends gen_scenario with load_tx and
match_radio_rate, then start_tx CONTINUOUS, and stop_tx at its stop. The spec
goes inline, as spec_inbox after a file push for a big spec, or as spec_path
for a radio file. These are the same commands the playback panel's Scenario
JSON source sends. The radio verdict in the summary line comes from a
gen_scenario check_only. An older service answers it without the signal count
for a radio file, so the line then shows the verdict only.

Helper scripts in the repo: tools/maps/vendor_map_assets.py and
tools/maps/build_base_map.py build the map store, and tools/sim_position.py
plays a position on the aircraft feed.

## 15. Appendix: every control on the page

### Top bar

- mission name: the plan's name. It goes into the saved file name.
- authorization ref: the reference that authorizes radiating steps. A
  radiating step without one, here or on the step, is an error and Run
  mission refuses to run.
- Check plan: checks the whole plan and fills the status strip. It does not
  change the plan and does not key anything. It flags:
  - a radio booked twice at the same time;
  - a step on a radio that cannot run that technique;
  - a radiating step with no authorization;
  - a stop before its start;
  - a step over 1 h or a plan over 24 h;
  - a playback with no file, or a replay with no recording;
  - an RX gain index outside the radio's range;
  - a sample rate the radio cannot do;
  - a step that starts within 3 s of a sample rate change (warning);
  - a location task that shares its radio with timeline steps (warning, with
    the priorities and what would happen).

  Run mission runs the same checks first, so Check plan shows them before you
  get to the confirm. Errors are red and block the run. Warnings and "needs
  input" are amber and do not.
- Open file: a dpmg-mission JSON replaces the plan. A CFE autoSchedule.csv or
  role XML opens a dialog asking which radio to put the steps on, and whether
  to append them to the plan or replace it.
- Save plan: downloads the plan as a dpmg-mission JSON file.
- Delete all steps: removes every step after a confirm. Radios and the start
  settings stay.
- idle, STARTING, RUNNING, KEYED, RUN FAILED: the run state. STARTING shows
  while Run mission brings radios up or gets them ready. RUNNING is a timeline
  run in progress and reads "RUNNING: keyed = ..." with the keyed steps. KEYED
  means a location task holds a radio outside a run. RUN FAILED shows the
  reason when no radio was left to run (section 5).
- Run mission: opens the run confirm. The confirm lists every radiating step
  with its time and radio, marks the ones on offline radios as skipped, says
  how many location tasks are armed separately, and has KEY THE MISSION NOW.
  Nothing radiates until you press that button.
- STOP ALL TX: sends stop to every connected radio together, cancels the run, drops
  waiting location tasks and disarms the triggers. The operator page button does the same.
- Stop this step: shown for each keyed step. Stops that step only; see section 6.

### Status strip

- console UTC: the ground station's clock.
- A radio's chip: its clock choice by name, the reference lock (on an external
  10 MHz, "10 MHz in use" with the card clock's error in ppm, or "10 MHz LOST" in
  red; on 1 PPS only the clock error for information), the 1 PPS input when the
  radio reports it (present or ABSENT, and whether the external or the
  internal line is selected), GPS time and "OS clock
  +x.x s". The last is the radio host's clock against this console, with half
  the round trip taken out, polled every 5 s. The chip is amber when the
  reference is unlocked or the OS clock is more than 1 s off. "OS clock not
  reported" means an older radio service build.
- Set time by hand: for a setup with no internet and no GPS. Type a UTC date
  and time. Set the radios pushes it to every radio host at once. Set this
  console sets the laptop with passwordless sudo; without it, the page shows
  the command to run (chronyc settime). With NTP from the laptop in place
  (appendix, Clocks), the hand-set console time also reaches the radios on
  their next poll.
- Sync radio clocks: sets every connected radio's OS clock to this console's
  UTC and logs the result per radio. Steps are keyed from the console's clock
  whether or not the radios agree, so this is for the radios' own logs,
  recordings and GPS time compare. A radio service outside its privileged
  container answers "no permission" and is left alone.
- error, warning and needs input chips: the counts from the last Check plan or
  run attempt. Click one to see the lines. "needs input" is a step waiting for
  something you have to pick, such as an IQ file or a recording.

### Mission radios

- start: how step times map to real time. "Start all together on Run mission"
  counts from the confirm. "Start all at planned UTC times" uses clock times
  on the date below.
- date (UTC): the day a clock-anchored plan runs on.
- A radio row: the radio's colour on its left edge, the serial, its state
  (not set up, ready, RX running, offline), how many steps it has, the link
  icon (connect to its address), the sweep icon (remove its steps), the x
  (remove it from the plan), and start sample rate. Click the row to select
  the radio: the row is tinted and gets a tick, and the map shows that
  radio's steps (section 7). That is the rate it is set to before the timeline runs; blank
  keeps the rate it was brought up at. Every rate is shown with the RF bandwidth it gives, about 0.8 of the rate
  (the main app's rule). The Set sample rate step shows it live under its
  field.
- Add radio: adds a radio by serial, type and optional address, so it can be
  planned before it is connected.
- Radios on the main GUI's saved address list (Connect by IP) show up here by
  themselves. That is how a B200 on a Super Nano over USB appears: no
  broadcast finds it, so the saved address is dialed until it answers. A
  planned radio with no address binds to a live radio whose serial matches
  its name and takes its address; B200mini binds to B200MINI-3000001, for
  example. A radio dialed by address takes the real serial its service
  reports.

### Catalog

- add to radio: where a clicked entry goes.
- search the catalog: filters the entries.
- An entry: click it to add it to that radio a minute ahead on the UTC clock,
  or drag it onto a lane at the time you want. The radio icon marks entries
  that radiate. Comm waveform from a scenario JSON, IQ playback and Replay
  open a chooser for their file first. Cancel there removes the new step.

### Timeline and Map tabs

- Timeline, Map: which of the two big views is shown. The other keeps its
  state. The map takes its size when its tab opens.

### Timeline

- zoom in, zoom out: the time scale. The wheel over a lane does the same,
  centred on the cursor.
- show whole day: the full 24 h on a clock plan. fit all steps: frames every
  step.
- The time scale. A relative plan reads T+ elapsed time (m:ss, h:mm:ss past an
  hour). An empty plan shows an hour; a plan with steps shows its length
  plus a little room, rounded up to 5 min and never under 30 min, so a
  loaded 30 or 60 min mission fills the width. A clock plan shows the 24 h
  UTC day.
- A lane: one radio, with the radio's colour on its left edge and as a band
  on each of its blocks; the block fill is the catalog group. The red line is
  the current time. Click a block to edit it (this also selects its radio for
  the map), ctrl-click to add it to a selection, and drag to move it in time
  or to another radio. Click the lane's name or empty lane space to select the
  radio. The Delete key removes the selection. Red blocks have a conflict.
- location tasks lane: one lane per radio with a block per location task
  showing its mode, priority and confirm state (needs confirm, confirmed once,
  automatic, KEYED, waiting for the radio). Click to edit.

### Map

- Draw flight path: click along the route, double-click to finish, Esc
  cancels. Import flight path: a GeoJSON, GPX, KML, KMZ, CSV or zipped
  shapefile; the path is the longest line in it, or its points in order.
  Clear flight path removes it.
- Overlays: the overlay layers from dropped files, each with zoom to it and
  remove.
- Dropping files on the map: for map data, a prompt asks what its lines, areas
  and points become (flight path, location tasks, overlay). Map tiles go to
  the store (section 9).
- Fit map to plan: frames the path, the areas and the markers now shown.
- Show on map: "selected only" (the map follows the radio you click) or
  "all", then one chip per radio in its colour, filled when the map draws it;
  click a chip to show or hide that radio. The line under the bar says what
  the map shows and, for each radio shown, how many steps and areas it has, or
  that it has no steps in this plan. It ends with the rule: the flight path
  and overlays always show, and the triggers watch every radio's areas
  whatever the map shows.
- Stored map areas: what is stored on this ground station, with Remove area
  and Download a box. Draw a box; Check download size tells you the size
  first, and Download tiles fetches it.
- New location task: Draw a box, Draw a polygon or Mark a point on the map,
  then the task dialog.
- The map itself: wheel to zoom, drag to pan. Markers can be dragged to move
  an area, and clicking a marker selects its step and its radio. Areas and
  markers are in the radio's colour and their labels start with the radio's
  name; a solid edge and a filled dot are a start area, a dashed edge and a
  hollow dot a stop area; the selected radio's areas have a thicker edge when
  several radios show. The flight path is grey. The purple dot is the
  platform; it turns grey when its position is stale.
- listen to the aircraft feed, group, port, iface: the multicast position
  feed. The interface is 0.0.0.0 for the network and 127.0.0.1 for the lab
  player on the same PC.
- location triggers armed: lets areas prompt or key. When it is off, areas do
  nothing.
- The status line: triggers armed or off, the position with its age and
  source, and the sources being watched.
- format: auto, json or nmea, the datagram form the listener accepts. Group,
  port, iface and format are saved for every view.
- Live feed (flight view only): one line with the source, the listener, the
  last fix and its age, the rate and the count, and a red or amber warning
  line under it when there is no fix, a stale fix, a simulated position or a
  feed time far from the console clock.
- Simulate platform motion (engineering view only): speed, loop, Play motion,
  Pause, Stop motion, and Path through the task areas, which builds a route
  through every task area in plan order. While it plays, the simulated
  position stands in for the feed.

### Step editor (a timeline step)

- start, stop: the step's times. priority: 1 highest to 9, default 5.
- radiates: set by the technique and shown for information.
- Choose IQ file, Choose recording: for IQ playback and replay steps, picked
  from the radio.
- Choose scenario JSON, Check on radio: for a comm waveform step. The chooser
  takes an upload from this console or a file on the radio. The line under it
  is the spec's summary and the radio's fit verdict. Check on radio asks
  again, for example after the radio comes online or changes rate.
- options: the technique's own settings, then RF center (RX and TX), TX
  frequency, TX atten and RX gain. Blank keeps what the radio has.
- location: start area and stop area. Each is none, a point with a radius
  (type it or Pick on the map) or a polygon (Draw on the map). An area that
  came from a dropped file shows its name from the file under it and keeps it
  through Apply, and the map label starts with that name.
- params as JSON: a read-only view of what the step will send.
- Apply changes: writes the edits into the plan. Edited fields are amber until
  then. Delete step: removes the step.

### Step editor (a location task)

- trigger: when it runs (while inside the area, or at the point for a set
  time), run for, at most, confirm (first entry then automatic, every entry,
  or automatic) and priority (default 3).
- area: the task's area, as above. A task made from a dropped file shows the
  area's name from the file.

### Trigger prompts

- KEY IT NOW: keys the step or task now. It is disabled while the radio is
  not ready, the authorization is missing or the step has errors. Don't key
  it: keys nothing, and the area re-arms when the platform leaves it.
- STOP IT NOW, Keep running: for a stop area entered while the step runs.

### Audit log

- Every edit, import, confirm, trigger, run, command result and stop, with a
  time, newest at the bottom.

### Clocks on the aircraft: 1 PPS, no 10 MHz

The aircraft gives the radio a 1 PPS and no 10 MHz is promised. What that
means for a plan:

- Steps are keyed from the console clock. The console UTC chip is the time a
  "Start all at planned UTC times" plan runs by, and nothing on the radio is
  consulted for the moment a step keys. So before a UTC plan the console clock
  is what must be right. Check it against the aircraft feed time (the chip
  "aircraft feed time +x.x s vs console" in the strip, or the Aircraft feed
  panel) and against a radio's GPS time when it has a fix.
- The radio's own time matters for its logs and recordings, not for keying.
  Sync radio clocks pushes the console time to each radio host; if a radio
  then reports a GPS time more than 1 s from the console, the planner warns
  that the console itself may be off, since it has just been copied to the
  radios.
- The G40's reference clock. DPMG does not key, time stamp or discipline
  anything from the 1 PPS. With a 1 PPS alone set the radio's clock to "1 PPS
  only (own oscillator)": the pulse goes to the cards for their time stamps and
  the clock is the radio's own, shown against the pulse in ppm. The external
  10 MHz choices are for a 10 MHz on REF; the service refuses them when the
  HMC7044 does not lock, and shows 10 MHz LOST if it goes away later. gpsdo
  needs the GPS antenna and its 3.3 V bias. The radio's chip shows which clock
  is in use and whether a 1 PPS is present on the external or the internal
  line. "1 PPS present" with clock internal means the GPS module's own pulse is
  there and unused.
- What the planner shows. The console chip is the keying clock. Each radio
  chip shows its clock source, its reference lock, its 1 PPS, its GPS time
  against the console and its OS clock against the console. The feed time
  chip shows the aircraft's clock against the console.

### Clocks: GPSDO lock and GPS fix

A GPSDO lock is not the same as GPS time. On a G40 the cards take their 1 PPS on
the external source (the carrier pin), and with that source the card's GPSDO
uses the pulse whether or not the GPS module has a fix (Sidekiq Software
Development Manual 4.25.0-1, p59). The GPS module gives a pulse before it has a
fix, so "locked" can mean locked to a free running 1 PPS. G40-0001 did exactly
that on 8 November 2026: locked a few seconds after the antenna bias went on, with
one satellite heard and no fix.

So every place that shows the lock shows the fix next to it: the radio chip in
the clock strip, the radio line in Mission radios, the operator TUNING card and
its status line, the engineering SDR tab's ref status, and the health log
(gps_fix, gps_sats and gpsdo columns, LOGGING.md).

| Shown | Colour | Means |
|---|---|---|
| GPSDO locked, GPS fix 3D, 17 sats | green | locked, and the receiver has a 2D or 3D fix |
| locked to 1 PPS, no GPS fix | amber | locked to the module's pulse with no fix: not GPS time |
| locked to 1 PPS, GPS fix not read | amber | locked; gpsd did not answer, or an older service sent no fix |
| GPSDO acquiring, ... | amber | not locked yet |
| X40 GPSDO, GPS fix 3D, 19 sats, lock not readable | grey | the X40 GPSDO with a fix; its lock cannot be read |
| X40 GPSDO, no GPS fix, not disciplined to GPS | amber | the X40 GPSDO with no fix |

Wait for green before trusting a G40 on gpsdo; allow a few minutes after the bias
goes on (a cold start).

The X40. Its GPSDO is a hardware GPSDO in its u-blox LEA-M8F, run from the
ZynqMP, and Epiq documents it as not configurable through libsidekiq (Sidekiq API
4.25.0-1 p26, the same in 4.27.0 p30); skiq_gpsdo_enable returns -95 (ENOTSUP) on
an X40. What libsidekiq does offer is the host reference clock, "Host (gpsdo)
reference clock configuration at 40 MHz", with libsidekiq 4.22.1 or later and both
the Jetson and the ZynqMP on BSP v3.0.0 or later (SDM 4.25.0-1 p58 and the 4.22.1
release notes). x40_gpsdo selects that reference and nothing else. What is and is
not known with it:

- The init fails with the reason if libsidekiq or the BSPs refuse the host
  reference; pick internal or external then.
- The GPSDO's lock cannot be read through libsidekiq, so ref_locked stays empty
  and the chip is grey at best. The fix comes from the X40's gpsd on the ZynqMP
  (192.168.55.100:2947 from the Jetson; DPMG_X40_GPSD changes it).
- The antenna bias is the ZynqMP's (on at boot); DPMG cannot switch or read it.
- With no fix the X40 oscillator is not disciplined; its factory figure is
  plus or minus 120 ppb at 25 C (Hardware User Manual v1.4, p46).

The other ways onto GPS for an X40: a GPS disciplined 10 MHz on REF with clock
external, or "platform clock gpsdo" on the ZynqMP (Hardware User Manual p46 to 47,
for hardware before Rev E), which is a lasting setting on the radio made outside
DPMG.

### Clocks, the lasting setup

The laptop is the radios' NTP source. It serves the USB links and the G40 LAN
with chrony, and the radio hosts then follow the laptop within a few minutes,
with or without internet. Set it up once on the laptop:

```bash
sudo deploy/ntp/laptop_chrony.sh
```

Then run this on each radio host, with the laptop's address on that link as
the argument. On a Super Nano it can be left out; the default is the router of
the USB link.

```bash
sudo deploy/ntp/jetson_timesyncd.sh <laptop ip>
```

Sync radio clocks is the immediate version, and Set time by hand covers a
field day with neither internet nor GPS. Full notes are in
the Super Nano repo, docs/B200_ORIN_BRINGUP.md, "Clock sync with the laptop".
