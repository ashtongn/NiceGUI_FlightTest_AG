# Flight test files

What the flight test cards load or run, kept with the code they run against. Paths on the cards are from the root of this flight copy.

| Folder | What is in it |
|---|---|
| `plans/` | Mission files the operator loads in the mission view (Open file). `plans/pairs/` holds the two radio plans and the open air go or no-go; `plans/bench/` the same two radio plans and a tone each way check; `plans/cabled/` the two radio plans for the cabled ground layer (GROUND-02), with the uplink at a low TX attenuation and the payload RX attenuator open. `make_flight_plans.py` writes the whole set and checks it; `README.md` lists every plan. |
| `files/` | Example IQ files for the file playback card, copied to the radio as `data/waveforms/flight/`. `make_example_files.py` writes them; `MANIFEST.md` gives each file's rate and hash. |
| `site_kit/` | Scripts that load the flight code onto the payload G40 at the site, with a rollback. `README.md` and `ROLLBACK.md` describe them. Bundles built by `make_site_bundle.sh` go in `site_kit/bundle/`, which git ignores. |

Radios in the plans are named by their serial names: G40-AX3G is the G40 in Payload 2 (USB-C address), G40-AX35 the ground G40 and X40-B00S the ground X40, so each row binds to its own radio when it comes online. A row that stays offline (another radio stood in) is bound in Mission radios with "bind to a connected radio". The location tasks are points with a radius, typed in the step editor once the route is known.
