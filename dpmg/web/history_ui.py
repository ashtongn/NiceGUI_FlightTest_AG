"""
history_ui.py: NiceGUI burst-history panel: full RF timeline (DETECTED/DECODED/LOST) with live
refresh, filter, counts and CSV export.
"""
from __future__ import annotations
import time

def build_history_panel(ui, key, section):
    from dpmg.dsp.burst_history import get_history

    with section("Burst History (RF timeline)"):
        counts_lbl = ui.label("detected 0 | decoded 0 | lost 0").classes("font-mono text-sm")

        with ui.row().classes("items-center gap-3"):
            kind_filter = ui.toggle({"ALL": "All", "DECODED": "Decoded",
                                     "DETECTED": "Detected", "LOST": "Lost"}, value="ALL")
            ui.button("Export CSV", on_click=lambda: _export()).props("color=primary size=sm")
            ui.button("Clear", on_click=lambda: (get_history(key).clear(), _refresh())).props("color=negative size=sm outline")

        table = ui.table(columns=[
            {"name": "time", "label": "Time", "field": "time", "align": "left"},
            {"name": "kind", "label": "Event", "field": "kind", "align": "left"},
            {"name": "offset", "label": "Offset kHz", "field": "offset", "align": "right"},
            {"name": "freq", "label": "Freq MHz", "field": "freq", "align": "right"},
            {"name": "power", "label": "Power dB", "field": "power", "align": "right"},
            {"name": "message", "label": "Message", "field": "message", "align": "left"},
            {"name": "dur", "label": "Dur s", "field": "dur", "align": "right"},
        ], rows=[], row_key="rid").classes("w-full").props("dense")

        def _export():
            csv_text = get_history(key).to_csv()
            fname = "burst_history_%s_%s.csv" % (key, time.strftime("%Y%m%d_%H%M%S"))
            import base64
            b64 = base64.b64encode(csv_text.encode()).decode()
            ui.download("data:text/csv;base64,%s" % b64, fname)
            ui.notify("exported %s" % fname, type="positive")

        def _refresh():
            h = get_history(key)
            c = h.counts()
            counts_lbl.set_text("detected %d | decoded %d | lost %d" %
                                (c.get("DETECTED", 0), c.get("DECODED", 0), c.get("LOST", 0)))
            evs = h.recent(200)
            filt = kind_filter.value
            rows = []
            for i, e in enumerate(reversed(evs)):
                if filt != "ALL" and e.kind != filt:
                    continue
                rows.append({
                    "rid": "%f_%s_%.0f" % (e.ts, e.kind, e.offset_khz),
                    "time": time.strftime("%H:%M:%S", time.localtime(e.ts)) +
                            (".%03d" % int((e.ts % 1) * 1000)),
                    "kind": e.kind,
                    "offset": "%+.0f" % e.offset_khz,
                    "freq": "%.4f" % (e.freq_hz / 1e6),
                    "power": ("%.1f" % e.power_db) if e.kind != "LOST" else "",
                    "message": e.message,
                    "dur": ("%.2f" % e.duration_s) if e.kind == "LOST" else "",
                })
            sig = (c.get("DETECTED",0), c.get("DECODED",0), c.get("LOST",0), filt)
            if sig == getattr(_refresh, "_last_sig", None):
                return
            _refresh._last_sig = sig
            table.rows = rows
            table.update()
        ui.timer(1.0, _refresh)

    return
