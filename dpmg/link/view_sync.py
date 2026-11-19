"""
Keeps the operator console and the engineering view showing the
radio's real settings, whichever view (or GUI process) changed them.

The radio service is the single source of truth. Its get_config command
reports the full current configuration (clock, ports, rate, centre, gains, TX
attenuation, TX waveform, logging, GPS antenna bias, front-end, attenuator);
link_client polls it once per status period per radio and right after every
state-changing command, and both views copy it into their controls.

This module is GUI-free so the rules are testable offline:

  EditGuard           decides when a service value may overwrite a widget
                      without fighting the person editing it
  op_fields(cfg)      service config: operator console control values
  eng_fields(cfg)     service config: engineering view control values
  pending_labels()    the held (not yet applied) fields worth flagging

Rule (EditGuard): a field is never overwritten while it has focus. After a
client edit it holds the operator's value until either the service's value
for that field changes (someone applied a setting: this view's own START or
live apply, another view, another GUI) or HOLD_S passes with the field
unfocused. Changes to other fields never release the hold.
"""

from __future__ import annotations

import time

HOLD_S = 20.0            # how long an unapplied edit survives without focus
STATE_CMDS = frozenset({ # commands after which link_client re-reads get_config at once
    "init", "close", "set_center", "set_gain", "set_tx_atten", "start_rx", "stop_rx",
    "set_tx_waveform", "start_tx", "stop_tx", "set_plan", "gen_scenario",
    "gen_test_waveform", "resample_waveform", "log_detections", "log_telemetry",
    "gps_antenna_bias", "frontend_set", "frontend_reload", "atten_set",
    "payload_key", "payload_restore", "transceive", "ground_checkout",
})

# operator console reference data (kept in step with link_tab.build_operator_sdr)
OP_ATTEN_DB = [0, 1, 2, 3, 5, 7, 10, 15, 20, 25, 30, 35, 40, 41.75]
_MISSING = object()


class EditGuard:
    """Per-field focus / edit bookkeeping for one view (one browser page)."""

    def __init__(self, hold_s: float = HOLD_S, clock=time.monotonic):
        self.hold_s = float(hold_s)
        self._clock = clock
        self._focus: set = set()
        self._edit: dict = {}          # key: (t_edit, service value at edit time)

    def focus(self, key, on: bool = True):
        if on:
            self._focus.add(key)
        else:
            self._focus.discard(key)
            if key in self._edit:      # the hold window starts when focus leaves
                self._edit[key] = (self._clock(), self._edit[key][1])

    def edited(self, key, svc_value=_MISSING):
        """A client edit of `key`; svc_value = what the service reported then."""
        prev = self._edit.get(key)
        base = svc_value if svc_value is not _MISSING else (prev[1] if prev else None)
        self._edit[key] = (self._clock(), base)

    def release(self, key):
        self._edit.pop(key, None)

    def held(self, key) -> bool:
        return key in self._focus or key in self._edit

    def may_apply(self, key, svc_value) -> bool:
        """True when the service's value may be written into the widget now."""
        if key in self._focus:
            return False
        e = self._edit.get(key)
        if e is None:
            return True
        t, base = e
        if svc_value != base or self._clock() - t > self.hold_s:
            del self._edit[key]
            return True
        return False


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def nearest(options, value):
    """The option (number or numeric string) closest to value; None if no options."""
    v = _f(value)
    if v is None:
        return None
    best, bd = None, None
    for o in options:
        ov = _f(o)
        if ov is None:
            continue
        d = abs(ov - v)
        if bd is None or d < bd:
            best, bd = o, d
    return best


def atten_key(db, options=None):
    """TX attenuation in dB: the operator/engineering dropdown key ('20', '41.75')."""
    keys = [str(d) for d in (options or OP_ATTEN_DB)]
    return nearest(keys, db)


def g40_trx(card, handle) -> str | None:
    """(card, RX/TX handle) then G40 front-panel jack name: RXA1@0 then TRx1A,
    TXB1/TXA2 (the J2 TX path) then TRx2, card 1 then B."""
    if not handle:
        return None
    h = str(handle).upper()
    n = "2" if h in ("TXB1", "TXA2", "RXA2") else "1" if h in ("RXA1", "TXA1") else None
    if n is None:
        return None
    return f"TRx{n}{'B' if int(card or 0) == 1 else 'A'}"


