"""
survey_ui.py: NiceGUI blind-survey panel for the SDR tab.

Blind (auto-discover) mode: the energy detector scans the full captured band and
reports whatever crosses threshold. Being on this panel is blind mode: activating
it sets the worker to blind; there is no per-panel mode toggle (targeted mode lives
on the Watch List tab, driven by loading a channel plan).

Usage (inside the SDR tab build, where `ui` is in scope):
    from survey_ui import build_survey_panel
    build_survey_panel(ui, lambda: CHANNELIZER.get(key))

Reads worker.gated_results (dict) and calls worker.set_mode / clear_results.
"""
from __future__ import annotations
import time

def build_survey_panel(ui, get_worker):
    with ui.card().classes("w-full"):
        with ui.row().classes("items-center gap-4 w-full"):
            ui.label("Blind Survey (auto-discover)").classes("text-lg font-bold")
            def _activate():
                w = get_worker()
                if w is None:
                    ui.notify("Start Worker first", type="warning"); return
                w.set_mode("blind")
                ui.notify("blind survey active", type="positive")
            ui.button("Activate Blind Mode", on_click=_activate).props("color=primary")
            ui.button("Clear Results",
                      on_click=lambda: (get_worker() and get_worker().clear_results())) \
                .props("color=secondary")

        ui.separator()
        ui.label("Discovered Emitters").classes("text-md font-bold")
        table = ui.table(columns=[
            {"name": "offset", "label": "Offset (kHz)", "field": "offset", "align": "right"},
            {"name": "freq", "label": "Freq (MHz)", "field": "freq", "align": "right"},
            {"name": "power", "label": "Power (dB)", "field": "power", "align": "right"},
            {"name": "message", "label": "Message", "field": "message", "align": "left"},
            {"name": "count", "label": "Count", "field": "count", "align": "right"},
            {"name": "age", "label": "Last seen (s)", "field": "age", "align": "right"},
        ], rows=[], row_key="freq").classes("w-full")

        def _refresh():
            w = get_worker()
            if w is None:
                return
            res = getattr(w, "gated_results", {}) or {}
            now = time.time()
            rows = []
            for fk, r in sorted(res.items()):
                rows.append({
                    "offset": f"{r.get('offset_khz', 0):+.0f}",
                    "freq": f"{r.get('freq_hz', 0)/1e6:.3f}",
                    "power": f"{r.get('power_db', 0):.1f}",
                    "message": str(r.get("message", "")),
                    "count": r.get("count", 0),
                    "age": f"{now - r.get('last_ts', now):.1f}",
                })
            table.rows = rows
            table.update()

        ui.timer(1.0, _refresh)
    return
