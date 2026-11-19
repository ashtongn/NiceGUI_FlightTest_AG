"""Per-step RX and TX port choice for the mission planner.

A step may carry {"ports": {"rx": "TRx1B", "tx": "TRx1A"}}: the SMA jacks it
wants on its radio. Either key may be missing, which means "whatever the radio
is on". The names are the operator console's: TRx1A, TRx2A, TRx1B, TRx2B on a
G40 (card A = 1A/2A, card B = 1B/2B), the RXA1/RXA2/RXB1/RXB2 and TXA1 handles
on an X40, RX2 / TX/RX on a B200. Ports are init parameters on the Epiq cards,
so a step whose ports differ from the radio's current ones makes the runner
re-initialize the radio first (35 to 46 s measured on a G40, RX stops); validate() warns
where that will happen.

The G40 mapping is the one the SDR tab sends (link_tab _PORT_MAP): the jack
gives the card and the RXA1/RXA2 or TXA1/TXA2 handle; RX and TX on different
cards is single chan_mode with tx_card, the same card is dual chan_mode.

RX may also be "none" (RX_NONE): a transmit only step that does not care where
the radio listens. The radio services always open an RX channel, so the runner
still needs some RX port; rx_for_none() picks it. It keeps the radio's current
RX when that can stream with the step's TX and otherwise moves RX somewhere
harmless: on a G40 the other card than the TX jack (same card RX on the 2 jack
is refused, and same card TX on a 2 jack only reaches the jack by coupling), on
an X40 the B side. A run of RX none steps therefore re-initializes at most once.

The functions that take pm follow the radio's port map (port_map.py, from its
get_config): a jack the map locks for a side is flagged and never picked for RX
none. No map (radio offline, or none in its config) means no restriction here;
the radio's service refuses a locked jack whatever the plan says.
"""
from __future__ import annotations

from dpmg.radio import port_map as _pm

G40_PORTS = ("TRx1A", "TRx2A", "TRx1B", "TRx2B")
_G40 = {"TRx1A": (0, "RXA1", "TXA1"), "TRx2A": (0, "RXA2", "TXA2"),
        "TRx1B": (1, "RXA1", "TXA1"), "TRx2B": (1, "RXA2", "TXA2")}
X40_RX = ("RXB1", "RXB2", "RXA1", "RXA2")
X40_TX = ("TXA1",)
B200_RX = ("RX2", "TX/RX")
B200_TX = ("TX/RX",)
RX_NONE = "none"

# how long a re-init costs, seconds, for port and rate changes. The G40 figure covers the
# 35 to 46 s measured on a bench G40 on 2026-11-05; the others are not measured here.
REINIT_S = {"g40": 50, "x40": 30, "b200": 3}


def _kind(rtype) -> str:
    return str(rtype or "").lower()


def rx_none_ok(rtype) -> bool:
    """RX "none" is offered on the radio types whose ports the planner knows."""
    return bool(rx_options(rtype))


def rx_options(rtype) -> list:
    k = _kind(rtype)
    if k == "g40":
        return list(G40_PORTS)
    if k == "x40":
        return list(X40_RX)
    if k == "b200":
        return list(B200_RX)
    return []


def tx_options(rtype) -> list:
    k = _kind(rtype)
    if k == "g40":
        return list(G40_PORTS)
    if k == "x40":
        return list(X40_TX)
    if k == "b200":
        return list(B200_TX)
    return []


def step_ports(step: dict) -> tuple:
    """(rx, tx) port names on a step, None where not chosen."""
    p = step.get("ports") if isinstance(step, dict) else None
    if not isinstance(p, dict):
        return None, None
    rx = str(p.get("rx") or "").strip() or None
    tx = str(p.get("tx") or "").strip() or None
    if rx and rx.lower() == RX_NONE:
        rx = RX_NONE
    return rx, tx


def set_step_ports(step: dict, rx, tx) -> None:
    """Write the choice on the step; both None removes the block."""
    p = {}
    if rx:
        p["rx"] = str(rx)
    if tx:
        p["tx"] = str(tx)
    if p:
        step["ports"] = p
    else:
        step.pop("ports", None)


def invalid(rtype, rx, tx, pm=None) -> list:
    """Messages for port names the radio type does not have (none for a type
    with no known port list) and for jacks the radio's port map locks."""
    out = []
    ro, to = rx_options(rtype), tx_options(rtype)
    if rx == RX_NONE:
        rx = None
    if rx and ro and rx not in ro:
        out.append(f"RX port {rx!r} is not one of {', '.join(ro)} on a {rtype}")
    if tx and to and tx not in to:
        out.append(f"TX port {tx!r} is not one of {', '.join(to)} on a {rtype}")
    for jack, side in ((rx, "rx"), (tx, "tx")):
        if jack and pm and not _pm.allows(pm, jack, side):
            chain = _pm.chain_of(pm, jack)
            can = _pm.jacks_for(pm, side)
            out.append(f"{side.upper()} port {jack} is locked: the radio's port map says "
                       f"{_pm.ROLE_TEXT.get(_pm.role_of(pm, jack), 'locked')}"
                       f"{f' ({chain})' if chain else ''}. {side.upper()} jacks: "
                       f"{', '.join(can) if can else 'none'}")
    return out


def conflict(rtype, rx, tx):
    """Why this RX and TX pair cannot stream together on this radio, or None.
    G40: the same-card rule the SDR tab enforces (RX on a 2 jack with TX on the
    1 jack of the same card). X40: RX on the A side shares the TX LO, so it is
    receive only."""
    k = _kind(rtype)
    if not rx or not tx or rx == RX_NONE:
        return None
    if k == "g40":
        try:
            from dpmg.radio.sdr_core import g40_same_card_conflict
        except Exception:
            return None
        if rx in _G40 and tx in _G40:
            return g40_same_card_conflict(rx, tx)
        return None
    if k == "x40" and rx in ("RXA1", "RXA2"):
        return (f"X40: RX on {rx} shares the TX LO and is receive only; a step "
                f"that transmits needs RX on RXB1 or RXB2")
    return None


