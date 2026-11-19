"""
The bitpipe link panel (NiceGUI): what is flowing, how well, and a chat.

build_link_panel(ctl, chat, file) draws one panel for one container, given
its control face and its chat and file application faces. Everything comes
from the container's status over the control face once a second; the modem's
own numbers (carrier offset, eye, underruns) arrive there too, pushed by the
modem over the bit face. Chat and files go over the application faces like
any other client of the container.

Shown: link state, the waveform, payload rates each way, frames sent (data,
idle, retransmitted) and received, BER before FEC, frame error rate on the
receive side, round trip (from the acknowledgements and from a ping), a
history chart, the chat, a file send with a received files list.
"""
from __future__ import annotations

import hashlib
import os
import time
from collections import deque
from typing import Optional

from nicegui import ui

from . import streams as st
from .ctl_client import CtlClient, StreamClient

INBOX = os.path.join(os.path.expanduser("~"), ".dpmg", "bitpipe", "inbox")
HISTORY = 120


def _fmt_rate(bps: Optional[float]) -> str:
    if bps is None:
        return "-"
    if bps >= 1000:
        return f"{bps / 1000:.2f} kbit/s"
    return f"{bps:.0f} bit/s"


def _fmt_sci(x: Optional[float]) -> str:
    if x is None:
        return "-"
    if x == 0:
        return "0"
    return f"{x:.1e}"


def _fmt_s(x: Optional[float]) -> str:
    return "-" if x is None else (f"{x * 1000:.0f} ms" if x < 2 else f"{x:.1f} s")


def link_state(s: Optional[dict]) -> tuple:
    """(label, colour) for the badge."""
    if s is None:
        return "NO CONTAINER", "grey"
    bf = s.get("bit_face", {})
    if not bf.get("connected"):
        return "NO MODEM", "orange"
    if s.get("link_down"):
        return "LINK DOWN", "red"
    if s.get("locked") and s.get("peer_seen"):
        return "LINKED", "green"
    return "SEARCHING", "amber"


