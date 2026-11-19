#!/usr/bin/env python3
"""Mission files for a short flight that stays close to the ground antenna.

  python3 make_near_plans.py
  python3 make_near_plans.py --limit 5 --unit mi --gates 1,2,3,4
  python3 make_near_plans.py --unit nm

Written to plans/near/ next to this script, with a README.

The big flight plans (trig_location, pairs/) have range gates from 5 to 30 km
and a far end at 34 km. An aircraft that goes no further than about five miles
from the ground antenna crosses only the first of them. These plans put the
gates inside that distance.

Each gate is the part of the flight box between two ranges from the ground
radio site, a band across the box, so any track through the box crosses it.
A gate keys its range message for a set time when the aircraft enters it and is
ready again once the aircraft has left it, so it keys going out and again
coming back. The outer band, just inside the limit, keys its message while the
aircraft is in it. --rings full makes every gate a full ring around the site
for a flight that is not held to the box.

Three files:

  near_gates              the payload alone
  near_gates_g40_pair     the same, with the ground G40 receiving for the whole
                          flight and decoding every range message
  near_gates_full_power   the same gates keying a jammer each at
                          --full-power-atten, the drive that puts the HPA at
                          its rated output, with the outer band keyed for a
                          shorter time; the ground G40 receives

Every message is the same length, so the ground radio's one decode entry
frames them all. Every gate is automatic: none asks for a confirm, because a
confirm prompt left unanswered on a page that is then reloaded is not shown
again. --confirm-first once puts the confirm back on the nearest gate.

Plans are checked with the repo's mission_file. Nothing here touches a radio.
"""
from __future__ import annotations

import argparse
import copy
import datetime
import json
import math
import os
import sys
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "near")
sys.path.insert(0, HERE)
import make_flight_plans as fp  # noqa: E402

UNIT_M = {"mi": 1609.344, "nm": 1852.0, "km": 1000.0}
UNIT_NAME = {"mi": "statute miles", "nm": "nautical miles", "km": "km"}
FLIGHT_BOX = "43.916667,-112.750000 43.916667,-112.650000 43.550000,-112.833333 43.583333,-112.933333"


def pts(text):
    return [[float(p.split(",")[0]), float(p.split(",")[1])] for p in text.split()]


def circle(site, radius_m, n=72):
    return [fp._dest(site, i * 360.0 / n, radius_m) for i in range(n)]


def ring(a, r0_m, r1_m):
    # the band from r0 to r1 around the site, as [lon, lat] vertices: the part
    # inside a.box_pts, which is the flight box, or a circle for full rings
    return fp._band(a, r0_m, r1_m)


def bearing_deg(p, q):
    la1, lo1, la2, lo2 = map(math.radians, (p[0], p[1], q[0], q[1]))
    y = math.sin(lo2 - lo1) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1)
    return math.degrees(math.atan2(y, x)) % 360.0


def areas(a):
    # (step id, message, polygon, mode, key or max seconds, confirm, inner m, outer m)
    u, tag = UNIT_M[a.unit], a.unit.upper()
    out = []
    for k, g in enumerate(a.gate_list):
        confirm = a.confirm_first if k == 0 else "auto"
        out.append((f"near_r{k + 1:02d}", f"RANGE {g:04.1f} {tag}", ring(a, g * u, g * u + a.gate_depth_m),
                    "dwell", a.gate_key_s, confirm, g * u, g * u + a.gate_depth_m))
    r0 = (a.limit - a.outer_depth) * u
    out.append(("near_outer", f"OUTER {a.limit:04.1f} {tag}", ring(a, r0, a.limit * u),
                "while_inside", a.outer_max_s, "auto", r0, a.limit * u))
    return out