def same_card_note(rtype, rx, tx):
    """G40: RX on a 1 jack with TX on the 2 jack of one card streams but sends
    nothing real out of the jack (port_map.same_card_tx_note), else None."""
    return _pm.same_card_tx_note(rx, tx) if _kind(rtype) == "g40" else None


def init_fields(rtype, rx, tx) -> dict:
    """The init parameters a port choice sets: rx_antenna, tx_antenna and, on a
    G40, extras card / tx_card / chan_mode. Only the chosen side is set; RX
    none sets nothing (resolve it with rx_for_none first)."""
    k = _kind(rtype)
    out = {}
    if rx == RX_NONE:
        rx = None
    if k == "g40":
        ex = {}
        if rx in _G40:
            card, rxa, _ = _G40[rx]
            out["rx_antenna"] = rxa
            ex["card"] = card
        if tx in _G40:
            tcard, _, txa = _G40[tx]
            out["tx_antenna"] = txa
            ex["tx_card"] = tcard
        if rx in _G40 and tx in _G40:
            ex["chan_mode"] = "dual" if _G40[rx][0] == _G40[tx][0] else "single"
        if ex:
            out["extras"] = ex
        return out
    if rx:
        out["rx_antenna"] = rx
    if tx:
        out["tx_antenna"] = tx
    return out


def current_ports(snapshot: dict, rtype) -> tuple:
    """(rx, tx) port names the radio is on now, from a get_config snapshot;
    None where the snapshot does not say."""
    snap = snapshot or {}
    rxa, txa = snap.get("rx_antenna"), snap.get("tx_antenna")
    k = _kind(rtype)
    if k != "g40":
        return (str(rxa) if rxa else None), (str(txa) if txa else None)
    try:
        card = int(snap.get("card") if snap.get("card") is not None else 0)
    except (TypeError, ValueError):
        card = 0
    try:
        tcard = int(snap.get("tx_card") if snap.get("tx_card") is not None else card)
    except (TypeError, ValueError):
        tcard = card
    rx = next((p for p, (c, r, _) in _G40.items() if c == card and r == rxa), None)
    tx = next((p for p, (c, _, t) in _G40.items() if c == tcard and t == txa), None)
    return rx, tx


def _rx_fits(rtype, rx, tx, pm=None) -> bool:
    """RX port rx can stay while the radio transmits on tx (for RX none)."""
    k = _kind(rtype)
    if rx and pm and not _pm.allows(pm, rx, "rx"):
        return False
    if not rx or not tx:
        return bool(rx)
    if k == "g40":
        return rx in _G40 and tx in _G40 and _G40[rx][0] != _G40[tx][0]
    if k == "x40":
        return rx in ("RXB1", "RXB2")
    return True


def rx_fitting(rtype, tx, pm=None) -> list:
    """The RX ports an RX none step leaves alone while transmitting on tx;
    empty when any port does (or nothing is known)."""
    opts = [p for p in rx_options(rtype) if not pm or _pm.allows(pm, p, "rx")]
    fit = [p for p in opts if _rx_fits(rtype, p, tx, pm)]
    return fit if tx and len(fit) < len(opts) else []


def rx_for_none(rtype, cur_rx, tx, pm=None):
    """The RX port a step with RX none runs on: the radio's current RX cur_rx
    when it fits with TX port tx, else a harmless one (G40: the 1 jack of the
    other card, X40: RXB1). A port map narrows the choice; when it leaves nothing
    on the other card the pick is any RX jack it allows. None when nothing is
    known (no current RX and no rule for the type)."""
    if _rx_fits(rtype, cur_rx, tx, pm):
        return cur_rx
    k = _kind(rtype)
    ok = (lambda p: _pm.allows(pm, p, "rx")) if pm else (lambda p: True)
    if k == "g40" and tx in _G40:
        card = _G40[tx][0]
        pick = next((p for p in G40_PORTS if _G40[p][0] != card and ok(p)), None)
        return pick or next((p for p in G40_PORTS if p != tx and ok(p)), None)
    if k == "x40":
        return next((p for p in X40_RX if ok(p)), None)
    return cur_rx


def resolve(snapshot: dict, rtype, rx, tx) -> tuple:
    """(rx, tx) a step really runs on, with RX none turned into a port by
    rx_for_none against the radio's current ports. A TX left unset then comes
    back as the current TX, so init_fields sets the G40 chan_mode for the pair.
    The snapshot's port map, if any, limits where RX none may go."""
    if rx != RX_NONE:
        return rx, tx
    cur_rx, cur_tx = current_ports(snapshot, rtype)
    tx = tx or cur_tx
    return rx_for_none(rtype, cur_rx, tx, (snapshot or {}).get("ports")), tx


def needs_reinit(snapshot: dict, rtype, rx, tx) -> bool:
    """True when the step's chosen ports are not the ones the radio is on. RX
    none only counts when the current RX does not fit with the TX."""
    cur_rx, cur_tx = current_ports(snapshot, rtype)
    rx, tx = resolve(snapshot, rtype, rx, tx)
    if rx and rx != cur_rx:
        return True
    if tx and tx != cur_tx:
        return True
    return False


def label(rx, tx) -> str:
    """Short text for a lane block or an editor line: 'RX TRx1B TX TRx1A'."""
    bits = []
    if rx:
        bits.append(f"RX {rx}")
    if tx:
        bits.append(f"TX {tx}")
    return " ".join(bits)