class LinkPanel:
    def __init__(self, ctl, chat=None, file=None, title: str = "Bitpipe link", inbox: str = INBOX):
        self.ctl = CtlClient(ctl)
        self.chat = StreamClient(chat, "chat") if chat else None
        self.file = StreamClient(file, "file") if file else None
        self.title = title
        self.inbox = inbox
        self.receiver = st.FileReceiver()
        self.prev = None
        self.hist = {k: deque(maxlen=HISTORY) for k in ("t", "ber", "fer", "rtt", "rx", "tx")}
        self.chat_rx_buf = b""
        self.files_seen = 0
        self.last_status: Optional[dict] = None

    # layout
    def build(self):
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center w-full"):
                ui.label(self.title).classes("text-lg font-medium")
                self.badge = ui.badge("NO CONTAINER", color="grey").classes("text-sm")
                self.where = ui.label("").classes("text-xs text-gray-500")
                ui.space()
                ui.button("Ping", on_click=self._ping).props("dense flat")
            with ui.row().classes("w-full gap-4 items-start"):
                self.k_wave = self._kv_card("Waveform", ["modulation", "symbol rate", "carrier offset", "eye", "underruns"])
                self.k_flow = self._kv_card("Flow", ["payload out", "payload in", "frames sent", "frames heard", "queued"])
                self.k_qual = self._kv_card("Quality", ["BER before FEC", "frame error rate", "retransmit share", "round trip", "ping"])
                self.k_link = self._kv_card("Link", ["bit face", "lock", "peer", "unacked", "crypto"])
            self.chart = ui.echart({
                "animation": False, "grid": {"left": 50, "right": 50, "top": 30, "bottom": 25},
                "legend": {"data": ["BER", "FER", "round trip s"], "top": 0},
                "tooltip": {"trigger": "axis"},
                "xAxis": {"type": "category", "data": []},
                "yAxis": [{"type": "log", "min": 1e-5, "max": 1, "name": "rate"},
                          {"type": "value", "name": "s", "min": 0}],
                "series": [{"name": "BER", "type": "line", "data": [], "showSymbol": False},
                           {"name": "FER", "type": "line", "data": [], "showSymbol": False},
                           {"name": "round trip s", "type": "line", "yAxisIndex": 1, "data": [], "showSymbol": False}],
            }).classes("w-full h-48")
            with ui.row().classes("w-full gap-4 items-start"):
                with ui.column().classes("grow min-w-[300px]"):
                    ui.label("Chat").classes("font-medium")
                    self.chat_log = ui.log(max_lines=400).classes("w-full h-48")
                    with ui.row().classes("w-full items-center no-wrap"):
                        self.chat_in = ui.input(placeholder="message").classes("grow").on(
                            "keydown.enter", self._send_chat)
                        ui.button("Send", on_click=self._send_chat).props("dense")
                with ui.column().classes("grow min-w-[260px] max-w-[420px]"):
                    ui.label("Files").classes("font-medium")
                    ui.upload(label="send a file", auto_upload=True, on_upload=self._send_file).classes("w-full")
                    self.file_rx_label = ui.label("nothing received").classes("text-xs")
                    self.file_list = ui.column().classes("w-full gap-0")
        ui.timer(1.0, self._tick)
        # the stream clients read from the container's application faces: a
        # page that is gone must let go, or it keeps swallowing chat and files
        # that the container would otherwise hold for the next client
        ui.context.client.on_delete(self.close)
        return self

    def close(self) -> None:
        for c in (self.chat, self.file):
            if c is not None:
                c.close()
        self.ctl.close()

    def _kv_card(self, title, keys):
        vals = {}
        with ui.card().classes("min-w-[220px]"):
            ui.label(title).classes("text-sm font-medium")
            with ui.grid(columns=2).classes("gap-x-3 gap-y-0 text-xs"):
                for k in keys:
                    ui.label(k).classes("text-gray-500")
                    vals[k] = ui.label("-")
        return vals

    # actions
    def _ping(self):
        if not self.ctl.ping():
            ui.notify("no container on the control face", type="warning")

    def _send_chat(self, *_):
        text = (self.chat_in.value or "").strip()
        if not text:
            return
        if self.chat is None or not self.chat.send((text + "\n").encode("utf-8")):
            ui.notify("chat stream not connected", type="warning")
            return
        self.chat_log.push(f"{time.strftime('%H:%M:%S')} me: {text}")
        self.chat_in.value = ""

    async def _send_file(self, e):
        data = await e.file.read()
        if self.file is None or not self.file.send(st.file_record(e.file.name, data)):
            ui.notify("file stream not connected", type="warning")
            return
        ui.notify(f"queued {e.file.name} ({len(data)} B)")
        self.chat_log.push(f"{time.strftime('%H:%M:%S')} sent file {e.file.name} ({len(data)} B)")

    # refresh
    def _tick(self):
        s = self.ctl.status()
        self.last_status = s
        label, colour = link_state(s)
        self.badge.text = label
        self.badge.props(f"color={colour}")
        self._pull_chat()
        self._pull_files()
        if s is None:
            self.where.text = f"control {self.ctl.ep.describe()}: {self.ctl.last_error}"
            return
        self.where.text = f"bit face {s.get('bit_face', {}).get('endpoint', '')}"
        now = time.monotonic()
        rate_in = rate_out = None
        sent = s["bytes_in"] - s.get("pending_bytes", 0)              # application bytes that left the queue
        if self.prev is not None:
            dt = max(1e-3, now - self.prev[0])
            rate_out = 8.0 * (sent - self.prev[1]) / dt
            rate_in = 8.0 * (s["bytes_out"] - self.prev[2]) / dt      # delivered from the peer, in order
        self.prev = (now, sent, s["bytes_out"])
        q = s.get("quality", {})
        m = s.get("modem", {}) or {}
        wv = m.get("waveform", {})
        dm = m.get("demod", {})
        ms = m.get("stats", {})
        stale = m and (time.time() - float(m.get("received_at", 0))) > 5
        self.k_wave["modulation"].text = (wv.get("modulation", "-") + (" (stale)" if stale else "")) if m else "no modem status"
        self.k_wave["symbol rate"].text = f"{wv['symbol_rate']:.0f} Bd" if wv.get("symbol_rate") else "-"
        self.k_wave["carrier offset"].text = f"{dm['cfo_hz']:.0f} Hz" if "cfo_hz" in dm and wv.get("modulation") in ("BPSK", "QPSK") else ("n/a (FSK)" if m else "-")
        self.k_wave["eye"].text = f"{dm['eye']:.2f}" if "eye" in dm else "-"
        self.k_wave["underruns"].text = str(ms.get("underruns", "-"))
        self.k_flow["payload out"].text = _fmt_rate(rate_out)
        self.k_flow["payload in"].text = _fmt_rate(rate_in)
        self.k_flow["frames sent"].text = f"{s['frames_tx']} ({s['data_tx']} data, {s['idle_tx']} idle, {s['tx']['retx']} again)"
        self.k_flow["frames heard"].text = f"{s['frames_rx']} ({s['deframer']['frames_bad']} bad)"
        self.k_flow["queued"].text = f"{s.get('pending_bytes', 0)} B"
        self.k_qual["BER before FEC"].text = _fmt_sci(q.get("ber"))
        self.k_qual["frame error rate"].text = _fmt_sci(q.get("fer")) + " (30 s)"
        self.k_qual["retransmit share"].text = f"{100 * q.get('retx_ratio', 0):.1f} %"
        self.k_qual["round trip"].text = _fmt_s(q.get("rtt_s"))
        self.k_qual["ping"].text = _fmt_s(q.get("ping_rtt_last_s"))
        bf = s.get("bit_face", {})
        self.k_link["bit face"].text = "connected" if bf.get("connected") else "waiting for a modem"
        self.k_link["lock"].text = "locked" if s.get("locked") else "searching"
        self.k_link["peer"].text = "heard" if s.get("peer_seen") else "not yet"
        self.k_link["unacked"].text = str(s.get("unacked", 0))
        cr = s.get("crypto", {})
        self.k_link["crypto"].text = cr.get("crypto", "-") + (f" ({cr.get('key_provider')})" if cr.get("key_provider") else "")
        h = self.hist
        h["t"].append(time.strftime("%H:%M:%S"))
        h["ber"].append(max(1e-5, q["ber"]) if q.get("ber") is not None else None)
        h["fer"].append(max(1e-5, q["fer"]) if q.get("fer") is not None else None)
        h["rtt"].append(q.get("rtt_s"))
        o = self.chart.options
        o["xAxis"]["data"] = list(h["t"])
        o["series"][0]["data"] = list(h["ber"])
        o["series"][1]["data"] = list(h["fer"])
        o["series"][2]["data"] = list(h["rtt"])
        self.chart.update()

    def _pull_chat(self):
        if self.chat is None:
            return
        data = self.chat_rx_buf + self.chat.take()
        *lines, rest = data.split(b"\n")
        self.chat_rx_buf = rest
        for ln in lines:
            self.chat_log.push(f"{time.strftime('%H:%M:%S')} peer: {ln.decode('utf-8', 'replace')}")

    def _pull_files(self):
        if self.file is None:
            return
        for rf in self.receiver.feed(self.file.take()):
            path = ""
            if rf.ok:
                os.makedirs(self.inbox, exist_ok=True)
                path = os.path.join(self.inbox, os.path.basename(rf.name) or "file.bin")
                with open(path, "wb") as f:
                    f.write(rf.data)
            with self.file_list:
                ui.label(f"{rf.name}  {rf.size} B  {'sha256 ok' if rf.ok else 'sha256 MISMATCH'}"
                         + (f"  saved {path}" if path else "")).classes("text-xs")
            self.chat_log.push(f"{time.strftime('%H:%M:%S')} received file {rf.name} ({rf.size} B, "
                               f"{'ok' if rf.ok else 'bad'})")
        p = self.receiver.progress()
        if p is not None:
            self.file_rx_label.text = f"receiving {p[0]}: {p[1]} of {p[2]} B"
        elif self.receiver.received:
            self.file_rx_label.text = f"{len(self.receiver.received)} received"


def build_link_panel(ctl, chat=None, file=None, title: str = "Bitpipe link", inbox: str = INBOX) -> LinkPanel:
    return LinkPanel(ctl, chat, file, title, inbox).build()


def panel_for_config(cfg, title: Optional[str] = None, inbox: str = INBOX) -> LinkPanel:
    """A panel for a container described by a BitpipeConfig (same machine)."""
    return build_link_panel(cfg.endpoint("ctl").describe(), cfg.endpoint("app", st.STREAM_CHAT).describe(),
                            cfg.endpoint("app", st.STREAM_FILE).describe(), title or f"Bitpipe {cfg.name}", inbox)