def is_inited(cfg) -> bool:
    return bool(cfg) and bool(cfg.get("inited"))


def op_fields(cfg: dict) -> dict:
    """Service get_config: operator console values (keys = widget names in
    build_operator_sdr). Only keys the service actually reported are returned,
    so an older service / a closed radio leaves the other widgets alone."""
    out = {}
    if not is_inited(cfg):
        if "clock_source" in (cfg or {}):
            out["clk"] = str(cfg["clock_source"])
        return out
    kind = str(cfg.get("kind") or "")
    if cfg.get("center_hz") is not None:
        out["center_mhz"] = round(float(cfg["center_hz"]) / 1e6, 6)
    rate = cfg.get("sample_rate") or cfg.get("achieved_rate")
    if rate:
        out["rate"] = float(rate)
    if cfg.get("clock_source"):
        out["clk"] = str(cfg["clock_source"])
    if kind in ("g40", "mock"):
        rx = g40_trx(cfg.get("card", 0), cfg.get("rx_antenna"))
        tc = cfg.get("tx_card")
        tx = g40_trx(cfg.get("card", 0) if tc is None else tc, cfg.get("tx_antenna"))
        if rx:
            out["rx_port"] = rx
        if tx:
            out["tx_port"] = tx
    elif kind == "x40":
        if cfg.get("rx_antenna"):
            out["rx_port"] = str(cfg["rx_antenna"])
        if cfg.get("tx_antenna"):
            out["tx_port"] = str(cfg["tx_antenna"])
    if kind in ("g40", "x40", "mock"):
        if cfg.get("rx_gain") is not None:
            out["rx_gain"] = str(int(round(float(cfg["rx_gain"]))))
        if cfg.get("tx_gain") is not None:
            out["tx_atten"] = atten_key(cfg["tx_gain"])
    return out


# engineering operating modes (kept in step with link_tab.build_remote_sdr _MODES):
# (tx handle, chan_mode): preferred label; the label is not sent to the radio,
# only its handle + chan_mode are, so several labels map to the same params.
ENG_MODE_FOR = {
    (None, "single"): "RX only",
    ("TXA1", "single"): "TX only: TRx1 (TxA1)",
    ("TXB1", "dual"): "Transceive FDD (RX TRx1 + TX TRx2)",
}
ENG_MODE_PARAMS = {
    "RX only": (None, "single"),
    "TX only: TRx1 (TxA1)": ("TXA1", "single"),
    "TX only: TRx2 (TxB1)": ("TXB1", "dual"),
    "Transceive FDD (RX TRx1 + TX TRx2)": ("TXB1", "dual"),
    "Transceive TDD same-port (TRx1)": ("TXA1", "single"),
}
ENG_CROSS_MODE = "Transceive cross-card (RX one card, TX the other)"
ENG_MODE_PARAMS[ENG_CROSS_MODE] = ("CROSS", "single")   # chosen by tx_card != card


def eng_mode(cfg: dict, current: str | None = None) -> str:
    """Pick the engineering Mode label for the radio's (tx handle, chan_mode).
    Keeps the current label when it already maps to the same params."""
    tc = cfg.get("tx_card")
    if cfg.get("tx_antenna") and tc is not None and int(tc) != int(cfg.get("card") or 0):
        return ENG_CROSS_MODE                       # RX and TX on different cards
    tx = cfg.get("tx_antenna")
    tx = None if not tx else ("TXB1" if str(tx).upper() in ("TXB1", "TXA2") else str(tx).upper())
    cm = str(cfg.get("chan_mode") or ("dual" if tx == "TXB1" else "single"))
    want = (tx, cm)
    if current in ENG_MODE_PARAMS and ENG_MODE_PARAMS[current] == want:
        return current
    if want in ENG_MODE_FOR:
        return ENG_MODE_FOR[want]
    for label, prm in ENG_MODE_PARAMS.items():     # same handle, other chan_mode
        if prm[0] == tx:
            return label
    return "RX only"


