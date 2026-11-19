"""
dsss_builder_tab.py: DSSS builder panel for the engineering view.

Lets an operator define LFSR spreading codes (polynomial as taps or hex, seed,
Fibonacci / Galois form, length, offset) or a whole code set (Gold family,
m-sequence phases, distinct polynomials), see the set's correlation table
(peak off-zero auto-correlation, pairwise peak cross-correlation, predicted
multi-user interference), assign codes to users with a carrier family, symbol
rate and message, preview the composite spectrum, build the scenario JSON (the
same custom-waveform JSON gen_scenario_json reads: "code_set" + "dsss" +
"users"), build the IQ on the radio and load it for TX, push the matching
decode plan, and watch the per-user decodes (live WATCH_UPDATE rows + an
offline replay check through the real decode path).

Mounted once from link_tab.build_remote_sdr (engineering view) next to the
Tools panel; the operator console does not show it.

    from dsss_builder_tab import build_dsss_builder_panel
    build_dsss_builder_panel(lambda: _host())
"""
from __future__ import annotations

import asyncio
import json
import os
import time

import numpy as np
from nicegui import ui

from dpmg.deploy_config import SCENARIO_DIR as _SCN_DIR
from dpmg.paths import REPO_DIR as _REPO_DIR

_REPO_SCN_JSON = os.path.join(_REPO_DIR, "scenarios", "json")

CARRIERS = ["bpsk", "qpsk", "oqpsk", "pi4qpsk"]
SET_KINDS = {"gold": "Gold family (preferred pair, offsets)",
             "mseq_phases": "m-sequence phases (one polynomial, distinct seeds)",
             "polys": "distinct primitive polynomials",
             "custom": "custom LFSRs (added one by one below)"}
MAX_INLINE_SPEC = 1150          # bytes of packed JSON that still fit one CMD datagram


def _parse_poly_text(txt: str):
    """'5,2' | '[5,2]' | '0x12' | 'x^5+x^2+1': what lfsr_codes.parse_poly takes."""
    t = str(txt or "").strip()
    if not t:
        raise ValueError("polynomial is empty")
    if t.startswith("[") and t.endswith("]"):
        return json.loads(t)
    return t


def _spec_from_state(st: dict) -> dict:
    """The scenario JSON the builder state describes (compact users form)."""
    d = st["dsss"]
    spec = {"name": st["name"], "description": st.get("description", ""),
            "sample_rate_hz": float(st["rate_hz"]), "center_freq_hz": float(st["center_hz"]),
            "loop_s": float(st["loop_s"]), "signals": []}
    if st["set_kind"] == "custom":
        spec["code_set"] = {"kind": "list", "codes": [dict(c) for c in st["custom_codes"]]}
    else:
        spec["code_set"] = dict(st["set_def"])
    spec["dsss"] = {"symbol_rate": float(d["symbol_rate"]), "samples_per_chip": int(d["spc"]),
                    "offset_hz": float(d["offset_hz"]), "carrier": d["carrier"],
                    "amplitude": float(d["amplitude"]), "stagger_chips": int(d["stagger_chips"]),
                    "spacing_hz": float(d["spacing_hz"])}
    spec["users"] = [dict(u) for u in st["users"]]
    return spec


def _state_from_spec(spec: dict, st: dict) -> None:
    """Load a scenario JSON (users form) back into the builder state."""
    st["name"] = str(spec.get("name", st["name"]))
    st["description"] = str(spec.get("description", ""))
    if spec.get("sample_rate_hz"):
        st["rate_hz"] = float(spec["sample_rate_hz"])
    if spec.get("center_freq_hz"):
        st["center_hz"] = float(spec["center_freq_hz"])
    st["loop_s"] = float(spec.get("loop_s", st["loop_s"]))
    cs = spec.get("code_set") or {}
    if cs.get("kind") == "list":
        st["set_kind"] = "custom"; st["custom_codes"] = [dict(c) for c in cs.get("codes", [])]
    elif cs:
        st["set_kind"] = str(cs.get("kind", "gold")); st["set_def"] = dict(cs)
    d = spec.get("dsss") or {}
    st["dsss"].update({"symbol_rate": float(d.get("symbol_rate", st["dsss"]["symbol_rate"])),
                       "spc": int(d.get("samples_per_chip", st["dsss"]["spc"])),
                       "offset_hz": float(d.get("offset_hz", st["dsss"]["offset_hz"])),
                       "carrier": str(d.get("carrier", st["dsss"]["carrier"])),
                       "amplitude": float(d.get("amplitude", st["dsss"]["amplitude"])),
                       "stagger_chips": int(d.get("stagger_chips", st["dsss"]["stagger_chips"])),
                       "spacing_hz": float(d.get("spacing_hz", st["dsss"]["spacing_hz"]))})
    st["users"] = [dict(u) for u in (spec.get("users") or [])]


def _papr_db(iq: np.ndarray) -> float:
    """Peak-to-average power ratio of one composite (same array for peak and mean)."""
    a2 = np.abs(np.asarray(iq)) ** 2
    return float(10 * np.log10(float(np.max(a2)) / (float(np.mean(a2)) + 1e-20) + 1e-20))


def _welch_psd(iq: np.ndarray, fs: float, nfft: int = 2048):
    x = np.asarray(iq, dtype=np.complex64)
    n = (len(x) // nfft) * nfft
    if n < nfft:
        nfft = max(64, 1 << int(np.log2(max(64, len(x)))))
        n = (len(x) // nfft) * nfft
    segs = x[:n].reshape(-1, nfft) * np.hanning(nfft)[None, :]
    P = np.mean(np.abs(np.fft.fftshift(np.fft.fft(segs, axis=1), axes=1)) ** 2, axis=0)
    f = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / fs))
    return f, 10 * np.log10(P / (fs * nfft) + 1e-20)


