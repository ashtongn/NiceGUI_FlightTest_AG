# Operator logging

The Log information button at the bottom of the operator view records detections,
position, SDR health and decoded messages for the radio in focus.

## Where the logging lives

With Local computer picked, the logging belongs to the GUI process, one set of
files per radio serial (`dpmg/link/ground_log.py`). It carries on with no browser
open, so moving to the mission view, reloading or closing the tab does not stop it,
and the line beside the button shows the current state when you come back. Opening the
dialog again shows what is ticked and the file paths. Only Stop logging, or the GUI
itself stopping, ends it. After a GUI restart nothing is being logged locally until
you press Start logging.

With On the radio (G40) picked, the radio writes its own files and reports them in
get_config, and that report is what the line shows. It keeps going through a GUI
restart, and Stop logging on any operator page turns it off.

The radio keeps no decode log, so decoded messages are saved on this computer
whichever destination is picked.

The line reads like `logging detections, health to local` or, for both kinds at
once, `logging decodes to local, detections to G40`.

## Laptop files

These are in `output/` beside the code (`/app/output` in the container, bind-mounted
from the repo). Each press of Start logging opens a new set, named with the local time
of the press, as `health_<serial>_<date>_<time>.csv`. The dialog lists the
names. Older files named `health_<serial>.csv` keep their older columns.

| File | Columns | Rate |
|---|---|---|
| `detections_<serial>_<start>.csv` | gcs_time, t_unix, freq_hz, snr_db, dur_ms | each new detection |
| `position_<serial>_<start>.csv` | gcs_time, t_unix, lat, lon, alt_m, source, heading_deg, speed_kt, fix_mode, sats, aircraft | about 1 Hz |
| `health_<serial>_<start>.csv` | gcs_time, t_unix, state, overruns, underruns, fps, uptime_s, clock, ref_locked, clock_vs_pps_ppm, ten_mhz, gps_fix, gps_sats, gpsdo | about 1 Hz |
| `decodes_<serial>_<start>.csv` | gcs_time, t_unix, band, freq_hz, modulation, message, frames | when a band changes |

gcs_time is the ground station's clock as `YYYY-MM-DD HH:MM:SS`, in the time zone of
the browser that pressed Start logging, because the GUI container itself runs in UTC.
The header names the zone, as in `gcs_time (UTC-05:00)`. If the browser does not
answer, it falls back to the GUI's own zone, which is UTC in the container. t_unix is
the same moment as a count of seconds since 1 January 1970 UTC: it reads the same in
any zone and is the column to use to line these files up with the radio's own files.

The radio's own files, detections.csv and telemetry.csv on the radio, start with
`g40_time_utc` (the radio kind, then `_time_utc`): the radio's clock, always in UTC,
then t_unix from the same clock. The two clocks are not the same clock. The planner's
status strip shows each radio's offset from the ground station.

The first detections file can hold rows from before the press: the radio's recent
detections, each with its own time. A detection or a decode goes in one file only, so
a new file does not repeat what the last one already has.

The position file's source column says where the row came from: `status` is the
radio's own GPS from its STATUS heartbeat, `acft_mcast` the aircraft feed, `no_fix`
neither. The last four columns are filled on `acft_mcast` rows only, and only when
the feed carried them: heading_deg is the course over ground in degrees true (VTG or
RMC, or a JSON heading key), speed_kt the ground speed in knots, fix_mode `2D`, `3D`
or `none` (GSA), sats the satellites used (GGA or GSA). A radio's STATUS has none of
these, so they are blank on a `status` row. The aircraft column names the
aircraft feed this radio follows: its plan row's aircraft, else the first on the
saved list. It is filled on `acft_mcast` and `no_fix` rows and blank on a
`status` row. With one aircraft it holds that aircraft's name, which is its group
and port (239.2.1.1:5005) until it is renamed. docs/MISSION_PLANNER.md section 7a lists
the sentences and JSON keys the feed reader takes.

