# Site kit: the flight code onto the payload G40 at the site

For "Loading the latest code onto Payload 2" in the troubleshooting section of the flight test
field cards. The payload G40 runs the code of its last radio
deploy (5 November). This folder loads the flight radio code at the site, within 30 minutes, with
a rollback. Laptop side only: nothing in this folder has been run against a radio.

| File | What |
|---|---|
| `make_site_bundle.sh` | builds `bundle/<id>/` from any commit of this flight copy (default HEAD) |
| `deploy_files.txt` | the radio files that are pushed, and the code differences left out on purpose |
| `bundle/<id>/` | the archive (with native/lib/librxdrain.so added), its SHA256, the library's SHA256, the deploy set, the radio diff against the payload's 5 November code; `bundle/CURRENT` names the one the scripts use |
| `site_env.sh` | shared settings: radio address, one SSH login reused by every step, results folder |
| `site_read_state.sh` | read only: code hashes, containers, modules, rf_frontend.yaml, the drain library hash and the service's drain engine line, the service read; phases before, after, final, rollback |
| `site_deploy.sh` | plan, then go: rollback image, push_update.sh, container recreate, read back |
| `site_rollback.sh` | files (level 1) or container (level 2) |
| `ROLLBACK.md` | the rollback as steps, with the by-hand commands |
| `SITE_PACK.md` | the site laptop pack checklist |
| `UNCERTAIN.md` | what is not known about the payload's state and the deploy |
| `tools/` | radio_closure.py (the service's imports), ast_same.py (comment-only check), site_query.py (read-only service read), old_run_cmd.py (old container run line) |

## Current bundle

Built 9 November 2026 from main after the two merges of that morning:

- commit `8beb544553c5bc5a156d1ec106ebced67bff2487` ("merge fix/x40-clock-gps-fix: X40 clock choices without the FPGA GPSDO, GPS fix beside every GPSDO lock")
- `bundle/8beb544/dpmg_flight_8beb544.tar.gz`, 25,404,080 bytes
- SHA256 `c09e30798a34d7f5145c73eb8b1d86d789597eb0b43fe4d4d1c71db5185c1f05`
- native/lib/librxdrain.so in the archive: 8,352 bytes, aarch64, SHA256
  `de8d9a2440830f3b7962f6e4bc94ec6e9623ba91f7c3772a6a32dec55c183114` (the build on the bench G40 and in
  the 5 November snapshot)
- built twice, same SHA256

The first bundle, `bundle/70f7f77/` (SHA256 4c47feb8...), is kept for reference only; it has no
drain library.

The native receive drain (native/lib/librxdrain.so) is ignored by git, so `git archive` alone
leaves it out and a radio tree made from such an archive falls back to the Python drain (overruns
at 61.44 MS/s). make_site_bundle.sh adds it from the flight copy (NATIVE_LIB overrides the path)
and stops unless it is an aarch64 library whose SHA256 starts with de8d9a2440830f3b (NATIVE_SHA).

Rebuild from the final commit before the flight: `bash make_site_bundle.sh <commit>`. The
script compares against the 5 November snapshot when REF_TGZ names it; without it the stored
reference hashes in `REFERENCE_PAYLOAD.sha256` are used.

## What goes onto the radio, and why only that

The radio-side files are the ones the service imports (99 with the radio scripts and the drain
library). Against the payload's 5 November code at 8beb544, 60 differ only in comments and
docstrings (the comment pass of 5 November evening, never pushed to a radio) and 29 are the same,
the drain library among them. Ten differ in code:

| File | Pushed | Why |
|---|---|---|
| dpmg/radio/dpmg_service.py | yes | clock choices and meter, log time columns, port_jacks in caps |
| dpmg/radio/g40_clock.py | yes | HMC7044 input, PCC.00, verdicts, the print_debug_msg fix |
| dpmg/radio/sdr_core.py | yes | the G40 clock sequence |
| dpmg/radio/skiq_bridge.py | yes | 1 PPS source write and timestamp read |
| dpmg/radio/port_map.py | yes | dpmg_service.py at 19872bf and later imports its new functions |
| dpmg/radio/gps_antenna.py | yes | simulator only (bias_on, no_fix); dpmg_service.py at 516282f passes bias_on |
| dpmg/dsp/demod_helper.py | yes | uplink carrier acquisition at 9600 Bd (2aec9d2); runs in the service's decode pool |
| deploy/radio/g40_up_r36.sh | yes | the recreate with the /epiq and hmc_from_file mounts |
| dpmg/scenario/gen_scenario_json.py | no | peak limit 1.0, never run on a radio; the bench G40 keeps 0.95 |
| dpmg/paths.py | no | a GUI output file helper; nothing on the radio calls it |

The library is pushed as well (deploy_files.txt), so its hash on the radio is known after the
deploy; it is expected to be the same build the payload already has. The clock files plus
g40_up_r36.sh are what the bench G40 got on 8 November; the bench G40 runs dpmg_service.py and port_map.py
from before 19872bf, and nothing of the 9 November merges (see UNCERTAIN.md, item 2). Pushing the
whole tree would bring the comment-only files too: harmless, but it moves 70 files instead of 9
and loses the one to one match with what the bench G40 runs.

## Order at the site

Steps: `site_read_state.sh before`, `site_deploy.sh plan`, `site_deploy.sh go` (logs
each step with the elapsed time), the RX-only init from the GUI, `site_read_state.sh final`, then `site_no_tx_floor.sh plan` and `go`.
Any NOT OK or the 30 minutes up: `site_rollback.sh files`, then `container` if needed.
Results in `~/dpmg_site_results/<date>_site/` unless SITE_RESULTS names another folder.

## Checked offline on 9 November

- `make_site_bundle.sh` on HEAD: archive, deploy set, radio diff; same SHA256 on a second build.
- The reference: the five radio files of the 5 November 09:09 deploy to the payload G40 match the
  snapshot hash for hash.
- `site_query.py read` and `compare` against a private simulator from the bundle on spare ports
  (47872/47873, no radio, not the 8080 GUI): five clock choices, guard, HPA line, compare all ok.
- The container pre-check of `site_deploy.sh` and `old_run_cmd.py` on a made-up docker inspect
  shaped like the bench G40's before its recreate: no problem found on the clean one; a changed image, a
  missing dpmg-gpu container, an extra service argument and an extra environment value each
  reported.
- The library and drain engine checks of `site_read_state.sh` with a stand-in ssh and docker on
  a copy of the bundle tree: native and matching gives ok; the Python engine, a missing library,
  and no drain engine line at the final read each give NOT OK; no line at the after read gives
  "later"; a library missing before the deploy gives a note at the rollback read.
- `bash -n` on every script. The SSH parts, the push and the recreate have not been run.

## site_no_tx_floor.sh (added 9 November 2026)

The 20 dB pad in front of the HPA is the protection, and the payload G40 must be
able to go to 0 dB TX attenuation. The 13 dB floor came from the payload line of the radio's
rf_frontend.yaml (load_power_dbm 47, load_margin_db 3, load_point switch). This script removes
those three keys from that line on the radio, keeps a dated copy beside the file, restarts
dpmg_svc and reads back that no load limit and no LNA floor are left. Everything else in the file
stays. The site kit's deploy does not push rf_frontend.yaml, so a later deploy does not bring the
floor back. Run it once, after the deploy's final read if there is a deploy. Tested on a copy of the 3 November as-built
file (payload line becomes {profile: P2, auto_protect: true, restore_rx_atten: true}); not yet run
on a radio, as the payload G40 is not on the bench and the bench G40 has no payload line.