def build_dsss_builder_panel(get_host):
    from dpmg.scenario import gen_scenario_json as g
    from dpmg.scenario import dsss_builder_validate as V
    from dpmg.modulation import lfsr_codes as L
    from dpmg.modulation.spreading_codes import resolve_code, describe_code

    st = {"name": "dsss_builder", "description": "", "rate_hz": 1_920_000.0,
          "center_hz": 425_025_000.0, "loop_s": 0.25,
          "set_kind": "gold", "set_def": {"kind": "gold", "n": 5, "count": 4, "length": 31},
          "custom_codes": [],
          "dsss": {"symbol_rate": 2400, "spc": 2, "offset_hz": 300_000.0, "carrier": "bpsk",
                   "amplitude": 0.8, "stagger_chips": 7, "spacing_hz": 0.0},
          "users": [], "codes": [], "report": None,
          "built": None, "plan_users": {}, "replay": {}}

    with ui.expansion("DSSS builder: LFSR codes & multi-user overlay", icon="grid_on",
                      value=False).classes("w-full border rounded") as panel:
        ui.label("Define LFSR spreading codes or a code set, check the set's correlation table, "
                 "assign codes to users (carrier / rate / message), preview the composite spectrum, "
                 "build the scenario JSON + IQ, load it for TX and push the matching decode plan. "
                 "Writes the same custom-waveform JSON the playback panels load.") \
            .classes("text-xs text-gray-600")

        # 1. code set
        ui.label("1. Code set").classes("text-sm font-bold mt-1")
        with ui.row().classes("items-center gap-2 flex-wrap"):
            kind_sel = ui.select(SET_KINDS, value="gold", label="Set kind") \
                .props("dense outlined").classes("w-80")
            n_in = ui.number("degree n", value=5, format="%d", min=2, max=16) \
                .props("dense outlined").classes("w-24")
            count_in = ui.number("codes (N users)", value=4, format="%d", min=1, max=64) \
                .props("dense outlined").classes("w-32")
            len_in = ui.number("length (0 = full period)", value=0, format="%d", min=0) \
                .props("dense outlined").classes("w-44")
            form_sel = ui.select(["fibonacci", "galois"], value="fibonacci", label="Form") \
                .props("dense outlined").classes("w-32")
            poly_in = ui.input("polynomial (taps 5,2 | hex 0x12 | x^5+x^2+1)", value="5,2") \
                .props("dense outlined").classes("w-72")
            poly2_in = ui.input("2nd polynomial (gold pair, blank = table)", value="") \
                .props("dense outlined").classes("w-72")
            seed_in = ui.number("state / seed", value=1, format="%d", min=1) \
                .props("dense outlined").classes("w-28")
            with ui.element("div").classes("inline-block") as build_codes_w:
                build_codes_btn = ui.button("Build codes").props("color=primary dense")
        with ui.row().classes("items-center gap-2 flex-wrap"):
            ui.label("custom LFSR:").classes("text-xs text-gray-600")
            c_poly = ui.input("poly", value="5,2").props("dense outlined").classes("w-36")
            c_state = ui.number("state", value=1, format="%d", min=1).props("dense outlined").classes("w-24")
            c_form = ui.select(["fibonacci", "galois"], value="fibonacci", label="form") \
                .props("dense outlined").classes("w-32")
            c_len = ui.number("length", value=0, format="%d", min=0).props("dense outlined").classes("w-24")
            c_off = ui.number("offset", value=0, format="%d", min=0).props("dense outlined").classes("w-24")
            add_custom_btn = ui.button("Add custom LFSR").props("dense outline")
            clear_custom_btn = ui.button("Clear customs").props("dense outline")
        codes_tbl = ui.table(columns=[
            {"name": "i", "label": "#", "field": "i"},
            {"name": "code", "label": "code", "field": "code", "align": "left"},
            {"name": "len", "label": "L", "field": "len"},
            {"name": "bal", "label": "balance", "field": "bal"},
            {"name": "auto", "label": "auto peak (off-zero)", "field": "auto"},
            {"name": "mui", "label": "MUI SNR dB (equal power)", "field": "mui"},
        ], rows=[], row_key="i").classes("w-full").props("dense")
        corr_tbl = ui.table(columns=[], rows=[], row_key="r").classes("w-full").props("dense")
        corr_lbl = ui.label("no codes built yet").classes("font-mono text-xs text-gray-700")

        # 2. users
        ui.label("2. Users").classes("text-sm font-bold mt-2")
        with ui.row().classes("items-center gap-2 flex-wrap"):
            d = st["dsss"]
            sr_in = ui.number("symbol rate (Bd)", value=d["symbol_rate"], format="%d", min=1) \
                .props("dense outlined").classes("w-36")
            spc_in = ui.number("samples / chip", value=d["spc"], format="%d", min=1, max=16) \
                .props("dense outlined").classes("w-32")
            off_in = ui.number("offset (Hz)", value=d["offset_hz"], format="%.0f") \
                .props("dense outlined").classes("w-36")
            spacing_in = ui.number("spacing (Hz, 0 = overlay)", value=0, format="%.0f") \
                .props("dense outlined").classes("w-48")
            car_sel = ui.select(CARRIERS, value="bpsk", label="default carrier") \
                .props("dense outlined").classes("w-36")
            amp_in = ui.number("amplitude", value=0.8, format="%.2f", min=0.01, max=1.0) \
                .props("dense outlined").classes("w-28")
            stag_in = ui.number("stagger (chips)", value=7, format="%d", min=0) \
                .props("dense outlined").classes("w-32")
            chip_lbl = ui.label("").classes("font-mono text-xs text-gray-600")
        with ui.row().classes("items-center gap-2 flex-wrap"):
            u_id = ui.input("user id", value="U01").props("dense outlined").classes("w-24")
            u_code = ui.number("code #", value=0, format="%d", min=0).props("dense outlined").classes("w-24")
            u_car = ui.select(CARRIERS, value="bpsk", label="carrier").props("dense outlined").classes("w-32")
            u_msg = ui.input("message", value="U01 PING").props("dense outlined").classes("w-56")
            u_amp = ui.number("ampl", value=0.8, format="%.2f", min=0.01, max=1.0) \
                .props("dense outlined").classes("w-24")
            add_user_btn = ui.button("Add user").props("color=primary dense")
            with ui.element("div").classes("inline-block") as auto_w:
                auto_btn = ui.button("Auto-fill N users").props("dense outline")
            del_user_btn = ui.button("Remove last").props("dense outline")
            clear_users_btn = ui.button("Clear users").props("dense outline")
        users_tbl = ui.table(columns=[
            {"name": "id", "label": "user", "field": "id"},
            {"name": "code", "label": "code #", "field": "code"},
            {"name": "carrier", "label": "carrier", "field": "carrier"},
            {"name": "message", "label": "message", "field": "message", "align": "left"},
            {"name": "amplitude", "label": "ampl", "field": "amplitude"},
            {"name": "codelbl", "label": "code", "field": "codelbl", "align": "left"},
        ], rows=[], row_key="id").classes("w-full").props("dense")
        users_lbl = ui.label("no users").classes("font-mono text-xs text-gray-700")

        # input checks (dsss_builder_validate.validate_builder)
        chk_box = ui.column().classes("w-full gap-0 border rounded px-2 py-1 mt-2")

        # 3. build
        ui.label("3. Build").classes("text-sm font-bold mt-2")
        with ui.row().classes("items-center gap-2 flex-wrap"):
            name_in = ui.input("scenario name", value="dsss_builder").props("dense outlined").classes("w-44")
            rate_in = ui.number("sample rate (Hz)", value=1_920_000, format="%.0f") \
                .props("dense outlined").classes("w-40")
            center_in = ui.number("center (Hz)", value=425_025_000, format="%.0f") \
                .props("dense outlined").classes("w-44")
            loop_in = ui.number("loop (s)", value=0.25, format="%.2f", min=0.05) \
                .props("dense outlined").classes("w-24")
            peak_in = ui.number("peak (FS)", value=0.45, format="%.2f", min=0.05, max=1.0,
                                step=0.05).props("dense outlined").classes("w-24").tooltip(
                "peak of the built waveform as a fraction of DAC full scale; "
                "the users keep their levels relative to each other")
            from_radio_btn = ui.button("rate/center from radio").props("dense outline")
            with ui.element("div").classes("inline-block") as preview_w:
                preview_btn = ui.button("Preview spectrum").props("color=secondary dense")
            with ui.element("div").classes("inline-block") as json_w:
                json_btn = ui.button("Scenario JSON").props("dense outline")
            with ui.element("div").classes("inline-block") as save_w:
                save_btn = ui.button("Save JSON on this PC").props("dense outline")
            with ui.element("div").classes("inline-block") as build_w:
                build_btn = ui.button("Build on radio + load TX + push plan").props("color=primary dense")
            preset_sel = ui.select({"": "preset...", "dsss_4user_bpsk": "4-user BPSK overlay",
                                    "dsss_mixed_carriers": "mixed BPSK/QPSK/OQPSK/pi4 users",
                                    "dsss_16user": "16-user overlay"}, value="", label="Preset") \
                .props("dense outlined").classes("w-64")
            up = ui.upload(label="Load scenario JSON", auto_upload=True).props("accept=.json dense") \
                .classes("w-56")
        build_lbl = ui.label("nothing built").classes("font-mono text-xs text-gray-700")
        spec_chart = ui.echart({
            "animation": False, "tooltip": {"trigger": "axis"},
            "grid": {"left": 60, "right": 20, "top": 24, "bottom": 40},
            "xAxis": {"type": "value", "name": "offset (kHz)", "scale": True,
                      "nameLocation": "middle", "nameGap": 24},
            "yAxis": {"type": "value", "name": "dB/Hz", "scale": True,
                      "nameLocation": "middle", "nameGap": 44},
            "series": [{"type": "line", "showSymbol": False, "lineStyle": {"width": 1}, "data": []}],
        }).classes("w-full h-56")
        spec_lbl = ui.label("no preview yet").classes("font-mono text-xs text-gray-600")
        json_area = ui.textarea("scenario JSON (users form)", value="").props("outlined dense readonly") \
            .classes("w-full font-mono text-xs")
        json_area.set_visibility(False)

        # 4. decodes
        ui.label("4. Per-user decodes").classes("text-sm font-bold mt-2")
        with ui.row().classes("items-center gap-2 flex-wrap"):
            with ui.element("div").classes("inline-block") as replay_w:
                replay_btn = ui.button("Replay decode (offline check)").props("color=secondary dense")
            replay_snr = ui.number("replay SNR dB", value=25, format="%.0f").props("dense outlined").classes("w-32")
            replay_cfo = ui.number("replay CFO Hz", value=250, format="%.0f").props("dense outlined").classes("w-32")
            dec_lbl = ui.label("push a plan to see live per-user rows").classes("font-mono text-xs text-gray-700")
        dec_tbl = ui.table(columns=[
            {"name": "user", "label": "user", "field": "user"},
            {"name": "carrier", "label": "carrier", "field": "carrier"},
            {"name": "code", "label": "code", "field": "code", "align": "left"},
            {"name": "want", "label": "expected", "field": "want", "align": "left"},
            {"name": "state", "label": "live", "field": "state"},
            {"name": "msg", "label": "last msg", "field": "msg", "align": "left"},
            {"name": "dn", "label": "decodes", "field": "dn"},
            {"name": "match", "label": "match", "field": "match"},
            {"name": "replay", "label": "offline replay", "field": "replay", "align": "left"},
        ], rows=[], row_key="user").classes("w-full").props("dense")

    def _note(msg, type_="info"):
        ui.notify(msg, type=type_, multi_line=True, close_button=(type_ == "negative"))

    # Input checks before anything is built or sent
    _fields = {"set_kind": kind_sel, "degree": n_in, "count": count_in, "length": len_in,
               "form": form_sel, "poly": poly_in, "poly2": poly2_in, "seed": seed_in,
               "symbol_rate": sr_in, "samples_per_chip": spc_in, "offset_hz": off_in,
               "spacing_hz": spacing_in, "carrier": car_sel, "amplitude": amp_in,
               "stagger": stag_in, "sample_rate": rate_in, "center": center_in,
               "loop_s": loop_in, "name": name_in}
    _gated = [(build_codes_w, build_codes_btn, "codes"), (auto_w, auto_btn, "codes"),
              (preview_w, preview_btn, "all"), (json_w, json_btn, "all"), (save_w, save_btn, "all"),
              (build_w, build_btn, "all"), (replay_w, replay_btn, "all")]
    chk = {"issues": [], "shown": None, "dirty": True, "off": {}}

    def _poly_or_raw(txt):
        try:
            return _parse_poly_text(txt)
        except Exception:                                    # noqa: BLE001
            return str(txt or "")

    def _collect_spec() -> dict:
        """The users-form spec as the inputs stand now (raw values: an empty or
        zero field is checked, not replaced by a default)."""
        kind = kind_sel.value
        if kind == "custom":
            cs = {"kind": "list", "codes": [dict(c) for c in st["custom_codes"]]}
        else:
            cs = {"kind": kind, "n": n_in.value, "count": count_in.value, "form": form_sel.value,
                  "state": seed_in.value}
            if len_in.value not in (None, 0):
                cs["length"] = len_in.value
            if kind == "gold" and str(poly2_in.value or "").strip():
                cs["polys"] = [_poly_or_raw(poly_in.value), _poly_or_raw(poly2_in.value)]
            elif kind == "mseq_phases":
                cs["poly"] = _poly_or_raw(poly_in.value)
        return {"name": str(name_in.value or "").strip(), "sample_rate_hz": rate_in.value,
                "center_freq_hz": center_in.value, "loop_s": loop_in.value,
                "peak_fs": float(peak_in.value or 0.45), "code_set": cs,
                "dsss": {"symbol_rate": sr_in.value, "samples_per_chip": spc_in.value,
                         "offset_hz": off_in.value, "carrier": car_sel.value,
                         "amplitude": amp_in.value, "stagger_chips": stag_in.value,
                         "spacing_hz": spacing_in.value},
                "users": [dict(u) for u in st["users"]], "signals": []}

    def _radio_rate():
        host = get_host()
        cfg = getattr(host, "config", None) if host else None
        try:
            return float(cfg.get("sample_rate")) if cfg and cfg.get("sample_rate") else None
        except (TypeError, ValueError):
            return None

    def _revalidate():
        try:
            issues = V.validate_builder(_collect_spec(), radio_rate=_radio_rate(), with_info=True)
        except Exception as e:                               # noqa: BLE001  (never break the panel)
            issues = [(V.ERROR, "spec", f"input check failed: {e}")]
        chk["issues"] = issues
        chk["dirty"] = False
        errs = V.errors(issues)
        # red outline + a short pointer under each input that has an error (the
        # full text is in the Input checks box; it does not fit under a w-24 field)
        for f, el in _fields.items():
            n_f = sum(1 for lv, ff, m in errs if ff == f)
            msg = (("see Input checks below" if n_f == 1 else f"{n_f} errors: see Input checks below")
                   if n_f else None)
            if hasattr(el, "error") and el.error != msg:
                el.error = msg
                el.update()
        # gate the build buttons
        all_err = bool(errs)
        set_err = bool(V.code_set_errors(issues))
        for w, b, scope in _gated:
            off = set_err if scope == "codes" else all_err
            if chk["off"].get(b.id) != off:
                chk["off"][b.id] = off
                b.set_enabled(not off)
                if off:
                    b.classes(add="pointer-events-none"); w.classes(add="cursor-not-allowed")
                else:
                    b.classes(remove="pointer-events-none"); w.classes(remove="cursor-not-allowed")
        # one summary box: errors red, warnings amber, passes grey
        key = tuple(issues)
        if key == chk["shown"]:
            return issues
        chk["shown"] = key
        warns = V.warnings(issues)
        infos = [i for i in issues if i[0] == V.INFO]
        chk_box.clear()
        with chk_box:
            if errs:
                ui.label(f"Input checks: {len(errs)} error(s). Build / Preview / Build on radio are "
                         f"blocked until fixed" + (f"; {len(warns)} warning(s)" if warns else "")) \
                    .classes("text-xs font-bold text-red-700")
            elif warns:
                ui.label(f"Input checks: no errors, {len(warns)} warning(s) (allowed)") \
                    .classes("text-xs font-bold text-amber-700")
            else:
                ui.label("Input checks: all OK").classes("text-xs font-bold text-green-700")
            for _lv, _f, m in errs:
                ui.label(f"ERROR: {m}").classes("text-xs text-red-700")
            for _lv, _f, m in warns:
                ui.label(f"WARNING: {m}").classes("text-xs text-amber-700")
            for _lv, _f, m in infos:
                ui.label(f"OK: {m}").classes("text-xs text-gray-600")
        return issues

    def _blocked(scope="all") -> bool:
        """Re-check now; True (and a notify of the first error) when the action
        must not run."""
        issues = _revalidate()
        errs = V.code_set_errors(issues) if scope == "codes" else V.errors(issues)
        if errs:
            _note(f"fix the inputs first: {errs[0][2]}"
                  + (f" (+{len(errs) - 1} more in Input checks)" if len(errs) > 1 else ""), "negative")
            return True
        return False

    def _disabled_click(b, scope):
        # a disabled button passes its click to the wrapper (pointer-events-none)
        if chk["off"].get(b.id):
            _blocked(scope)

    def _mark(*_a):
        chk["dirty"] = True

    def _tick():
        if chk["dirty"]:
            _revalidate()

    def _sync_set_def():
        kind = kind_sel.value
        st["set_kind"] = kind
        n = int(n_in.value or 5); cnt = int(count_in.value or 1)
        ln = int(len_in.value or 0)
        sd = {"kind": kind, "n": n, "count": cnt, "form": form_sel.value,
              "state": int(seed_in.value or 1)}
        if ln > 0:
            sd["length"] = ln
        if kind == "gold" and poly2_in.value.strip():
            sd["polys"] = [_parse_poly_text(poly_in.value), _parse_poly_text(poly2_in.value)]
        elif kind == "mseq_phases":
            sd["poly"] = _parse_poly_text(poly_in.value)
        elif kind == "polys":
            sd.pop("count", None) if False else None
        st["set_def"] = sd

    def _build_codes():
        if _blocked("codes"):
            return
        try:
            _sync_set_def()
            if st["set_kind"] == "custom":
                specs = [dict(c) for c in st["custom_codes"]]
                if not specs:
                    raise ValueError("add at least one custom LFSR first")
            else:
                specs = L.make_code_set(st["set_def"])
            codes = [resolve_code(c) for c in specs]
            st["codes"] = specs
            st["codes_from"] = _set_key()
            same = len({len(c) for c in codes}) == 1
            rep = L.set_report(codes, [f"C{i}" for i in range(len(codes))]) if same else None
            st["report"] = rep
            rows = []
            for i, (c, sp) in enumerate(zip(codes, specs)):
                rows.append({"i": i, "code": describe_code(sp, len(c)), "len": len(c),
                             "bal": L.balance(c)["sum_pm"], "auto": L.peak_offzero(c),
                             "mui": (f"{rep['mui_snr_db'][i]:.1f}" if rep and len(codes) > 1 else "-")})
            codes_tbl.rows = rows; codes_tbl.update()
            if rep and len(codes) > 1:
                cols = [{"name": "r", "label": "peak |xcorr|", "field": "r"}] + \
                       [{"name": f"c{j}", "label": f"C{j}", "field": f"c{j}"} for j in range(len(codes))]
                crows = []
                for i in range(len(codes)):
                    row = {"r": f"C{i}"}
                    for j in range(len(codes)):
                        row[f"c{j}"] = "-" if i == j else rep["xcorr_peak"][i][j]
                    crows.append(row)
                corr_tbl.columns = cols; corr_tbl.rows = crows; corr_tbl.update()
                wp = rep["worst_pair"]
                corr_lbl.set_text(f"{len(codes)} codes, L={rep['length']}: worst pair C{wp[0]}/C{wp[1]} "
                                  f"peak {wp[2]:.0f} ({rep['worst_db']:.1f} dB of the peak); predicted "
                                  f"equal-power MUI SNR >= {min(rep['mui_snr_db']):.1f} dB "
                                  f"(Gold bound t={L.gold_t(int(np.log2(rep['length'] + 1)))} for a "
                                  f"preferred pair)" if (rep['length'] + 1) & rep['length'] == 0 else
                                  f"{len(codes)} codes, L={rep['length']}: worst pair C{wp[0]}/C{wp[1]} "
                                  f"peak {wp[2]:.0f} ({rep['worst_db']:.1f} dB); predicted MUI SNR "
                                  f">= {min(rep['mui_snr_db']):.1f} dB")
            else:
                corr_tbl.columns = []; corr_tbl.rows = []; corr_tbl.update()
                corr_lbl.set_text(f"{len(codes)} code(s)" + ("" if same else " of DIFFERENT lengths "
                                  "(no cross-correlation table; users must share a length to overlay)"))
            _refresh_users()
            _mark()
            _note(f"built {len(codes)} code(s)", "positive")
        except Exception as e:                                   # noqa: BLE001
            corr_lbl.set_text(f"code set error: {e}")
            _note(f"code set: {e}", "negative")

    def _add_custom():
        try:
            spec = {"family": "lfsr", "poly": _parse_poly_text(c_poly.value), "state": int(c_state.value or 1),
                    "form": c_form.value}
            if int(c_len.value or 0) > 0:
                spec["length"] = int(c_len.value)
            if int(c_off.value or 0) > 0:
                spec["offset"] = int(c_off.value)
            bad = V.errors(V.validate_code_spec(spec, "custom LFSR"))
            if bad:
                _note(bad[0][2], "negative"); return
            resolve_code(spec)                                   # validate now
            st["custom_codes"].append(spec)
            kind_sel.value = "custom"
            _build_codes()
        except Exception as e:                                   # noqa: BLE001
            _note(f"custom LFSR: {e}", "negative")

    def _clear_custom():
        st["custom_codes"] = []; st["codes"] = []; codes_tbl.rows = []; codes_tbl.update()
        corr_tbl.rows = []; corr_tbl.update(); corr_lbl.set_text("customs cleared")
        _mark()

    def _sync_dsss():
        st["dsss"].update({"symbol_rate": float(sr_in.value or 2400), "spc": int(spc_in.value or 2),
                           "offset_hz": float(off_in.value or 0), "spacing_hz": float(spacing_in.value or 0),
                           "carrier": car_sel.value, "amplitude": float(amp_in.value or 0.8),
                           "stagger_chips": int(stag_in.value or 0)})
        st["name"] = str(name_in.value or "dsss_builder").strip()
        st["rate_hz"] = float(rate_in.value or 1_920_000); st["center_hz"] = float(center_in.value or 0)
        st["loop_s"] = float(loop_in.value or 0.25)

    def _refresh_users():
        _sync_dsss()
        rows = []
        for u in st["users"]:
            k = int(u.get("code", 0))
            lbl = describe_code(st["codes"][k]) if k < len(st["codes"]) else "(no such code, build codes first)"
            rows.append({"id": u["id"], "code": k, "carrier": u.get("carrier", st["dsss"]["carrier"]),
                         "message": u.get("message", ""), "amplitude": u.get("amplitude", st["dsss"]["amplitude"]),
                         "codelbl": lbl})
        users_tbl.rows = rows; users_tbl.update()
        L0 = len(resolve_code(st["codes"][0])) if st["codes"] else 0
        chip = st["dsss"]["symbol_rate"] * L0
        chip_lbl.set_text(f"chip rate {chip/1e3:.1f} kchip/s, main lobe +/-{chip/1e3:.1f} kHz, "
                          f"{chip * st['dsss']['spc']/1e3:.1f} kSps native" if L0 else "")
        users_lbl.set_text(f"{len(rows)} user(s)" + (" at one centre (CDMA overlay)"
                           if rows and st["dsss"]["spacing_hz"] == 0 else ""))
        _mark()

    def _add_user():
        uid = str(u_id.value or f"U{len(st['users']) + 1:02d}").strip()
        if any(u["id"] == uid for u in st["users"]):
            _note(f"user {uid} exists", "warning"); return
        st["users"].append({"id": uid, "code": int(u_code.value or 0), "carrier": u_car.value,
                            "message": str(u_msg.value or f"{uid} PING"),
                            "amplitude": float(u_amp.value or 0.8)})
        nxt = len(st["users"]) + 1
        u_id.value = f"U{nxt:02d}"; u_code.value = len(st["users"]); u_msg.value = f"U{nxt:02d} PING"
        _refresh_users()

    def _auto_fill():
        n = int(count_in.value or 4)
        if not st["codes"]:
            _build_codes()
        n = min(n, len(st["codes"])) if st["codes"] else n
        st["users"] = [{"id": f"U{k + 1:02d}", "code": k, "carrier": car_sel.value,
                        "message": f"U{k + 1:02d} PING", "amplitude": float(amp_in.value or 0.8)}
                       for k in range(n)]
        _refresh_users()

    def _del_user():
        if st["users"]:
            st["users"].pop()
        _refresh_users()

    def _clear_users():
        st["users"] = []; _refresh_users()

    def _set_key():
        return json.dumps([st["set_kind"], st["set_def"], st["custom_codes"]], sort_keys=True, default=str)

    def _spec():
        _sync_dsss()
        _sync_set_def()
        # the code set follows the inputs (a degree / count changed after the
        # last Build codes is rebuilt here, the same set the checks looked at)
        if not st["codes"] or st.get("codes_from") != _set_key():
            _build_codes()
        if not st["users"]:
            raise ValueError("no users (Add user / Auto-fill)")
        return _spec_from_state(st)

    def _preview():
        if _blocked():
            return
        try:
            spec = _spec()
            v = g.validate_spec(spec, spec["sample_rate_hz"])
            if not v["ok"]:
                raise ValueError("; ".join(v["errors"]))
            ex = g.expand_users(spec)
            fs = float(spec["sample_rate_hz"])
            nloop = int(fs * float(spec["loop_s"]))
            mix = np.zeros(nloop, dtype=np.complex64)
            for sig in ex["signals"]:
                iq, _e = g.build_signal(sig, fs)
                reps = int(np.ceil(nloop / len(iq)))
                mix += np.tile(iq, reps)[:nloop]
            papr = _papr_db(mix)               # before the 0.9 peak scaling
            peak = float(np.max(np.abs(mix)) or 1.0)
            mix = (mix * (0.9 / peak)).astype(np.complex64)
            f, p = _welch_psd(mix, fs)
            spec_chart.options["series"][0]["data"] = [[float(ff) / 1e3, float(pp)] for ff, pp in zip(f, p)]
            spec_chart.update()
            rep = g.overlay_report(spec)
            spec_lbl.set_text(f"{len(ex['signals'])} signal(s), {nloop} samp @ {fs/1e6:g} MSps, PAPR "
                              f"{papr:.1f} dB" + (f", predicted MUI SNR >= {rep['expected_mui_snr_db']:.1f} dB"
                                                  if rep and rep.get("expected_mui_snr_db") is not None else ""))
            st["preview"] = (mix, ex)
            _note("preview built", "positive")
        except Exception as e:                                   # noqa: BLE001
            spec_lbl.set_text(f"preview error: {e}")
            _note(f"preview: {e}", "negative")

    def _show_json():
        if _blocked():
            return
        try:
            spec = _spec()
            json_area.value = json.dumps(spec, indent=1)
            json_area.set_visibility(True)
        except Exception as e:                                   # noqa: BLE001
            _note(f"JSON: {e}", "negative")

    def _save_json():
        if _blocked():
            return
        try:
            spec = _spec()
            # deploy_config SCENARIO_DIR (/app/scenarios in the container); on a
            # dev box without it, the repo's own scenarios/json
            jdir = os.path.join(_SCN_DIR, "json")
            try:
                os.makedirs(jdir, exist_ok=True)
                if not os.access(jdir, os.W_OK):
                    raise OSError("not writable")
            except OSError:
                jdir = _REPO_SCN_JSON
                os.makedirs(jdir, exist_ok=True)
            fn = os.path.join(jdir, f"{spec['name']}.json")
            json.dump(spec, open(fn, "w"), indent=1)
            build_lbl.set_text(f"saved {fn}")
            _note(f"saved {fn}", "positive")
        except Exception as e:                                   # noqa: BLE001
            _note(f"save: {e}", "negative")

    def _load_preset():
        name = preset_sel.value
        if not name:
            return
        spec = next((s for s in g.dsss_preset_specs() if s["name"] == name), None)
        if spec is None:
            return
        _apply_spec(spec)          # the dropdown keeps showing which preset is loaded

    def _apply_spec(spec):
        _state_from_spec(spec, st)
        sd = st["set_def"]
        kind_sel.value = st["set_kind"]
        n_in.value = int(sd.get("n", sd.get("degree", n_in.value)) or n_in.value)
        count_in.value = int(sd.get("count", len(st["users"]) or count_in.value) or 1)
        len_in.value = int(sd.get("length", 0) or 0)
        form_sel.value = sd.get("form", "fibonacci")
        if sd.get("polys"):
            poly_in.value = json.dumps(sd["polys"][0]); poly2_in.value = json.dumps(sd["polys"][1])
        elif sd.get("poly"):
            poly_in.value = json.dumps(sd["poly"])
        seed_in.value = int(sd.get("state", 1) or 1)
        # snapshot first: every widget assignment fires on_value_change, then
        # _refresh_users and _sync_dsss, which reads the widgets back into st
        # (the stale ones would overwrite the values not yet applied)
        d = dict(st["dsss"])
        name, rate, center, loop = st["name"], st["rate_hz"], st["center_hz"], st["loop_s"]
        sr_in.value = d["symbol_rate"]; spc_in.value = d["spc"]; off_in.value = d["offset_hz"]
        spacing_in.value = d["spacing_hz"]; car_sel.value = d["carrier"]; amp_in.value = d["amplitude"]
        stag_in.value = d["stagger_chips"]
        name_in.value = name; rate_in.value = rate; center_in.value = center
        loop_in.value = loop
        st["dsss"].update(d); st["name"], st["rate_hz"], st["center_hz"], st["loop_s"] = name, rate, center, loop
        _build_codes()
        _note(f"loaded {st['name']}: {len(st['users'])} users", "positive")

    async def _on_upload(e):
        try:
            spec = json.loads(e.content.read().decode("utf-8"))
            if not spec.get("users"):
                raise ValueError("this JSON has no 'users' block (plain scenario: load it in the playback panel)")
            _apply_spec(spec)
        except Exception as ex:                                  # noqa: BLE001
            _note(f"load: {ex}", "negative")
    up.on_upload(_on_upload)

    def _from_radio():
        host = get_host()
        cfg = getattr(host, "config", None) if host else None
        if not cfg:
            _note("no radio config yet (select a host / wait for get_config)", "warning"); return
        if cfg.get("sample_rate"):
            rate_in.value = float(cfg["sample_rate"])
        if cfg.get("center_freq"):
            center_in.value = float(cfg["center_freq"])
        build_lbl.set_text(f"rate {rate_in.value/1e6:g} MSps, center {center_in.value/1e6:.4f} MHz from the radio")

    async def _build_on_radio():
        host = get_host()
        if host is None:
            _note("no host selected", type_="warning"); return
        if _blocked():
            return
        try:
            spec = _spec()
        except Exception as e:                                   # noqa: BLE001
            _note(f"spec: {e}", "negative"); return
        build_lbl.set_text("building on the radio...")
        try:
            packed = json.dumps({"n": "gen_scenario", "p": {"spec": spec, "load_tx": True}},
                                separators=(",", ":")).encode()
            if len(packed) <= MAX_INLINE_SPEC:
                base = {"spec": spec}
            else:
                # too big for one command datagram: push the JSON as a file and
                # build from the inbox (service: gen_scenario spec_inbox)
                name = await host.send_file(f"{spec['name']}.json", json.dumps(spec).encode(), timeout=60.0)
                base = {"spec_inbox": name}
            ack = await host.cmd("gen_scenario", dict(base, load_tx=True, match_radio_rate=True), timeout=90.0)
            r = ack.result or {}
            if ack.status != 0:
                build_lbl.set_text(f"build failed: {r.get('err')}")
                _note(f"build failed: {r.get('err')}", "negative"); return
            st["built"] = r
            msg = (f"built {r.get('signals')} signal(s) @ {float(r.get('sample_rate') or 0)/1e6:g} MSps "
                   f"-> {r.get('iq_path')}" + (", loaded as TX" if r.get("loaded_tx") else ""))
            if r.get("plan_path"):
                pa = await host.cmd("set_plan", {"plan_path": r["plan_path"]}, timeout=15.0)
                msg += (f"; plan pushed ({(pa.result or {}).get('entries')} entries"
                        f"{', applied live' if (pa.result or {}).get('applied') else ', applies at START'})"
                        if pa.status == 0 else f"; plan push failed: {(pa.result or {}).get('err')}")
            for w in (r.get("validation") or {}).get("warnings") or []:
                _note(w, "warning")
            build_lbl.set_text(msg)
            ex = g.expand_users(spec)
            st["plan_users"] = {s["user"]: s for s in ex["signals"] if s.get("user")}
            st["replay"] = {}
            _refresh_decodes()
            _note("scenario built on the radio; press START TX (operator console / TX panel) to key it",
                  "positive")
        except Exception as e:                                   # noqa: BLE001
            build_lbl.set_text(f"error: {e}")
            _note(f"build: {e}", "negative")

    def _refresh_decodes():
        host = get_host()
        bands = {b.get("n"): b for b in (getattr(host, "watch_bands", []) or [])} if host else {}
        rows = []
        users = st["plan_users"] or {}
        if not users and bands:
            dec_lbl.set_text("live plan rows (not from this builder)")
        for uid, sig in users.items():
            b = bands.get(uid, {})
            want = sig.get("message", "")
            msg = b.get("msg", "")
            state = "decoding" if b.get("d") else ("present" if b.get("p") else "-")
            rep = st["replay"].get(uid, "")
            rows.append({"user": uid, "carrier": sig.get("dsss_modem", ""),
                         "code": describe_code(sig.get("code_spec")), "want": want,
                         "state": state, "msg": msg, "dn": b.get("dn", 0),
                         "match": ("yes" if msg and msg == want else ("no" if msg else "")),
                         "replay": rep})
        dec_tbl.rows = rows; dec_tbl.update()
        if users:
            live = sum(1 for r in rows if r["match"] == "yes")
            age = time.monotonic() - getattr(host, "_watch_t", 0.0) if host else 1e9
            dec_lbl.set_text(f"{len(rows)} user(s): {live} live match" +
                             (" (live)" if age < 3.0 else " (no live rows: plan not applied / RX stopped)"))

    async def _replay():
        if _blocked():
            return
        try:
            spec = _spec()
        except Exception as e:                                   # noqa: BLE001
            _note(f"spec: {e}", "negative"); return
        dec_lbl.set_text("offline replay running (build -> channelize -> decode per user)...")
        snr = float(replay_snr.value or 25); cfo = float(replay_cfo.value or 0)

        def _work():
            import tempfile, shutil
            tmp = tempfile.mkdtemp(prefix="dsss_gui_")
            from dpmg.scenario import dsss_replay as T              # product module, no tests/ needed
            try:
                iq, plan, raw = T.build(spec, tmp)
                rng = np.random.default_rng(int(time.time()) & 0xFFFF)
                rows, fs_out = T.decode_users(iq, plan, rng, reps=1, cfo_hz=cfo, snr_db=snr)
                return rows, raw.get("dsss_overlay") or {}
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        try:
            rows, rep = await asyncio.get_running_loop().run_in_executor(None, _work)
            ex = g.expand_users(spec)
            st["plan_users"] = {s["user"]: s for s in ex["signals"] if s.get("user")}
            st["replay"] = {}
            for r in rows:
                sin = [v for v in r["sinr_db"] if v is not None]
                st["replay"][r["user"]] = (f"{'ok' if r['ok'] else 'MISS'}, bit err {min(r['bit_err'])}"
                                           + (f", SINR {np.median(sin):.1f} dB" if sin else ""))
            ok = sum(1 for r in rows if r["ok"])
            _refresh_decodes()
            dec_lbl.set_text(f"offline replay: {ok}/{len(rows)} users decoded at {snr:.0f} dB, CFO {cfo:.0f} Hz"
                             + (f"; predicted MUI SNR >= {rep['expected_mui_snr_db']:.1f} dB"
                                if rep.get("expected_mui_snr_db") is not None else ""))
            _note(f"offline replay {ok}/{len(rows)}", "positive" if ok == len(rows) else "warning")
        except Exception as e:                                   # noqa: BLE001
            dec_lbl.set_text(f"replay error: {e}")
            _note(f"replay: {e}", "negative")

    build_codes_btn.on_click(_build_codes)
    add_custom_btn.on_click(_add_custom)
    clear_custom_btn.on_click(_clear_custom)
    add_user_btn.on_click(_add_user)
    auto_btn.on_click(_auto_fill)
    del_user_btn.on_click(_del_user)
    clear_users_btn.on_click(_clear_users)
    preview_btn.on_click(_preview)
    json_btn.on_click(_show_json)
    save_btn.on_click(_save_json)
    build_btn.on_click(_build_on_radio)
    from_radio_btn.on_click(_from_radio)
    preset_sel.on_value_change(lambda e: _load_preset())
    replay_btn.on_click(_replay)
    for el in (sr_in, spc_in, off_in, spacing_in, car_sel, amp_in, stag_in):
        el.on_value_change(lambda e: _refresh_users())
    # Every input re-checks (debounced by the 0.3 s tick)
    for el in _fields.values():
        el.on_value_change(_mark)
    for w, b, scope in _gated:
        w.on("click", lambda e, b=b, scope=scope: _disabled_click(b, scope))
    ui.timer(0.3, _tick)
    ui.timer(1.0, _refresh_decodes)
    return panel
