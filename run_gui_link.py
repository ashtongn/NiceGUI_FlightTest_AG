"""Dev launcher: run the (patched) NiceGUI app on an alternate port.

`python -m dpmg.web.nicegui_app` hardcodes port 8080 in its module-level
ui.run guard; importing the module instead registers all @ui.page routes
without starting the server, so the port can be picked here. Used on the dev
box where another instance may already hold 8080.

    python3 run_gui_link.py [port] [discovery_port]   # default 8081, 47811
"""

import sys
from pathlib import Path

import os
# optional 2nd arg: ANNOUNCE listener port, so a dev GUI can run beside the
# deployed container (which holds 47811). Point a service at it with
# --discovery-port <n>, or use Connect by IP.
if len(sys.argv) > 2:
    os.environ["DPMG_DISCOVERY_PORT"] = sys.argv[2]
    os.environ.setdefault("DPMG_GUI_ID", "GCS-DEV")

from nicegui import ui

import dpmg.web.nicegui_app  # noqa: F401  (registers pages on import)

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8081
_icon = Path(__file__).resolve().parent / "mothball_fury.svg"
ui.run(host="0.0.0.0", port=port, title="MOTHBALL FURY",
       favicon=str(_icon) if _icon.exists() else None,
       reload=False, show=False, reconnect_timeout=30.0)
