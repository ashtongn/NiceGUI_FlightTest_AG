"""A ui.timer on a deleted page ends without raising (dpmg/web/timer_patch.py).

    python3 -m pytest -q tests/test_timer_patch.py
"""

import os
import sys
from contextlib import nullcontext

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from nicegui.elements.timer import Timer

from dpmg.web import timer_patch


class _Deleted:
    @property
    def parent_slot(self):
        raise RuntimeError("The parent slot of the element has been deleted.")


class _Live:
    parent_slot = "slot"


def test_timer_uses_the_patched_context():
    assert Timer._get_context is timer_patch._timer_context


def test_deleted_page_gives_an_empty_context():
    assert isinstance(timer_patch._timer_context(_Deleted()), nullcontext)


def test_live_page_keeps_its_slot():
    assert timer_patch._timer_context(_Live()) == "slot"
