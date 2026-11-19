"""
port_map.py: how a radio's SMA jacks are connected and what each may be used for.

The map is the ports: section of that radio's rf_frontend.yaml, one entry per
jack: a role (tx, rx, txrx, none) and a chain, plain text saying what the jack
connects to. One set of rules shared by the service (which refuses commands),
the GUI (which greys out jacks) and the mission planner (which flags steps).

No ports: section means no restriction, so other radios and older files are not
limited. With a section, a jack it does not list counts as none. A map
only takes things away: it never lifts another guard.

The service always opens an RX channel, so a map must leave at least one jack
that can receive. A map with no TX jack is a receive only radio.

Jack names are what the radio's own port dropdown shows: TRx1A, TRx2A, TRx1B,
TRx2B on a G40 (RX and TX handles are turned into these here), the handle names
on an X40 or a B200. Stdlib only: the planner imports this with no radio stack.
"""
from __future__ import annotations

import json
import zlib

ROLES = ("tx", "rx", "txrx", "none")
ROLE_TEXT = {"tx": "TX only", "rx": "RX only", "txrx": "TX and RX", "none": "not connected"}
G40_JACKS = ("TRx1A", "TRx2A", "TRx1B", "TRx2B")
# the X40 is 4R1T: four receive jacks and one transmitter, named by their handles
X40_RX_JACKS = ("RXA1", "RXA2", "RXB1", "RXB2")
X40_TX_JACKS = ("TXA1",)
X40_JACKS = X40_RX_JACKS + X40_TX_JACKS
# the whole map has to fit one CMD_ACK (get_config with ports=true)
CHAIN_MAX = 140
JACKS_MAX = 6

_ALLOWS = {"tx": {"tx"}, "rx": {"rx"}, "txrx": {"tx", "rx"}, "none": set()}
_ENTRY_KEYS = {"role", "chain"}

# G40 handles to the jack digit. TXB1 is the J2 transmitter, TXA2 the console's
# name for it (sdr_core.g40_trx_port). Anything else is not a G40 jack the map
# can vouch for, so with a map it is refused.
_G40_RX = {"RXA1": "1", "RXA2": "2"}
_G40_TX = {"TXA1": "1", "TXA2": "2", "TXB1": "2"}
_G40_LIKE = ("g40", "mock")

# commands that use a side whatever their parameters say
_TX_ONLY = frozenset({"set_tx_waveform", "start_tx", "replay_recording"})
_RX_ONLY = frozenset({"start_rx", "record_iq", "capture_iq", "survey_classify",
                      "channelizer_test", "selftest_rx_wedge"})


def parse(section) -> dict:
    """The ports: mapping checked and normalized to {jack: {role, chain}}. An
    empty section is the same as none. Raises ValueError with the bad key."""
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ValueError(f"must be a mapping of jack names, got {type(section).__name__}")
    if len(section) > JACKS_MAX:
        raise ValueError(f"{len(section)} jacks listed, at most {JACKS_MAX}")
    out = {}
    for jack, entry in section.items():
        jack = str(jack).strip()
        if not jack:
            raise ValueError("empty jack name")
        if jack.lower().startswith("trx") and jack not in G40_JACKS:
            raise ValueError(f"unknown jack {jack!r}, a G40 has {', '.join(G40_JACKS)}")
        if isinstance(entry, str):
            entry = {"role": entry}
        if not isinstance(entry, dict):
            raise ValueError(f"{jack}: must be a role or a mapping with role and chain")
        bad = set(entry) - _ENTRY_KEYS
        if bad:
            raise ValueError(f"{jack}: unknown key {sorted(bad)[0]!r}, allowed: role, chain")
        # YAML reads a bare none as a string but an empty value as null
        role = str(entry.get("role") if entry.get("role") is not None else "").strip().lower()
        if role not in ROLES:
            raise ValueError(f"{jack}: role {role!r} is not one of {', '.join(ROLES)}")
        chain = " ".join(str(entry.get("chain") or "").split())
        if len(chain) > CHAIN_MAX:
            raise ValueError(f"{jack}: chain is {len(chain)} characters, at most {CHAIN_MAX}")
        out[jack] = {"role": role, "chain": chain}
    if out and not any("rx" in _ALLOWS[e["role"]] for e in out.values()):
        raise ValueError("no jack allows RX, and the radio always opens an RX channel: "
                         "mark one unused jack rx")
    return out


