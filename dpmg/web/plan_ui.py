"""
plan_ui.py: NiceGUI panel: load a channel_plan .json, push it to the worker as the
watch-list, and show one row per declared band with live present/power/decode state +
a per-band decode checkbox (the <=5 that go to the decode pool). Mirrors the
history_ui module pattern: build_plan_panel(ui, get_worker, section, ...).

Wire in channelizer_tab.build_channelizer_tab, next to build_gated_panel:

    try:
        from plan_ui import build_plan_panel
    except Exception:
        build_plan_panel = None
    ...
    if build_plan_panel is not None:
        build_plan_panel(ui, lambda: CHANNELIZER.get(key), section,
                         capture_center_hz=_cc, default_dir="scenarios")
"""
from __future__ import annotations
import os, glob


def build_plan_panel(ui, get_worker, section, capture_center_hz: float = 425_025_000.0,
                     default_dir: str = "scenarios"):
    state = {"plan": None, "path": None}

    with section("Channel Plan (watch-list)"):
        with ui.row().classes("items-center gap-2 w-full"):
            # discover scenario files so the operator isn't typing paths
            choices = []
            for d in (default_dir, "output", "."):
                choices += sorted(glob.glob(os.path.join(d, "*.json")))
            choices = sorted(set(choices))
            path_in = ui.select(choices or ["<no .json found>"],
                                label="Scenario .json",
                                value=(choices[0] if choices else None)) \
                        .props("dense outlined").classes("w-96")
            load_btn = ui.button("Load Plan").props("color=primary")
            lint_lbl = ui.label("").classes("text-xs text-gray-500")

        status_lbl = ui.label("no plan loaded").classes("font-mono text-sm")

        # selftest: compose the loaded plan and inject it into this worker via
        # loopback_source, so the watch-list sees its own declared signal (one box, no radio).
        with ui.row().classes("items-center gap-2 w-full"):
            selftest_sw = ui.switch("Self-Test: inject this plan (loopback)", value=False)
            selftest_lbl = ui.label("").classes("font-mono text-xs text-gray-500")

        _st_guard = {"resetting": False}

        def _reset_switch():
            # flip the switch off without re-running the body (avoids clobbering the
            # real error message with "stopped")
            _st_guard["resetting"] = True
            selftest_sw.value = False
            selftest_sw.update()
            _st_guard["resetting"] = False

        def _toggle_selftest(ev):
            if _st_guard["resetting"]:
                return
            from dpmg.dsp.loopback_source import get_source
            src = get_source()
            if not ev.value:
                src.stop(); selftest_lbl.set_text("self-test injection stopped"); return
            w = get_worker()
            if w is None:
                selftest_lbl.set_text("bail: worker not started (click Start Worker)")
                _reset_switch(); return
            if state["plan"] is None:
                selftest_lbl.set_text("bail: no plan in state -- click Load Plan first")
                _reset_switch(); return
            try:
                from dpmg.scenario.plan_compose import compose_plan
                selftest_lbl.set_text("composing plan ...")
                wb, fs, center, rep = compose_plan(state["plan"])
            except Exception as e:
                import traceback; traceback.print_exc()
                selftest_lbl.set_text(f"compose error: {e}")
                _reset_switch(); return
            if not len(wb) or not rep["rendered"]:
                why = "; ".join(f"{n}:{r}" for n, r in rep.get("skipped", [])[:3]) or "empty"
                selftest_lbl.set_text(f"nothing renderable ({why})")
                _reset_switch(); return
            try:
                from dpmg.scenario.plan_compose import spectrum_report
                print("[SELFTEST]", spectrum_report(wb, fs, center, state["plan"]), flush=True)
            except Exception:
                pass
            try:
                src.set_buffer(wb, fs, center); src.loop = True; src.start(w)
            except Exception as e:
                selftest_lbl.set_text(f"inject error: {e}"); _reset_switch(); return
            skipped = f", {len(rep['skipped'])} skipped" if rep["skipped"] else ""
            selftest_lbl.set_text(f"injecting {len(rep['rendered'])} bands @ {fs/1e6:g} MSps{skipped} -- watch the table")

        selftest_sw.on_value_change(_toggle_selftest)

        table = ui.table(
            columns=[
                {"name": "name", "label": "Band", "field": "name", "align": "left"},
                {"name": "freq", "label": "MHz", "field": "freq"},
                {"name": "mod", "label": "Mod", "field": "mod"},
                {"name": "present", "label": "Present", "field": "present"},
                {"name": "power", "label": "SNR dB", "field": "power"},
                {"name": "decoded", "label": "Decoded", "field": "decoded"},
                {"name": "message", "label": "Message", "field": "message"},
                {"name": "det_n", "label": "Det", "field": "det_n", "align": "right"},
                {"name": "dec_n", "label": "Dec", "field": "dec_n", "align": "right"},
                {"name": "clean_n", "label": "Clean", "field": "clean_n", "align": "right"},
                {"name": "decode", "label": "Decode?", "field": "decode"},
            ],
            rows=[], row_key="name").classes("w-full").props("dense")
        # color whole row by decode state (green=decoding, amber=present)
        table.add_slot("body", r'''
            <q-tr :props="props" :class="props.row._rowclass">
              <q-td v-for="col in props.cols" :key="col.name" :props="props">
                {{ col.value }}
              </q-td>
            </q-tr>
        ''')

        # a decode toggle per row via a slot would need Quasar templating; a compact
        # checkbox grid below the table, kept in sync with the plan, is simpler.
        with ui.expansion("Flag channels for full decode (<=5)", icon="tune").classes("w-full"):
            checks_box = ui.row().classes("flex-wrap gap-2 w-full")

        def _do_load():
            w = get_worker()
            if w is None:
                status_lbl.set_text("start the worker first"); return
            path = path_in.value
            if not path or not os.path.exists(path):
                status_lbl.set_text(f"file not found: {path}"); return
            try:
                from dpmg.scenario.channel_plan import ChannelPlan, lint
                from dpmg.scenario.plan_adapter import load_plan
                plan = load_plan(path)
            except Exception as e:
                status_lbl.set_text(f"load error: {e}"); return
            # lint + surface conflicts before pushing to the worker
            rep = lint(plan)
            lint_lbl.set_text(
                f"{rep['n_entries']} entries | usable {rep['usable_bw_hz']/1e6:g} MHz | "
                f"{len(rep['errors'])} errors, {len(rep['warnings'])} warnings"
                + ("  -- " + rep['errors'][0] if rep['errors'] else ""))
            state["plan"] = plan; state["path"] = path
            try:
                w.set_plan(plan, tol_hz=150e3)
            except Exception as e:
                status_lbl.set_text(f"set_plan error: {e}"); return
            status_lbl.set_text(f"loaded {os.path.basename(path)} -> {len(plan.entries)} bands watching")
            _build_checks()

        def _build_checks():
            checks_box.clear()
            plan = state["plan"]
            if plan is None:
                return
            with checks_box:
                for e in plan.entries:
                    if e.role != "known":
                        continue
                    def _mk(name):
                        def _cb(ev):
                            w = get_worker()
                            if w is not None:
                                w.set_band_decode(name, bool(ev.value))
                        return _cb
                    ui.checkbox(e.name, value=bool(e.decode), on_change=_mk(e.name)) \
                        .props("dense").classes("text-xs")

        load_btn.on_click(_do_load)

        def _refresh():
            w = get_worker()
            if w is None or state["plan"] is None:
                return
            try:
                st = w.band_status()
            except Exception:
                return
            rows = []
            for r in st:
                rows.append({
                    "name": r["name"],
                    "freq": f"{r['f_center_hz']/1e6:.4f}",
                    "mod": r["modulation"],
                    "present": "YES" if r["present"] else "-",
                    "power": (f"{r['power_db']:.1f}" if r["power_db"] is not None else "-"),
                    "decoded": ("DECODED" if r["decoded"] else "-"),
                    "message": r["message"] or "",
                    "det_n": r.get("detected_n", 0),
                    "dec_n": r.get("decoded_n", 0),
                    "clean_n": r.get("clean_n", 0),
                    "decode": ("on" if r["decode"] else "off"),
                    # whole-row tint: green if decoding, amber if present-not-decoded
                    "_rowclass": ("bg-green-2" if r["decoded"]
                                  else "bg-amber-1" if r["present"] else ""),
                })
            table.rows = rows
            table.update()
            npres = sum(1 for r in st if r["present"])
            ndec = sum(1 for r in st if r["decoded"])
            nflag = sum(1 for r in st if r["decode"])
            status_lbl.set_text(f"{os.path.basename(state['path'] or '')}: "
                                f"{npres} present, {ndec} decoding, {nflag} flagged")

        ui.timer(1.0, _refresh)
