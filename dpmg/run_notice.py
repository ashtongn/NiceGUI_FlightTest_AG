"""
run_notice.py: what a mission is waiting on, for a banner on the GUI pages.

A radio takes up to about 50 s to initialize and nothing moves on screen meanwhile. The mission
runner says here when that starts and ends, and every page shows the same plain message.

    run_notice.starting(True)            before the timeline's zero
    run_notice.begin("G40-0002")         an init is going
    run_notice.end("G40-0002")
    run_notice.text()                    the message, "" when there is nothing to say
"""
from __future__ import annotations

import time

_busy = {}          # radio id: monotonic start of its init
_starting = False


def starting(on: bool) -> None:
    global _starting
    _starting = bool(on)
    if not on:
        _busy.clear()


def begin(rid) -> None:
    _busy[rid] = time.monotonic()


def end(rid) -> None:
    _busy.pop(rid, None)


def text() -> str:
    now = time.monotonic()
    who = ", ".join(f"{r} {now - t:.0f} s" for r, t in sorted(_busy.items(), key=str))
    if _starting:
        return ("Mission starting: radios are coming up, about 50 s each"
                + (f" ({who})" if who else "") + ". The plan's steps have not started. Please wait.")
    if who:
        return f"Radio re-initializing, about 50 s ({who}). Its steps wait until this finishes."
    return ""


def add_banner() -> None:
    """A banner fixed at the top of the current page, shown only while there is a message."""
    from nicegui import ui
    lbl = ui.label("").style(
        "position:fixed;top:56px;left:50%;transform:translateX(-50%);z-index:6000;max-width:90vw;"
        "background:#b45309;color:white;font-weight:700;font-size:15px;padding:8px 16px;"
        "border-radius:6px;box-shadow:0 2px 8px rgba(0,0,0,.35)")
    lbl.set_visibility(False)

    def _update():
        t = text()
        if t != lbl.text:
            lbl.set_text(t)
        lbl.set_visibility(bool(t))
    ui.timer(0.5, _update)
