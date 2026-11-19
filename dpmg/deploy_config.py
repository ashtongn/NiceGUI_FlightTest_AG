"""
deploy_config.py: one place for the DPMG ground-station deployment addresses.

Every host/port here can be overridden with the matching environment variable, so
the committed defaults stay generic and nothing site-specific is baked into the
tools. Radio login credentials are not stored here: keep them in your SSH agent
/ ~/.ssh/config, or copy .env.example to .env (which is git-ignored).

    from deploy_config import G40_HOST, X40_HOST, CMD_PORT

Defaults below match the current bench setup; change them (or set the env vars) for
a different network. The G40 address has no default, since there is more than one G40.
"""
import os

# radio service hosts (the Jetson on each Matchstiq running dpmg_service)
G40_HOST = os.environ.get("DPMG_G40_HOST", "")   # no default: there is more than one G40
X40_HOST = os.environ.get("DPMG_X40_HOST", "192.168.0.26")

# the GUI/control host (x86) the radios unicast their ANNOUNCE to (see README:
# the x86 firewall drops broadcast discovery, so radios announce to this address)
GUI_HOST = os.environ.get("DPMG_GUI_HOST", "192.168.0.25")

# UDP link ports (service and GUI)
CMD_PORT = int(os.environ.get("DPMG_CMD_PORT", "47810"))
DISCOVERY_PORT = int(os.environ.get("DPMG_DISCOVERY_PORT", "47811"))

# radio login user
RADIO_USER = os.environ.get("DPMG_RADIO_USER", "sidekiq")

# on-radio code + libsidekiq mount paths (bind-mounted into the service container)
RADIO_CODE_DIR = os.environ.get("DPMG_RADIO_CODE_DIR",
                                "/home/sidekiq/work/NiceGui_test")
EPIQ_LIB_DIR = os.environ.get("DPMG_EPIQ_LIB_DIR", "/usr/lib/epiq")


def radio_host(kind: str) -> str:
    """Host address for a radio kind ('g40'|'x40')."""
    return {"g40": G40_HOST, "x40": X40_HOST}.get(kind, G40_HOST)


# Scenario / IQ storage directories used by the GUI's save dialogs.
# Linux/Docker keeps the /app paths (the container's own tree, with
# scenarios_ram meant to be a tmpfs mount). On Windows there is no /app and no
# tmpfs, so the defaults fall under this repo's output/ folder. Override either
# with the env var. Set DPMG_SCENARIO_RAM_DIR to an empty string to drop the
# "RAM (tmpfs)" option everywhere.
import sys as _sys

from dpmg.paths import REPO_DIR as _REPO_DIR  # noqa: E402
_IS_WINDOWS = _sys.platform == "win32"

SCENARIO_DIR = os.environ.get(
    "DPMG_SCENARIO_DIR",
    os.path.join(_REPO_DIR, "output", "scenarios") if _IS_WINDOWS
    else "/app/scenarios")
SCENARIO_RAM_DIR = os.environ.get(
    "DPMG_SCENARIO_RAM_DIR",
    os.path.join(_REPO_DIR, "output", "scenarios_ram") if _IS_WINDOWS
    else "/app/scenarios_ram")

SCENARIO_DISK_LABEL = "Disk"
SCENARIO_RAM_LABEL = "RAM (tmpfs)"


def scenario_ram_available() -> bool:
    """Whether the RAM storage option should be offered. On POSIX it always is
    (the directory is created on demand and is a tmpfs only if the
    deployment mounted one). On Windows there is no tmpfs, so the
    option appears only when the configured folder already exists (e.g. on a
    RAM drive the operator created). An empty DPMG_SCENARIO_RAM_DIR hides it."""
    if not SCENARIO_RAM_DIR:
        return False
    if not _IS_WINDOWS:
        return True
    return os.path.isdir(SCENARIO_RAM_DIR)


def scenario_storage_targets() -> dict:
    """Ordered {label: directory} for the storage selectors ("Disk" first)."""
    targets = {SCENARIO_DISK_LABEL: SCENARIO_DIR}
    if scenario_ram_available():
        targets[SCENARIO_RAM_LABEL] = SCENARIO_RAM_DIR
    return targets


def is_scenario_ram_dir(path) -> bool:
    """True when `path` is (inside) the RAM storage directory."""
    p = str(path)
    if SCENARIO_RAM_DIR and (p == SCENARIO_RAM_DIR
                             or p.startswith(SCENARIO_RAM_DIR.rstrip("/\\") + os.sep)):
        return True
    return "scenarios_ram" in p   # older substring check, kept as a fallback
