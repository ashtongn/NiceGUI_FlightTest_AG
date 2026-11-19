"""The port dropdown's tooltip handler goes on once, at page build (port_pick_ui).

    python3 -m pytest -q tests/test_port_pick_hook.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from dpmg.web import port_pick_ui as ppu


class _Client:
    def __init__(self):
        self.js = []

    def run_javascript(self, code):
        self.js.append(code)


class _Select:
    id = 7

    def __init__(self):
        self.client = _Client()
        self.listeners = []

    def on(self, event, js_handler=None):
        self.listeners.append(event)


def test_hooked_at_build_a_later_map_adds_no_listener():
    sel = _Select()
    ppu.hook_tips(sel)
    assert sel.listeners == ["popup-show"]
    ppu._set_tips(sel, {"TRx1A": "splitter"})
    assert sel.listeners == ["popup-show"]
    assert "splitter" in sel.client.js[0]


def test_hook_is_only_added_once():
    sel = _Select()
    ppu.hook_tips(sel)
    ppu.hook_tips(sel)
    assert sel.listeners == ["popup-show"]


def test_no_hook_no_tips_does_nothing():
    sel = _Select()
    ppu._set_tips(sel, {})
    assert sel.listeners == [] and sel.client.js == []