def map_block(a):
    u = UNIT_M[a.unit]
    limit = [{"id": "ov-near-limit", "name": f"{a.limit:g} {a.unit} from the ground radio",
              "source": "make_near_plans.py --limit", "color": "#2e7d32", "lines": [],
              "polygons": [{"name": f"{a.limit:g} {a.unit} limit",
                            "coords": [[p[1], p[0]] for p in circle(a.site_pt, a.limit * u)]}],
              "points": []}] if a.rings == "full" else []
    return {"flight_path": [[p[1], p[0]] for p in a.route_pts], "flight_path_source": "make_near_plans.py --bearing",
            "overlays": limit + [
                {"id": "ov-flight-box", "name": "Flight box", "source": "make_near_plans.py --flight-box",
                 "color": "#e65100", "lines": [],
                 "polygons": [{"name": "Flight box", "coords": [[p[1], p[0]] for p in a.real_box]}],
                 "points": []},
                {"id": "ov-ground-site", "name": "Ground radio site", "source": "make_near_plans.py --ground-site",
                 "color": "#1565c0", "lines": [], "polygons": [],
                 "points": [{"name": "Ground radio site", "lat": a.site_pt[0], "lon": a.site_pt[1]}]}]}


def add_location_tasks(a, m):
    m["map"] = map_block(a)
    for sid, msg, poly, mode, length, confirm, _r0, _r1 in areas(a):
        sp = fp.spec(sid.replace("near_", "gate_"), 1_920_000, a, [fp._area_signal(msg)])
        step = fp.comm_step(sid, a, sp, 0.0, 60.0, a.tx_atten)
        step.pop("schedule", None)
        step["location"] = {"start": {"polygon": poly}}
        step["priority"] = 3
        step["trigger"] = {"mode": mode, "dwell_s": float(length if mode == "dwell" else 20.0),
                           "max_s": float(length if mode != "dwell" else 120.0), "confirm": confirm}
        m["steps"].append(step)


def what(a):
    gates = ", ".join(f"{g:g}" for g in a.gate_list)
    shape = "a full ring" if a.rings == "full" else "a band across the flight box"
    return (f"Short flight, no further than {a.limit:g} {UNIT_NAME[a.unit]} from the ground radio site. Range gates "
            f"at {gates} {a.unit} from the site, each {shape} {a.gate_depth_m:g} m deep, keying its range message "
            f"for {a.gate_key_s:g} s when the aircraft enters it, going out and coming back. The outer band, from "
            f"{a.limit - a.outer_depth:g} to {a.limit:g} {a.unit}, keys its message while the aircraft is in it "
            f"(max {a.outer_max_s:g} s a visit). TX attenuation {a.tx_atten:g} dB on {a.tx_port}. "
            "Location triggers on and the aircraft feed listening before the aircraft moves.")


def plan_alone(a):
    m = fp.base(f"Near flight: range gates inside {a.limit:g} {a.unit}", what(a) + " The payload alone.", a)
    m["radios"][0]["sample_rate"] = 1_920_000
    add_location_tasks(a, m)
    return m


def plan_pair(a):
    g = fp.Ground("g40", a.ground, a.ground_address)
    m = fp.base(f"Near flight with the ground G40: range gates inside {a.limit:g} {a.unit}",
                what(a) + f" {a.ground} receives on {g.rx_port} from 5 s after Run mission for "
                f"{a.decode_s / 60:g} min and decodes every range message.", a,
                radios=[fp.air_radio(a, 1_920_000), fp.ground_radio(g, 1_920_000)])
    sp = fp.spec("near_all", 1_920_000, a, [fp._area_signal(areas(a)[0][1])])
    m["steps"].append(fp.decode_step("near_rx_all", a, sp, 5.0, 5.0 + a.decode_s, g.radio, g.rx_port, None))
    add_location_tasks(a, m)
    return m


# the full power file keys a jammer on each area instead of a comm message: at this drive the
# HPA is in compression and the comm waveforms are not expected to decode. The 20 dB pad in
# front of the HPA keeps its input under the damage limit at any TX attenuation, 0 dB included.
# What limits the drive is the T/R switch after the HPA (--switch-max-dbm).
# All at half scale. The first three have a constant envelope, so their average power is
# their peak power; the comb and the barrage have the same peak and a lower average.
FULL_POWER_JAMMERS = {
    # area step id: (technique, kind, label, fields at the sample rate)
    "near_r01": ("jammer.tone", "tone", "spot tone", lambda fs: {"freq": fs / 8.0}),
    "near_r02": ("jammer.swept", "swept", "swept",
                 lambda fs: {"span": 0.8 * fs, "sweep_period_s": 0.001, "noise_bw": 0.0, "center": 0.0}),
    "near_r03": ("jammer.chirp", "chirp", "LFM chirp", lambda fs: {"f0": -0.4 * fs, "f1": 0.4 * fs}),
    "near_r04": ("jammer.comb", "comb", "comb, 8 tones", lambda fs: {"spacing": fs / 16.0, "n_tones": 8, "center": 0.0}),
    "near_outer": ("jammer.barrage", "barrage", "barrage noise", lambda fs: {}),
}
FULL_POWER_DEFAULT = ("jammer.tone", "tone", "spot tone", lambda fs: {"freq": fs / 8.0})