def ordered(pm) -> list:
    """The listed jacks, G40 then X40 panel order first."""
    names = [j for j in G40_JACKS + X40_JACKS if j in (pm or {})]
    return names + [j for j in (pm or {}) if j not in names]


def kind_text(kind) -> str:
    """The radio kind as the panels print it: G40, X40, B200, X410."""
    k = str(kind or "").strip()
    return k.upper() if k else "unknown kind"


def jacks_of(kind, caps=None) -> list:
    """The SMA jacks a radio of this kind has, in panel order: the G40 and X40
    names from here, any other kind the rx_antennas and tx_antennas of its
    capability list (a jack that does both is listed once). Empty when nothing
    says what the jacks are, which the lockout panel reads as no port map
    support. caps may also carry port_jacks straight from the service."""
    k = str(kind or "").lower()
    caps = caps or {}
    given = caps.get("port_jacks")
    if isinstance(given, (list, tuple)) and given:
        return [str(j) for j in given]
    if k in _G40_LIKE:
        return list(G40_JACKS)
    if k == "x40":
        return list(X40_JACKS)
    out = []
    for name in list(caps.get("rx_antennas") or []) + list(caps.get("tx_antennas") or []):
        name = str(name).strip()
        if name and name not in out and name.lower() not in ("auto",):
            out.append(name)
    return out


def roles_for(kind, jack) -> tuple:
    """The roles a jack can take. A G40 jack is RX or TX at a time, so all four;
    an X40 receive jack can only be rx or none, its transmitter tx or none. Any
    other kind: all four."""
    if str(kind or "").lower() == "x40":
        if jack in X40_RX_JACKS:
            return ("rx", "none")
        if jack in X40_TX_JACKS:
            return ("tx", "none")
    return ROLES


def unknown_jack(kind, pm, caps=None):
    """The first listed jack that is not one this kind has, or None. A kind with
    no known jack list accepts any name."""
    known = jacks_of(kind, caps)
    if not known:
        return None
    return next((j for j in (pm or {}) if j not in known), None)


def role_of(pm, jack) -> str:
    """A jack's role. pm is {jack: {role, chain}} or the compact {jack: role}."""
    e = (pm or {}).get(jack)
    if e is None:
        return "none"
    return e if isinstance(e, str) else str(e.get("role") or "none")


def chain_of(pm, jack) -> str:
    e = (pm or {}).get(jack)
    return str(e.get("chain") or "") if isinstance(e, dict) else ""


def allows(pm, jack, side) -> bool:
    """May this jack do this side ("tx" or "rx")? No map: yes."""
    if not pm:
        return True
    return side in _ALLOWS.get(role_of(pm, jack), set())


def jacks_for(pm, side) -> list:
    """The listed jacks that may do this side, in G40 panel order first."""
    return [j for j in ordered(pm) if allows(pm, j, side)]


def compact(pm) -> dict:
    """{jack: role}, small enough to ride in every get_config."""
    return {j: role_of(pm, j) for j in (pm or {})}


def version(pm) -> int:
    """Changes when the map does, so a GUI knows to fetch the chain text again."""
    return zlib.crc32(json.dumps(pm or {}, sort_keys=True).encode())


def _card(v, default=0) -> int:
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _g40(kind) -> bool:
    return str(kind or "").lower() in _G40_LIKE


def _side_jack(kind, handle, card, table):
    h = str(handle or "").strip()
    if not _g40(kind):
        return h or None
    n = table.get(h.upper())
    return f"TRx{n}{'B' if _card(card) == 1 else 'A'}" if n else None


def rx_jack(kind, handle, card=0):
    """The jack an RX handle listens on, None when the handle is not one the map
    can vouch for."""
    return _side_jack(kind, handle, card, _G40_RX)


def tx_jack(kind, handle, card=0):
    """The jack a TX handle drives, None when the handle is not one the map can
    vouch for."""
    return _side_jack(kind, handle, card, _G40_TX)


def _refusal(pm, jack, side, source) -> str:
    chain = chain_of(pm, jack)
    where = f" in {source}" if source else ""
    can = jacks_for(pm, side)
    return (f"{side.upper()} on {jack} refused: the port map{where} says {jack} is "
            f"{ROLE_TEXT.get(role_of(pm, jack), role_of(pm, jack))}"
            f"{f' ({chain})' if chain else ''}. {side.upper()} jacks on this radio: "
            f"{', '.join(can) if can else 'none'}.")


