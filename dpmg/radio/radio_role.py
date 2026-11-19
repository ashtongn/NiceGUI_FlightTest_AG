"""
radio_role.py: what each radio is acting as (receiver, jammer, IQ playback,
transponder, bitpipe modem, dCFE node, or anything for engineering).

One table shared by the service (which refuses commands the role does not
use), the GUI (which shows only the panels the role uses) and the mission
planner (which flags steps a radio's role cannot run). A role never loosens a
TX limit; it only takes things away.

"any" is the default and refuses nothing, so a radio with no role set has no
limits.
"""
from __future__ import annotations

import json
import os
import time

DEFAULT = "any"

# Commands that key the transmitter or load something to transmit.
_TX_CMDS = frozenset({
    "set_tx_waveform", "start_tx", "transceive", "transponder",
    "bitpipe_start", "replay_recording", "ground_checkout", "payload_key",
})

# Commands every role accepts: stops, status and the role/FPGA controls.
# A stop must always work.
ALWAYS = frozenset({
    "stop_tx", "stop_rx", "close", "get_config", "caps", "sensors",
    "wedge_status", "gps_status", "secure_status", "frontend_status",
    "payload_status", "atten_get", "bitpipe_stop", "bitpipe_status",
    "list_waveforms", "list_dir", "list_recordings", "waveform_rate",
    "set_role", "fpga_status",
})

# panels a GUI may show; a role lists the ones it uses
PANELS = ("tune", "rx", "tx", "jammer", "playback", "record", "transponder",
          "bitpipe", "detections")

ROLES = {
    "any": {
        "label": "Any (engineering)", "short": "ANY", "tx": True,
        "refuse": frozenset(),
        "panels": frozenset(PANELS),
        "help": "every control; nothing refused",
    },
    "monitor": {
        "label": "Receiver / monitor", "short": "RX", "tx": False,
        "refuse": _TX_CMDS,
        "panels": frozenset({"tune", "rx", "record", "detections"}),
        "help": "receive, detect, decode and record; the radio refuses anything that transmits",
    },
    "jammer": {
        "label": "Jammer", "short": "JAM", "tx": True,
        "refuse": frozenset({"bitpipe_start", "transponder", "transceive"}),
        "panels": frozenset({"tune", "rx", "tx", "jammer", "detections"}),
        "help": "jammer and test signals on the TX port",
    },
    "playback": {
        "label": "IQ playback", "short": "PLAY", "tx": True,
        "refuse": frozenset({"bitpipe_start", "transponder", "transceive"}),
        "panels": frozenset({"tune", "rx", "tx", "playback", "record", "detections"}),
        "help": "play IQ files, scenarios and recordings",
    },
    "transponder": {
        "label": "Transponder", "short": "XPDR", "tx": True,
        "refuse": frozenset({"bitpipe_start"}),
        "panels": frozenset({"tune", "rx", "tx", "transponder", "detections"}),
        "help": "detect, then key a response",
    },
    "bitpipe": {
        "label": "Bitpipe modem", "short": "PIPE", "tx": True,
        "refuse": frozenset({"set_tx_waveform", "transponder", "transceive",
                             "replay_recording", "ground_checkout"}),
        "panels": frozenset({"tune", "rx", "tx", "bitpipe"}),
        "help": "streamed two way link; TX keys only the stream modem",
    },
    "dcfe": {
        "label": "dCFE node", "short": "dCFE", "tx": False,
        "refuse": None,          # allow list: only ALWAYS passes
        "panels": frozenset(),
        "help": "dCFE owns the radio; DPMG only reads status",
    },
}

ORDER = ("any", "monitor", "jammer", "playback", "transponder", "bitpipe", "dcfe")

# Mission planner: catalog group to the roles that may run it. "any" runs all.
_GROUP_ROLES = {
    "Radio setup": set(ORDER) - {"dcfe"},
    "Comms": {"playback"},
    "Jammers": {"jammer"},
    "IQ playback": {"playback"},
    "Tools generators": {"jammer", "playback"},
    "Transponder": {"transponder"},
    "Record and replay": {"playback"},
    "Secure": {"bitpipe"},
}
# per technique exceptions to the group rule
_TECH_ROLES = {
    "record": set(ORDER) - {"dcfe", "bitpipe"},
    "rx_dwell": set(ORDER) - {"dcfe"},
}


def known(role) -> bool:
    return role in ROLES


def label(role) -> str:
    return ROLES.get(role or DEFAULT, ROLES[DEFAULT])["label"]


def short(role) -> str:
    return ROLES.get(role or DEFAULT, ROLES[DEFAULT])["short"]


def choices() -> dict:
    """{id: label} in display order, for a dropdown."""
    return {r: ROLES[r]["label"] for r in ORDER}


def panels(role) -> frozenset:
    return ROLES.get(role or DEFAULT, ROLES[DEFAULT])["panels"]


def shows(role, panel) -> bool:
    return panel in panels(role)


def can_tx(role) -> bool:
    return bool(ROLES.get(role or DEFAULT, ROLES[DEFAULT])["tx"])


def refusal(role, cmd, live_tx_source=False):
    """Why this role refuses the command, or None if it is allowed.
    live_tx_source: the bitpipe stream modem is attached (start_tx is then
    the stream, which a bitpipe radio may key)."""
    role = role or DEFAULT
    r = ROLES.get(role)
    if r is None or cmd in ALWAYS:
        return None
    if r["refuse"] is None:
        return (f"radio role is {r['label']}: dCFE owns this radio, DPMG does not "
                f"run {cmd} (change the role to use it here)")
    if cmd in r["refuse"]:
        return f"radio role is {r['label']}: {cmd} is not used in this role"
    if role == "bitpipe" and cmd == "start_tx" and not live_tx_source:
        return (f"radio role is {r['label']}: start_tx keys only the stream modem "
                f"(bitpipe_start first)")
    return None


def technique_allowed(role, technique, group=None) -> bool:
    """Can a radio in this role run this mission technique? group is the
    catalog group (looked up by the caller)."""
    role = role or DEFAULT
    if role == "any":
        return True
    if role not in ROLES:
        return False
    if technique in _TECH_ROLES:
        return role in _TECH_ROLES[technique]
    return role in _GROUP_ROLES.get(group, set())


# persistence (service side)

def load(path, default=DEFAULT) -> dict:
    """{"role", "by", "t"} from the role file, or the default if the file is
    missing or holds an unknown role."""
    try:
        with open(path) as fh:
            d = json.load(fh)
        if known(d.get("role")):
            return {"role": d["role"], "by": d.get("by"), "t": d.get("t")}
    except (OSError, ValueError, AttributeError):
        pass
    return {"role": default if known(default) else DEFAULT, "by": None, "t": None}


def save(path, role, by=None) -> dict:
    d = {"role": role, "by": by, "t": time.time()}
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(d, fh)
    os.replace(tmp, path)
    return d