def plan_full_power(a):
    """Jammers on the range gates at the full power drive, the ground G40 receiving.
    Its own namespace, so the other two files keep theirs. Returns (plan, namespace, rows for the README)."""
    b = copy.copy(a)
    b.tx_atten = a.full_power_atten
    b.outer_max_s = a.full_power_outer_max_s
    fs = 1_920_000
    g = fp.Ground("g40", a.ground, a.ground_address)
    # what the payload model gives for a half scale constant envelope jammer at this drive
    try:
        sys.path.insert(0, a.repo)
        from dpmg.payload import payload_budget as pb
        e = pb.eirp_chain(pb.profile(pb.load_profiles(), a.profile), a.center / 1e6, b.tx_atten, 6.02)
        b.switch_in = float(e["switch_in_dbm"])
        b.model = (f"By the payload model the HPA puts out about {10 ** ((e['hpa_out_dbm'] - 30) / 10):.0f} W on them, "
                   f"{e['hpa_compression_db']:.1f} dB into compression: {b.switch_in:.1f} dBm into the T/R switch "
                   f"against its {a.switch_max_dbm:g} dBm rating, about "
                   f"{10 ** ((e['ant_in_dbm'] - 30) / 10):.0f} W at the antenna and {e['eirp_w']:.0f} W EIRP.")
    except Exception:
        b.switch_in = None
        b.model = "The payload model was not read, so no power figure is given here."
    if b.switch_in is not None and b.switch_in > a.switch_max_dbm:
        sys.exit(f"full power file: TX attenuation {b.tx_atten:g} dB puts {b.switch_in:.1f} dBm into the T/R switch, "
                 f"over its {a.switch_max_dbm:g} dBm rating. Raise --full-power-atten. Not written.")
    gates = ", ".join(f"{x:g}" for x in a.gate_list)
    shape = "a full ring" if a.rings == "full" else "a band across the flight box"
    m = fp.base(f"Near flight at full power: a jammer on each range gate inside {a.limit:g} {a.unit}",
                f"Short flight, no further than {a.limit:g} {UNIT_NAME[a.unit]} from the ground radio site. Range "
                f"gates at {gates} {a.unit} from the site, each {shape} {a.gate_depth_m:g} m deep, keying a jammer "
                f"for {a.gate_key_s:g} s when the aircraft enters it, going out and coming back. The outer band, "
                f"from {a.limit - a.outer_depth:g} to {a.limit:g} {a.unit}, keys its jammer while the aircraft is in "
                f"it (max {b.outer_max_s:g} s a visit). Every jammer at half scale and TX attenuation "
                f"{b.tx_atten:g} dB on {a.tx_port}. The tone, the sweep and the chirp have a constant envelope. "
                f"{b.model} The comb and the barrage have the same peak and a lower average. The 20 dB pad "
                f"keeps the HPA input under its limit at this drive. Watch the HPA current. {a.ground} receives on {g.rx_port} from 5 s after "
                f"Run mission for {a.decode_s / 60:g} min, for its spectrum and detections log; no decodes are "
                f"expected. Location triggers on and the aircraft feed listening before the aircraft moves.",
                a, radios=[fp.air_radio(a, fs), fp.ground_radio(g, fs)])
    sp = fp.spec("near_all", fs, a, [fp._area_signal(areas(a)[0][1])])
    m["steps"].append(fp.decode_step("near_rx_all", a, sp, 5.0, 5.0 + a.decode_s, g.radio, g.rx_port, None))
    m["map"] = map_block(a)
    table = []
    for sid, _msg, poly, mode, _length, confirm, r0, r1 in areas(b):
        tech, kind, label, fields = FULL_POWER_JAMMERS.get(sid, FULL_POWER_DEFAULT)
        length = b.outer_max_s if mode != "dwell" else a.gate_key_s
        step = fp.jam_step(sid, b, tech, kind, fields(fs), 0.0, 60.0, b.tx_atten, ampl=0.5)
        step.pop("schedule", None)
        step["location"] = {"start": {"polygon": poly}}
        step["priority"] = 3
        step["trigger"] = {"mode": mode, "dwell_s": float(length if mode == "dwell" else 20.0),
                           "max_s": float(length if mode != "dwell" else 120.0), "confirm": confirm}
        m["steps"].append(step)
        keys = f"{length:g} s on entry" if mode == "dwell" else f"while inside, max {length:g} s"
        table.append(f"| {sid} | {label} | {r0 / UNIT_M[a.unit]:.2f} | {r1 / UNIT_M[a.unit]:.2f} | {keys} |")
    return m, b, table