def _unknown(side, handle, card, source) -> str:
    where = f" in {source}" if source else ""
    return (f"{side.upper()} handle {handle!r} on card {card} refused: it is not a "
            f"jack the port map{where} can check.")


def init_refusal(pm, kind, p, source=None):
    """Why this init cannot run under the map, or None. An init that names no TX
    antenna (RX only) is allowed: the check then happens when something keys."""
    if not pm:
        return None
    ex = p.get("extras") or {}
    card = _card(ex.get("card"))
    rx = rx_jack(kind, p.get("rx_antenna"), card)
    if rx is None:
        return _unknown("rx", p.get("rx_antenna"), card, source)
    if not allows(pm, rx, "rx"):
        return _refusal(pm, rx, "rx", source)
    txa = p.get("tx_antenna")
    if not txa:
        return None
    tcard = _card(ex.get("tx_card"), card)
    tx = tx_jack(kind, txa, tcard)
    if tx is None:
        return _unknown("tx", txa, tcard, source)
    return None if allows(pm, tx, "tx") else _refusal(pm, tx, "tx", source)


def session_jacks(kind, init, cfg_tx=None) -> tuple:
    """(rx jack, tx jack) of a radio opened with this init. No TX antenna at init
    leaves the backend on its default: TXA1 of the RX card on a G40 or an X40, the
    config's default elsewhere (cfg_tx)."""
    ex = (init or {}).get("extras") or {}
    card = _card(ex.get("card"))
    txa = (init or {}).get("tx_antenna") or (
        "TXA1" if _g40(kind) or str(kind or "").lower() == "x40" else cfg_tx)
    return (rx_jack(kind, (init or {}).get("rx_antenna"), card),
            tx_jack(kind, txa, _card(ex.get("tx_card"), card)))


def session_refusal(pm, kind, init, sides, source=None, cfg_tx=None):
    """Why an open radio cannot use these sides under the map, or None."""
    if not pm:
        return None
    rx, tx = session_jacks(kind, init, cfg_tx)
    ex = (init or {}).get("extras") or {}
    if "rx" in sides:
        if rx is None:
            return _unknown("rx", (init or {}).get("rx_antenna"), _card(ex.get("card")), source)
        if not allows(pm, rx, "rx"):
            return _refusal(pm, rx, "rx", source)
    if "tx" in sides:
        if tx is None:
            return _unknown("tx", (init or {}).get("tx_antenna"), _card(ex.get("tx_card")), source)
        if not allows(pm, tx, "tx"):
            return _refusal(pm, tx, "tx", source)
    return None


def locks_gained(old, new, side) -> list:
    """Jacks new allows for this side that old did not, for a change that opens a
    lock up. A radio with no map allows everything."""
    return [j for j in ordered(new) if old and allows(new, j, side) and not allows(old, j, side)]


def sides_used(name, p=None) -> set:
    """Which sides this command uses on an open radio. Stops, status and tuning
    use none. A disarmed responder or checkout only listens."""
    p = p or {}
    if name in _TX_ONLY:
        return {"tx"}
    if name in _RX_ONLY:
        return {"rx"}
    if name == "bitpipe_start":
        return {"tx", "rx"}
    if name == "transceive":
        if not p.get("enabled", True):
            return set()
        return {"tx", "rx"} if p.get("armed") else {"rx"}
    if name == "ground_checkout":
        return {"tx", "rx"} if p.get("armed") else {"rx"}
    if name == "transponder":
        return {"tx", "rx"} if (p.get("armed") and not p.get("emulate", True)) else {"rx"}
    return set()


def same_card_tx_note(rx, tx):
    """The warning for RX on a 1 jack with TX on the 2 jack of one G40 card. That
    pair opens and streams, but the 2026-10-18 port matrix (moves 3 and 9)
    measured on-card coupling only: nothing real at the 2
    jack. Cross-card TX on the same jack works. None for every other pair."""
    if rx not in G40_JACKS or tx not in G40_JACKS:
        return None
    if rx[4] != tx[4] or rx[3] != "1" or tx[3] != "2":
        return None
    other = "B" if rx[4] == "A" else "A"
    return (f"RX {rx} with TX {tx} are on one card: bench tests found same-card TX on a "
            f"2 jack is on-card coupling only, so expect no RF out of {tx}. "
            f"Receive on TRx1{other} or TRx2{other} (the other card) to transmit.")
