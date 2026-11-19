"""
channelizer_tab.py: the Channelizer UI tab for the DPMG NiceGUI app.

Drop-in tab that loads a channel plan (up to 30 bands) over the captured band,
each with a demod type + per-type params, applies them to the ChannelizerWorker
(which talks to the GPU service), and shows live per-channel decoded output.

Integration (in nicegui_app.py):
    from channelizer_tab import build_channelizer_tab, CHANNELIZER
    # inside the tab panels, add a panel:
    with ui.tab_panel(channelizer_tab_ref):
        build_channelizer_tab(key="X40", HW=HW, SCHED=SCHED, section=section)

Requires in the app tree: channels.py, channelizer_client.py, channelizer_worker.py
and the app container started with --ipc=host (to reach the GPU service), plus
posix_ipc in requirements.txt.
"""

from __future__ import annotations

from nicegui import ui

from dpmg.dsp.channels import DEMOD_SCHEMA, SUPPORTED_DEMODS, ChannelConfig, ChannelSet
from dpmg.dsp.channelizer_worker import ChannelizerWorker, ChannelRuntime
from dpmg.dsp.decode_log import get_log

# one worker per device key
CHANNELIZER: dict[str, ChannelizerWorker] = {}

PI = "\u03c0"
_DEMOD_CHOICES = ["BPSK", "QPSK", "FSK", "4FSK", f"{PI}/4-QPSK", "DQPSK", "OQPSK", "GFSK"]


try:
    from dpmg.web.plan_ui import build_plan_panel  
except Exception:
    build_plan_panel = None

try:
    from dpmg.web.history_ui import build_history_panel
except Exception:
    build_history_panel = None

def build_channelizer_tab(key, HW, SCHED, section):
    status_holder = {"lbl": None}


    with section("GPU Channelizer Service"):
        with ui.row().classes("items-center gap-2"):
            svc_lbl = ui.label("service: unknown").classes("font-mono")
            ui.button("Start Worker", on_click=lambda: _start_worker()).props("color=positive")
            ui.button("Stop Worker", on_click=lambda: _stop_worker()).props("color=negative")
            def _reset_counts():
                wk = CHANNELIZER.get(key)
                if wk is None:
                    ui.notify("no worker running", type="warning"); return
                try:
                    wk.reset_counters()
                    ui.notify("scoreboard counters reset", type="positive")
                except Exception as e:
                    ui.notify(f"reset failed: {e}", type="negative")
            ui.button("Reset Counters", on_click=_reset_counts).props("outline")
        if status_holder.get("lbl") is None:
            status_holder["lbl"] = ui.label("").classes("font-mono text-sm")
        with ui.expansion("Diagnostics").classes("w-full"):
            _dump_name = ui.input("channel name", value="FSK-01")
            def _dump_bb():
                wk = CHANNELIZER.get(key)
                if wk is None:
                    ui.notify("no worker running", type="warning"); return
                try:
                    wk.dump_flagged_baseband(only_name=_dump_name.value)
                    ui.notify(f"dumped {_dump_name.value} -> /tmp/chan_bb.npy", type="positive")
                except Exception as e:
                    ui.notify(f"dump failed: {e}", type="negative")
            ui.button("Dump baseband", on_click=_dump_bb)
    # Blind survey is on the SDR tab (survey_ui.py). This tab is targeted-only:
    # loading a plan sets the worker to targeted mode; the scoreboard shows Det/Dec/Clean.

    # load a channel_plan .json as the worker watch-list (25 bands, <=5 decode)
    if build_plan_panel is not None:
        _pcc = 425_025_000.0
        try:
            if HW is not None and hasattr(HW, 'center_freq'):
                _pcc = float(getattr(HW, 'center_freq', _pcc)) or _pcc
        except Exception:
            pass
        try:
            build_plan_panel(ui, lambda: CHANNELIZER.get(key), section,
                             capture_center_hz=_pcc, default_dir="scenarios")
        except Exception as _e:
            pass

    # full RF timeline + CSV export
    if build_history_panel is not None:
        try:
            build_history_panel(ui, key, section)
        except Exception as _e:
            with section("Burst History"):
                ui.label(f"history panel error: {_e}")

    def _start_worker():
        w = CHANNELIZER.get(key)
        if w is None:
            w = ChannelizerWorker(frame_len=1048576, fs_out=250_000.0)
            CHANNELIZER[key] = w
        w.log_key = key   # write decodes to the log this tab reads
        w.start()
        sched = SCHED.get(key)
        if sched is not None:
            sched.frame_hook = w.feed      # tap live RX frames (non-lossy)
            sched._saved_rx_pace_s = getattr(sched, "rx_pace_s", 0.05)
            sched.rx_pace_s = 0.02  
            ui.notify("channelizer worker started (tapped to live RX)", type="positive")
        else:
            # no scheduler yet: fine for synthetic testing, which feeds the worker
            # directly. The live RX tap attaches automatically when streaming starts.
            ui.notify("worker started (no live RX yet -- OK for synthetic test)",
                      type="positive")

    def _stop_worker():
        sched = SCHED.get(key)
        if sched is not None:
            sched.frame_hook = None
            sched.rx_pace_s = getattr(sched, "_saved_rx_pace_s", 0.05)  
        w = CHANNELIZER.get(key)
        if w is not None:
            w.stop()
        ui.notify("channelizer worker stopped", type="warning")

    # live status refresh
    def _refresh_status():
        # timer can fire before the label exists
        # or after its client disconnected; bail out quietly.
        if not status_holder or status_holder.get("lbl") is None:
            return
        w = CHANNELIZER.get(key)
        if w is None:
            svc_lbl.set_text("service: worker not started")
            return
        st = w.status()
        svc_lbl.set_text("service: " + ("UP" if st["service_ok"] else "DOWN"))
        status_holder["lbl"].set_text(
            f"processed={st['processed']} dropped={st['dropped']} "
            f"channels={st['n_channels']}"
            + (f"  err: {st['last_error']}" if st['last_error'] else ""))
        # per-band decode state is shown in the Channel Plan table

    ui.timer(0.5, _refresh_status)