def crossings(a):
    """Seconds along the route, at --speed, where each area is entered."""
    out = {}
    polys = [(sid, [[c[1], c[0]] for c in poly]) for sid, _m, poly, *_ in areas(a)]
    t, step_m = 0.0, 20.0
    was = {sid: False for sid, _ in polys}
    for p, q in zip(a.route_pts, a.route_pts[1:]):
        d = fp.geo_range_m(p, q)
        n = max(1, int(d / step_m))
        for k in range(n + 1):
            pt = [p[0] + (q[0] - p[0]) * k / n, p[1] + (q[1] - p[1]) * k / n]
            for sid, poly in polys:
                inside = fp._in_box(SimpleNamespace(box_pts=poly), pt)
                if inside and not was[sid]:
                    out.setdefault(sid, []).append(t + d * k / n / a.speed)
                was[sid] = inside
        t += d / a.speed
    return out, t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.path.normpath(os.path.join(HERE, "..", "..")),
                    help="the flight repo, for the plan check")
    ap.add_argument("--unit", choices=sorted(UNIT_M), default="mi", help="unit of --limit and --gates")
    ap.add_argument("--limit", type=float, default=5.0, help="furthest the aircraft goes from the ground radio")
    ap.add_argument("--gates", default="1,2,3,4", help="ranges of the gates from the ground radio")
    ap.add_argument("--gate-depth-m", type=float, default=400.0, help="depth of each gate (m)")
    ap.add_argument("--gate-key-s", type=float, default=15.0, help="how long a gate keys once entered (s)")
    ap.add_argument("--outer-depth", type=float, default=0.5, help="depth of the outer ring, in --unit")
    ap.add_argument("--outer-max-s", type=float, default=120.0, help="longest the outer ring keys in one visit (s)")
    ap.add_argument("--confirm-first", choices=["auto", "once"], default="auto",
                    help="confirm of the nearest gate; the rest are automatic")
    ap.add_argument("--ground-site", default="43.598621,-112.905566", help="lat,lon of the ground radio")
    ap.add_argument("--flight-box", default=FLIGHT_BOX, help="lat,lon corners of the flight box, drawn on the map")
    ap.add_argument("--rings", choices=["box", "full"], default="box",
                    help="box: each gate is the part of the flight box at its range; full: a ring all round the site")
    ap.add_argument("--bearing", type=float, default=None,
                    help="bearing of the out and back route drawn on the map and played on the bench (degrees); "
                         "default: from the site toward the middle of the flight box")
    ap.add_argument("--speed", type=float, default=30.0, help="aircraft ground speed for the times in the README (m/s)")
    ap.add_argument("--center", type=float, default=1325e6, help="RF center of every step (Hz)")
    ap.add_argument("--air-radio", default="G40-AX3G", help="the payload G40's name in the plans")
    ap.add_argument("--air-address", default="192.168.0.23:47810", help="its address as IP:port (blank for none)")
    ap.add_argument("--tx-port", default="TRx2B", help="the payload TX jack")
    ap.add_argument("--air-clock", default="external",
                    choices=["internal", "external", "ext_10mhz", "ext_pps", "gpsdo"],
                    help="clock the payload radio comes up on when Run mission has to set it up: external is "
                         "the 10 MHz and the 1 PPS from the payload clock board")
    ap.add_argument("--tx-atten", type=float, default=30.0, help="payload TX attenuation of every gate (dB)")
    ap.add_argument("--full-power-atten", type=float, default=4.5,
                    help="payload TX attenuation of the full power file (dB): the T/R switch sets it. By the "
                         "payload model 4.5 dB puts a half scale tone 0.9 dB under the switch rating, 3.5 dB "
                         "0.2 dB under, and anything lower over it")
    ap.add_argument("--switch-max-dbm", type=float, default=47.0,
                    help="T/R switch maximum input (dBm), as given by the operator on 17 November 2026; the "
                         "full power file is not written over it")
    ap.add_argument("--profile", default="P2", help="payload profile for the power figure in the full power file's notes")
    ap.add_argument("--full-power-outer-max-s", type=float, default=30.0,
                    help="longest the outer band keys in one visit in the full power file (s)")
    ap.add_argument("--ground", default="G40-AX35", help="the ground G40's name in the pair plan")
    ap.add_argument("--ground-address", default="192.168.0.22:47810")
    ap.add_argument("--decode-s", type=float, default=3600.0, help="length of the ground radio's decode step (s)")
    ap.add_argument("--date", default=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"))
    ap.add_argument("--auth", default="FLIGHT-2026-11")
    a = ap.parse_args()

    u = UNIT_M[a.unit]
    a.gate_list = sorted(float(x) for x in a.gates.split(",") if x.strip())
    a.site_pt = [float(x) for x in a.ground_site.split(",")]
    a.real_box = pts(a.flight_box)
    # what fp._band clips to: the flight box, or a circle past the limit so every bearing counts
    a.box_pts = a.real_box if a.rings == "box" else circle(a.site_pt, a.limit * u + 50.0)
    if a.bearing is None:
        mid = [sum(q[0] for q in a.real_box) / len(a.real_box), sum(q[1] for q in a.real_box) / len(a.real_box)]
        a.bearing = round(bearing_deg(a.site_pt, mid), 1)
    turn = fp._dest(a.site_pt, a.bearing, (a.limit - a.outer_depth / 2) * u)
    a.route_pts = [a.site_pt, turn, a.site_pt]
    if a.gate_list[-1] * u + a.gate_depth_m > (a.limit - a.outer_depth) * u:
        sys.exit("the last gate runs into the outer ring: move it in or make the outer ring thinner. Not written.")
    gaps = [(b - c) * u - a.gate_depth_m for c, b in zip(a.gate_list, a.gate_list[1:])]
    if gaps and min(gaps) <= 0:
        sys.exit("two gates overlap: space them further apart or make them thinner. Not written.")

    sys.path.insert(0, a.repo)
    from dpmg.mission import mission_file as mf

    os.makedirs(OUT, exist_ok=True)
    rows, bad = [], 0
    first_plan = plan_alone(a)
    full, full_a, full_table = plan_full_power(a)
    for name, m, att in (("near_gates", first_plan, a.tx_atten), ("near_gates_g40_pair", plan_pair(a), a.tx_atten),
                         ("near_gates_full_power", full, full_a.tx_atten)):
        path = os.path.join(OUT, f"{name}.dpmg-mission.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(m, f, indent=1)
            f.write("\n")
        found = mf.validate(mf.load(path))
        errs = [c for c in found if c.level == "error"]
        bad += len(errs)
        radios = ", ".join(r["id"] for r in m["radios"])
        rows.append(f"| `{name}.dpmg-mission.json` | {m['name']} | {radios} | {att:g} | "
                    f"{sum(1 for s in m['steps'] if 'location' in s)} | {'valid' if not errs else 'errors'} |")
        print(f"{name}: {len(m['steps'])} steps, {len(errs)} error(s), {len(found) - len(errs)} other")
        for c in found:
            print(f"  {c.level}: {c.step_id}: {c.message}")

    empty = [sid for sid, _m, poly, *_ in areas(a) if not poly]
    if empty:
        sys.exit(f"no part of the flight box is at the range of {', '.join(empty)}: use --rings full or move the "
                 "gates. Not written.")
    cross, total = crossings(a)
    table = []
    for sid, msg, _poly, mode, length, confirm, r0, r1 in areas(a):
        keys = f"{length:g} s on entry" if mode == "dwell" else f"while inside, max {length:g} s"
        ts = cross.get(sid, [])
        when = ", ".join(f"{int(t // 60):02d}:{int(t % 60):02d}" for t in ts) if ts else "not crossed"
        table.append(f"| {sid} | {msg} | {r0 / u:.2f} | {r1 / u:.2f} | {r0 / 1e3:.2f} | {r1 / 1e3:.2f} | {keys} | "
                     f"{confirm} | {when} |")
        print(f"  {sid}: {msg}, {r0 / 1e3:.2f} to {r1 / 1e3:.2f} km, entered at {when}")

    readme = f"""# Near flight mission files

Written by make_near_plans.py for a flight that goes no further than {a.limit:g} {UNIT_NAME[a.unit]}
({a.limit * u / 1e3:.2f} km) from the ground radio site at {a.site_pt[0]:.6f}, {a.site_pt[1]:.6f}.
Center {a.center / 1e6:g} MHz, 1.92 MS/s, payload {a.air_radio} keying on {a.tx_port} at {a.tx_atten:g} dB.

| File | Plan name | Radios | TX attenuation (dB) | Location tasks | Check |
|---|---|---|---|---|---|
{chr(10).join(rows)}

`near_gates_full_power` keys a jammer on each gate in place of the range message, at half
scale and {a.full_power_atten:g} dB TX attenuation. {full_a.model}
The comm messages are not expected to decode at this drive, which is why it keys jammers. The
20 dB pad in front of the HPA keeps its input under the damage limit at any TX attenuation.
The T/R switch, rated {a.switch_max_dbm:g} dBm at its input, is what sets this file's drive. {a.ground} receives so its spectrum and detections
log show each one. Its outer band keys for {a.full_power_outer_max_s:g} s a visit at most. The
engineering view's Payload tab shows the expected EIRP of each step of the loaded file.

| Step | Jammer | From ({a.unit}) | To ({a.unit}) | Keys |
|---|---|---|---|---|
{chr(10).join(full_table)}

## The gates

{"Each one is a full ring around the ground radio site." if a.rings == "full" else "Each one is the part of the flight box at its range from the ground radio site, a band from one side of the box to the other."} The times are for the route drawn
on the map: straight out on bearing {a.bearing:g} degrees to {(a.limit - a.outer_depth / 2):g} {a.unit} and
straight back, at {a.speed:g} m/s, {int(total // 60)} min {int(total % 60)} s in all.

| Step | Message | From ({a.unit}) | To ({a.unit}) | From (km) | To (km) | Keys | Confirm | Entered at (mm:ss) |
|---|---|---|---|---|---|---|---|---|
{chr(10).join(table)}

A gate keys once when the aircraft enters it and is ready again when the aircraft has
left it. An aircraft that circles inside a gate keys it once, not over and over.

## Run it

1. Radios connected. Aircraft feed listening and showing the aircraft's position.
2. Mission view, Open file. Check plan: no errors.
3. Run mission, then switch the location triggers on. Do this before the aircraft moves.
4. Watch the Live feed row and the audit log: each gate logs when it keys.
5. With the pair file, watch Decodes on {a.ground}: one message for each gate crossed.
6. STOP ALL TX to end.

On the bench, with no aircraft, play the route:

    python3 tools/sim_position.py --plan --speed {a.speed:g}

## Change it

    python3 make_near_plans.py --unit nm                       nautical miles
    python3 make_near_plans.py --rings full                    full rings, for a flight not held to the box
    python3 make_near_plans.py --limit 3 --gates 0.5,1,1.5,2   a shorter flight
    python3 make_near_plans.py --tx-atten 20                   more drive
    python3 make_near_plans.py --confirm-first once            a confirm on the nearest gate

## Status

Checked in the planner's plan check and against the route. Not run on the radios.
"""
    with open(os.path.join(OUT, "README.md"), "w", encoding="utf-8") as f:
        f.write(readme)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
