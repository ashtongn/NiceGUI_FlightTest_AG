# Scenarios

Scenario specs are kept as small JSON; the heavy `.iq.bin` renders and `.plan.json`
decode plans are generated on demand (and git-ignored), so this directory stays light.

## `json/`: FSK and 4FSK preset specs

Human-editable generator specs for the JSON scenario builder. Each is one
`sample_rate_hz` and a list of `signals` (modulation, symbol rate, tone spacing,
offset, message, frame). Current set:

    4fsk_dmr  4fsk_wide  fsk4_sweep  fsk_9600_gmsk
    fsk_afsk_bell202  fsk_pocsag_1200  fsk_pocsag_2400  fsk_sweep

`single_freq.cdif` (one folder up) is a field capture for IQ playback.

Build the waveforms and decode plans from them (writes `<name>.iq.bin` and
`<name>.plan.json` here, then push the plan from the SDR tab for RX decode):

    python3 -m dpmg.scenario.gen_scenario_json --presets          # rebuild the whole library
    python3 -m dpmg.scenario.gen_scenario_json my_spec.json       # build one spec

`tests/test_gen_scenario_json.py` decodes every signal in every preset; it builds
its own fixtures in a temp dir, so it needs nothing on disk here.

## Flight-test compose plans

Multi-signal plans (modem sweep, rate sweep, density-25, frequency agility) for the
G40 and X40 are not kept in the tree; regenerate them when needed:

    python3 tools/gen_flight_scenarios.py --both                   # default 1350 MHz
    python3 tools/gen_flight_scenarios.py --both --center 2200e6   # any center band

`--center` (Hz, default 1350e6) sets the band for s1/s2/s3 and the base for the s4
agility hops (fixed offsets of -35/-5/+25/+45 MHz), and is baked into each filename's
MHz tag. The generator is deterministic, so a regenerated set matches any prior one
at the same center.

## `examples/`: the files printed in the users manual

Small, checked examples for someone writing a file by hand, the same ones shown in
docs/manuals/DPMG_Users_Manual.pdf: `min_fsk.json` and `two_signals_fec.json` (scenario
specs for the JSON builder), `min_dsss.json` (a DSSS spec), `min_plan.dpmg-mission.json` (a
timed mission plan) and `location_plan.dpmg-mission.json` (a plan with a location task). The
specs build with `python3 -m dpmg.scenario.gen_scenario_json`, and the plans load with Open
file in the mission planner; set the radio to your own unit first.