A gap in the health file means the link to the radio was down: the last status of a
lost link is not repeated. Position then falls back to the aircraft feed or a
`no_fix` row. Rows stop being added while the radio is gone and start again on their
own when it returns. If a write fails (a full disk, say) the logging stops and the
line says why.

## File ownership

Snap LibreOffice refuses a file in the home folder that the user does not own, however
readable it is, with "source file could not be loaded". The GUI container runs as root,
so everything below `output/` is given to the owner of `output/`: each log file when it
is opened, and the whole folder when the GUI starts and at each Start logging. A file
another part of the GUI writes there is corrected at the next of those. If it cannot
be done the GUI warns once in its log and logging carries on. To check, this should
print nothing:

    find output ! -user "$(stat -c %U output)"

## The decodes file

There is one row each time a band's frame count or message changes, the same data
as the Decodes table.

- gcs_time and t_unix are when the GUI saw the change, not when the radio decoded the frame. The
  time of the decode is not on the wire.
- frames is the radio's running decode count for that band. It can go up by more
  than one between rows, because the radio sends only the latest message for each
  band, twice a second. Messages between two rows are not recorded.
- message is cut to 48 characters by the radio. A secure message stays as its
  `[sealed ...]` text: the plain text is not written to disk.
- Messages can hold commas and quotes, so read this file with a CSV reader rather
  than splitting on commas.

## What ref_locked tells you, and the two clock columns

`ref_locked` in `health_<serial>_<start>.csv`, and in the radio's own telemetry CSV, is the
reference flag from the radio's status. On a G40 it means:

| Clock chosen in DPMG | ref_locked |
|---|---|
| internal, 1 PPS only | always 0, there is nothing to lock to |
| gpsdo | the card's GPSDO lock, which can be a lock to a free running 1 PPS: read gps_fix with it |
| external 10 MHz and 1 PPS, external 10 MHz only | 1 while the 10 MHz is in use, 0 when it is LOST or not yet measured |

On an external 10 MHz the G40 service sets the HMC7044 to the REF jack at init and
refuses the init when the chip does not lock. From then on it reads the card clock
against the 1 PPS every 2 s: the FPGA stamps the card's 40 MHz counter at each pulse,
and a clock running on the 10 MHz counts 40,000,000 ticks a second within a fraction
of a ppm, while a clock that has lost it runs about 137 ppm low (G40-0002, 5 November
2026). `clock_vs_pps_ppm` is that reading and `ten_mhz` is the verdict: `in use`
(within 10 ppm), `LOST` (beyond it, or the HMC7044 not locked) or `not measured`
(no 1 PPS to count against). Both are blank on internal and gpsdo, and on 1 PPS only
the ppm column alone is filled: the radio's own oscillator against the aircraft's
pulse, for information. A lost 10 MHz shows in these columns within a few seconds of
the pull. The radio's telemetry CSV has the same two columns at the end.

## The GPS fix columns

`gps_fix` (3D, 2D or none) and `gps_sats` (satellites used) are the GNSS fix the
radio service last read from its gpsd, carried in get_config. `gpsdo` is the verdict
shown in the GUI: `locked` (GPSDO locked with a 2D or 3D fix), `no_fix` (locked with
no fix, that is locked to the GPS module's free running 1 PPS, or the X40 GPSDO with
no fix), `acquiring` (not locked yet), `unread` (the X40 GPSDO with a fix; its lock
cannot be read through libsidekiq). All three are blank when the service sends no fix
(an older service, or a radio with no GPS receiver such as the B200), and `gpsdo` is
blank on a clock that is not GPS disciplined. On an X40 the fix comes from the gpsd on
its ZynqMP. MISSION_PLANNER.md, appendix "Clocks: GPSDO lock and GPS fix", has why a
lock alone is not GPS time.

The same reading by hand, with the radio closed: run `pps_tester --displayts` on the
G40 and feed its output to `bench/pps_rate.py`, which counts the ticks of the card
clock per pulse the same way.