def eng_fields(cfg: dict, current_mode: str | None = None) -> dict:
    """Service get_config: engineering view values (keys = widget names in
    build_remote_sdr). Only what the service reported."""
    out = {}
    if not is_inited(cfg):
        if "clock_source" in (cfg or {}):
            out["clk"] = str(cfg["clock_source"])
        return out
    kind = str(cfg.get("kind") or "")
    if kind:
        out["kind"] = kind
    if cfg.get("clock_source"):
        out["clk"] = str(cfg["clock_source"])
    rate = cfg.get("sample_rate") or cfg.get("achieved_rate")
    if rate:
        out["rate"] = float(rate)
    if cfg.get("center_hz") is not None:
        out["cf"] = str(int(round(float(cfg["center_hz"]))))
    txc = cfg.get("tx_center_hz")
    if txc is None:
        txc = cfg.get("center_hz")
    if txc is not None:
        out["txcf"] = str(int(round(float(txc))))
    if kind in ("g40", "x40", "mock"):
        out["card"] = int(cfg.get("card") or 0)
        tc = cfg.get("tx_card")
        out["txcard"] = int(out["card"] if tc is None else tc)
        if cfg.get("rx_antenna"):
            out["rxant"] = str(cfg["rx_antenna"])
        out["mode"] = eng_mode(cfg, current_mode)
        if out["mode"] == ENG_CROSS_MODE:      # "same" or the TX rate
            txr = cfg.get("tx_sample_rate")
            rr = out.get("rate")
            out["txrate"] = ("same" if not txr or (rr and abs(float(txr) - float(rr)) < 1.0)
                             else str(int(round(float(txr)))))
        if out["mode"] == ENG_CROSS_MODE and cfg.get("tx_antenna"):
            t = str(cfg["tx_antenna"]).upper()
            out["txant"] = "TXA2" if t in ("TXA2", "TXB1") else "TXA1"   # TRx2x / TRx1x
        if cfg.get("rx_gain") is not None:
            out["gain"] = str(int(round(float(cfg["rx_gain"]))))
        if cfg.get("tx_gain") is not None:
            out["txatten"] = atten_key(cfg["tx_gain"])
    psd = cfg.get("psd") or {}
    if psd.get("nfft"):
        out["nfft"] = str(int(psd["nfft"]))
    if psd.get("window"):
        out["win"] = str(psd["window"])
    if psd.get("buffer"):
        out["buf"] = str(int(psd["buffer"]))
    wf = cfg.get("tx_wf") or {}
    if wf.get("mode") == "tone":
        out["tx_kind"] = "tone"
        if wf.get("offset_hz") is not None:
            out["tx_off_khz"] = float(wf["offset_hz"]) / 1e3
        if wf.get("ampl") is not None:
            out["tx_ampl"] = float(wf["ampl"])
    elif wf.get("path"):
        out["tx_kind"] = "file"
        out["tx_file"] = str(wf["path"])
    return out


def log_text(cfg: dict) -> str:
    """One-line logging state from get_config 'log' (radio-side CSVs)."""
    lg = (cfg or {}).get("log") or {}
    on = [k for k in ("detections", "telemetry") if lg.get(k)]
    if not on:
        return "radio logging off"
    return "radio logging " + ", ".join(f"{k} to {lg[k]}" for k in on)


def tx_wf_text(cfg: dict) -> str:
    wf = (cfg or {}).get("tx_wf")
    if not wf:
        return "TX waveform: none loaded"
    return "TX waveform: " + str(wf.get("desc") or wf.get("path") or wf.get("mode"))


def pending_labels(guard: EditGuard, widget_vals: dict, svc_vals: dict, names: dict) -> list:
    """Labels of fields the operator edited that still differ from the radio
    (held by the guard, not yet applied)."""
    out = []
    for k, label in names.items():
        if k not in svc_vals or not guard.held(k):
            continue
        a, b = widget_vals.get(k), svc_vals[k]
        fa, fb = _f(a), _f(b)
        same = (abs(fa - fb) < 1e-6) if (fa is not None and fb is not None) else (str(a) == str(b))
        if not same:
            out.append(label)
    return out
