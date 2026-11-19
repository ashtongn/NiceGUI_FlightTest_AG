"""Let a ui.timer on a deleted page end quietly.

A page that never gets a socket (fetched with curl by launch_gui.sh or
push_update.sh, or a load the browser gave up on) is deleted by NiceGUI a minute
later. Its timers wake then, and the context lookup raises "The parent slot of
the element has been deleted", once per timer. With no context the timer's own
loop sees it is deleted and ends. Imported once by nicegui_app.
"""

from contextlib import nullcontext

from nicegui.elements.timer import Timer


def _timer_context(self):
    try:
        return self.parent_slot or nullcontext()
    except RuntimeError:
        return nullcontext()


Timer._get_context = _timer_context
