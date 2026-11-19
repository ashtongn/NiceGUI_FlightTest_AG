"""
port_pick_ui.py: the GUI side of the port map (dpmg/radio/port_map.py), used by
the operator console, the engineering SDR tab and the mission step editor.

The map lives on the radio (rf_frontend.yaml, enforced by dpmg_service) and
every GUI only follows it. get_config carries the roles and a version number;
the chain text is fetched once per version (get_config with ports=true) because
it would not fit the snapshot polled every second. Greying a jack out here is
only a courtesy: the service refuses it for every client anyway.

The lockout panel in the engineering view edits the map through the service's
set_ports command, which writes the radio's own file. It follows the radio
selected in the SDR tab, whatever its kind: the jack rows come from that
radio's jack list (port_map.jacks_of, from the kind or its capability list),
Apply and the read-back go to that radio only, and an edit not yet applied is
kept per radio when the selection moves to another one. Nothing else is kept
here.
"""
from __future__ import annotations

import asyncio
import json
import time

from nicegui import ui

from dpmg.link import proto
from dpmg.radio import port_map as _ports

_FULL = {}          # serial: (ports_v, {jack: {role, chain}} or None)
_TRIED = {}         # (serial, ports_v): monotonic time of the last fetch
_RETRY_S = 10.0
_SEEN = {}          # serial: (ports_v, ports_by) last seen, to log a change once
_event_log = None   # where a change goes: the GUI's activity log


def set_event_log(fn):
    global _event_log
    _event_log = fn


def _note_change(serial, cfg):
    """A radio's map changed while this GUI was watching: say so, and who did it."""
    now = (cfg.get("ports_v"), cfg.get("ports_by"))
    was = _SEEN.get(serial)
    _SEEN[serial] = now
    if was is None or was == now or _event_log is None:
        return
    what = "port map changed" if cfg.get("ports") else "port map removed"
    _event_log(f"{serial} {what}, by {cfg.get('ports_by') or 'unknown'}")


async def _fetch_full(host, v, on_ready=None):
    try:
        ack = await host.cmd("get_config", {"ports": True}, timeout=5.0)
        r = getattr(ack, "result", None) or {}
        _FULL[host.serial] = (r.get("ports_v", v), r.get("ports_full"))
    except Exception:
        return                  # roles still work; tried again after _RETRY_S
    if on_ready is not None:
        try:
            on_ready()
        except Exception:
            pass


def port_view(host, cfg, on_ready=None):
    """What the pickers need for this radio from its get_config snapshot, or None
    when it has no map: {pm, v, err, sig}. pm is {jack: {role, chain}} once the
    chain text has arrived and {jack: role} until then; port_map reads both.
    sig changes whenever the pickers should be drawn again."""
    cfg = cfg or {}
    serial = getattr(host, "serial", None)
    if cfg and serial:
        _note_change(serial, cfg)
    roles, err = cfg.get("ports"), cfg.get("ports_err")
    if not roles and not err:
        return None
    v = cfg.get("ports_v")
    got = _FULL.get(serial)
    full = got[1] if got and got[0] == v else None
    if roles and not (got and got[0] == v) and serial \
            and time.monotonic() - _TRIED.get((serial, v), -_RETRY_S) >= _RETRY_S:
        _TRIED[(serial, v)] = time.monotonic()
        asyncio.create_task(_fetch_full(host, v, on_ready))
    return {"pm": full or roles or None, "v": v, "err": err, "sig": (v, bool(full), err)}


def _clean(text) -> str:
    """Chain text goes into a Vue template as a single quoted string literal."""
    return "".join(c for c in str(text) if c not in "<>{}'\"\\`")


def _set_disabled(sel, labels):
    if labels:
        js = json.dumps(list(labels)).replace('"', "'")
        sel.props(f':option-disable="o => {js}.includes(o.label)"')
    else:
        sel.props(remove=":option-disable")


def _set_tips(sel, tips):
    """One tooltip per dropdown row: the chain behind that jack, set as each row's
    title when the menu opens. Not a q-tooltip in an option slot: NiceGUI mounts a
    slot template afresh on every page update, about once a second on these pages,
    which drops the tooltip and the rows under the mouse. The handler goes on once
    and reads its table from the page, because this runs every time a list changes
    and an element's listeners only ever add up."""
    table = json.dumps({_clean(k): _clean(v) for k, v in tips.items()})
    if not getattr(sel, "_port_tips_hooked", False):
        if not tips:
            return
        hook_tips(sel)
    sel.client.run_javascript(
        f"(window.dpmgPortTips = window.dpmgPortTips || {{}})[{sel.id}] = {table}")


def hook_tips(sel):
    """Put the row tooltip handler on a dropdown. Call it while the page is built:
    a listener added to a dropdown already on screen makes the browser redraw it
    ("Event listeners changed after initial definition")."""
    if getattr(sel, "_port_tips_hooked", False):
        return sel
    sel._port_tips_hooked = True
    # popup-show fires before the menu's rows exist, so set the titles just after
    sel.on("popup-show", js_handler=(
        f"() => {{ const t = (window.dpmgPortTips || {{}})[{sel.id}] || {{}}; "
        "const go = () => document.querySelectorAll('.q-menu [role=option]').forEach(r => { "
        "r.title = t[r.innerText.trim()] || ''; }); "
        "setTimeout(go, 60); setTimeout(go, 400); }"))
    return sel


def grey_labels(sel, labels):
    """Grey out dropdown rows by their label text and change nothing else."""
    _set_disabled(sel, labels)
    sel.update()


def apply(sel, view, side, jack_of=None, skip=(), move=True):
    """Follow the map on one port dropdown: rows the jack may not be used for read
    'TRx1A (RX only)' and cannot be picked, each row's tooltip is its chain text.
    jack_of turns an option value into a jack name (default: they are the same);
    skip lists values that are not jacks (a blank, or RX none). move=True steps
    off a greyed out value to the first allowed one. No map and nothing drawn
    earlier: the dropdown is left exactly as it is."""
    drawn = getattr(sel, "_port_drawn", None) is not None and sel.options is sel._port_drawn
    base = sel._port_base if drawn else sel.options
    if not isinstance(base, dict):
        base = {o: o for o in base}
    pm = view.get("pm") if view else None
    if not pm and not getattr(sel, "_port_on", False):
        return
    sel._port_on = bool(pm)
    jack_of = jack_of or (lambda v: v)
    labels, blocked, tips, first_ok = {}, [], {}, None
    for val, lab in base.items():
        if not pm or val in skip:
            labels[val] = lab
            continue
        jack = jack_of(val)
        if _ports.allows(pm, jack, side):
            labels[val] = lab
            first_ok = val if first_ok is None else first_ok
        else:
            role = _ports.ROLE_TEXT.get(_ports.role_of(pm, jack), "locked")
            labels[val] = f"{lab} ({role})"
            blocked.append(labels[val])
        chain = _ports.chain_of(pm, jack)
        if chain:
            tips[labels[val]] = chain
    value = sel.value
    if move and pm and value in base and labels.get(value) in blocked and first_ok is not None:
        value = first_ok
    sel._port_base, sel._port_drawn = base, labels if pm else None
    _set_disabled(sel, blocked)
    _set_tips(sel, tips)
    sel.set_options(labels if pm else base, value=value)
    _selected_tip(sel, (pm, jack_of, skip))


def _selected_text(sel) -> str:
    pm, jack_of, skip = sel._port_state
    return "" if not pm or sel.value in skip else _ports.chain_of(pm, jack_of(sel.value))


def _selected_tip(sel, state):
    """The closed dropdown's own tooltip names the chain of the picked jack."""
    sel._port_state = state
    tip = getattr(sel, "_port_tip", None)
    if tip is None:
        if not state[0]:
            return
        with sel:
            tip = ui.tooltip("")
        sel._port_tip = tip
        sel.on_value_change(lambda _e: _show_selected(sel))
    _show_selected(sel)


def _show_selected(sel):
    text = _selected_text(sel)
    sel._port_tip.set_text(text)
    sel._port_tip.set_visibility(bool(text))


def lock_note(view, rx, tx):
    """(text, is_refusal) for a port note under the pickers, or (None, False).
    Refusals are what the service would say; the same-card note is a warning."""
    if view and view.get("err"):
        return f"Port map did not load: {view['err']}", True
    pm = view.get("pm") if view else None
    if pm:
        for jack, side in ((rx, "rx"), (tx, "tx")):
            if jack and not _ports.allows(pm, jack, side):
                role = _ports.ROLE_TEXT.get(_ports.role_of(pm, jack), "locked")
                return (f"Locked by the port map: {jack} is {role}, so it cannot "
                        f"{'receive' if side == 'rx' else 'transmit'}"), True
    note = _ports.same_card_tx_note(rx, tx)
    return (f"Warning: {note}", False) if note else (None, False)


def change_lines(old, new) -> list:
    """Plain text for the confirm dialog: what would change, jack by jack."""
    out = []
    for jack in _ports.ordered(new or old):
        ro, rn = _ports.role_of(old, jack) if old else "txrx", _ports.role_of(new, jack) if new else "txrx"
        co, cn = _ports.chain_of(old, jack), _ports.chain_of(new, jack)
        if ro != rn:
            out.append(f"{jack}: {_ports.ROLE_TEXT.get(ro, ro)} to {_ports.ROLE_TEXT.get(rn, rn)}")
        if co != cn:
            out.append(f"{jack}: chain text changed")
    return out


class LockoutEdits:
    """Edits not yet applied, kept per radio serial, so switching the selected
    radio and back does not lose them. Plain data, so the panel logic can be
    tested without a page."""

    def __init__(self):
        self._by = {}            # serial: {jack: {role, chain}}

    def keep(self, serial, edited: dict) -> None:
        if serial:
            self._by[serial] = dict(edited)

    def take(self, serial):
        """The kept edit for this radio, or None; it stays kept until drop()."""
        return self._by.get(serial)

    def drop(self, serial) -> None:
        self._by.pop(serial, None)

    def others(self, serial) -> list:
        """Radios other than this one with an unapplied edit."""
        return sorted(s for s in self._by if s != serial)


def lockout_title(serial, kind, jacks, view) -> str:
    """The panel's one line on the selected radio: who it is, its kind, and the
    state of its map. No jack list means the kind has no port map support."""
    if not serial:
        return "no radio selected"
    who = f"{serial} ({_ports.kind_text(kind)})"
    if not jacks:
        return f"{who}: this radio reports no port map support"
    err = (view or {}).get("err")
    if err:
        return f"{who}: the file did not load: {err}"
    if view:
        return f"{who}: lockout on"
    return f"{who}: no lockout, every jack can do TX and RX"


def build_lockout_panel(get_host, cfg_of, by="engineering view", caps_of=None, kind_of=None):
    """Engineering view panel: each jack of the selected radio, its role and
    chain text as the radio reports them, with Apply sending set_ports to that
    radio. The rows are built again when the selected radio or its jack list
    changes. An edit stays on screen until it is applied or reverted, and is
    kept for its radio when another one is selected; otherwise the rows follow
    the radio. caps_of(host) gives the radio's capability list (for the jack
    names of kinds this module does not know), kind_of(host, cfg) its kind."""
    state = {"serial": None, "sig": None, "dirty": False, "drawing": False,
             "jacks": None, "kind": None}
    rows = {}
    kept = LockoutEdits()
    caps_of = caps_of or (lambda _h: None)
    kind_of = kind_of or (lambda _h, cfg: (cfg or {}).get("kind"))
    with ui.expansion("Ports lockout", icon="lock").classes("w-full border rounded") as exp:
        ui.label("What each SMA jack of the selected radio may be used for and what it "
                 "connects to. Saved in the ports: section of that radio's rf_frontend.yaml, "
                 "and the radio refuses anything else from every GUI and script. Pick the "
                 "radio with the cards above.").classes("text-xs text-gray-600")
        now_lbl = ui.label("no radio selected").classes("text-sm font-bold")
        by_lbl = ui.label("").classes("text-xs font-mono text-gray-600")
        rows_box = ui.column().classes("w-full gap-1")
        note_lbl = ui.label("").classes("text-xs")
        with ui.row().classes("items-center gap-3"):
            apply_btn = ui.button("Apply", icon="lock").props("dense color=primary no-caps")
            revert_btn = ui.button("Revert").props("dense outline no-caps")
            clear_btn = ui.button("Remove lockout", icon="lock_open") \
                .props("dense outline color=negative no-caps")

    def _edited():
        return {j: {"role": r.value, "chain": " ".join(str(c.value or "").split())}
                for j, (r, c) in rows.items()}

    def _on_edit():
        if not state["drawing"]:
            state["dirty"] = True
            _show_note()

    def _build_rows(kind, jacks):
        """One row per jack of this radio, in its own role choices."""
        rows.clear()
        rows_box.clear()
        with rows_box:
            for jack in jacks:
                allowed = _ports.roles_for(kind, jack)
                opts = {r: _ports.ROLE_TEXT[r] for r in allowed}
                default = "txrx" if "txrx" in allowed else allowed[0]
                with ui.row().classes("items-center gap-3 w-full no-wrap"):
                    ui.label(jack).classes("font-mono text-sm w-14")
                    role = ui.select(opts, value=default, label="may be used for") \
                        .props("dense outlined").classes("w-44")
                    chain = ui.input("connects to", value="") \
                        .props(f"dense outlined maxlength={_ports.CHAIN_MAX}").classes("grow")
                rows[jack] = (role, chain)
                role.on_value_change(lambda _e: _on_edit())
                chain.on_value_change(lambda _e: _on_edit())
        state["jacks"], state["kind"] = list(jacks), kind

    def _view():
        host = get_host()
        return host, (port_view(host, cfg_of(host)) if host is not None else None)

    def _kind_jacks(host, cfg):
        kind = kind_of(host, cfg)
        return kind, _ports.jacks_of(kind, caps_of(host))

    def _draw(view, edited=None):
        """Rows from the radio's map, or from a kept edit."""
        pm = edited if edited is not None else (view["pm"] if view else None)
        state["drawing"] = True
        try:
            for j, (r, c) in rows.items():
                allowed = _ports.roles_for(state["kind"], j)
                role = _ports.role_of(pm, j) if pm else ("txrx" if "txrx" in allowed else allowed[0])
                r.set_value(role if role in allowed else allowed[0])
                c.set_value(_ports.chain_of(pm, j))
        finally:
            state["drawing"] = False
        state["dirty"] = edited is not None

    def _show_note():
        host, _v = _view()
        cfg = (cfg_of(host) or {}) if host is not None else {}
        serial = getattr(host, "serial", None)
        others = kept.others(serial)
        tail = (f" Unapplied edits kept on {', '.join(others)}." if others else "")
        if cfg.get("tx_active"):
            note_lbl.set_text("TX is running: stop it before changing the lockout" + tail)
            note_lbl.classes(replace="text-xs text-red-600 font-bold")
        elif state["dirty"]:
            note_lbl.set_text(f"not applied yet: Apply writes it to {serial}, Revert drops it."
                              f" Switching radios keeps it for {serial}." + tail)
            note_lbl.classes(replace="text-xs text-amber-700 font-bold")
        else:
            note_lbl.set_text(tail.strip())
            note_lbl.classes(replace="text-xs text-gray-600")

    def _tick():
        host, view = _view()
        cfg = (cfg_of(host) or {}) if host is not None else {}
        serial = getattr(host, "serial", None) if host is not None else None
        kind, jacks = _kind_jacks(host, cfg) if host is not None else (None, [])
        on = bool(jacks)
        for w in (apply_btn, revert_btn, clear_btn):
            w.set_enabled(on and not cfg.get("tx_active"))
        if serial != state["serial"]:
            # leaving a radio with an unapplied edit: keep it for when it is back
            if state["dirty"] and state["serial"]:
                kept.keep(state["serial"], _edited())
            state.update(serial=serial, sig=None, dirty=False)
            exp.set_text(f"Ports lockout: {serial} ({_ports.kind_text(kind)})" if serial
                         else "Ports lockout")
        title = lockout_title(serial, kind, jacks, view)
        if now_lbl.text != title:
            now_lbl.set_text(title)
        if host is None or not on:
            by_lbl.set_text("")
            if state["jacks"]:
                _build_rows(None, [])
            _show_note()
            return
        if list(jacks) != state["jacks"] or kind != state["kind"]:
            _build_rows(kind, jacks)
            state["sig"] = None
        by_lbl.set_text(f"last changed by {cfg.get('ports_by')}" if cfg.get("ports_by") else "")
        sig = (serial, view["sig"] if view else None)
        if sig != state["sig"] and not state["dirty"]:
            state["sig"] = sig
            held = kept.take(serial)
            _draw(view, held)
        _show_note()

    async def _confirm(title, lines, warn=None):
        with ui.dialog() as dlg, ui.card():
            ui.label(title).classes("text-base font-bold")
            for line in lines:
                ui.label(line).classes("text-sm font-mono")
            if warn:
                ui.label(warn).classes("text-sm font-bold text-red-600")
            with ui.row().classes("justify-end w-full"):
                ui.button("Cancel", on_click=lambda: dlg.submit(False)).props("flat")
                ui.button("Apply", on_click=lambda: dlg.submit(True)).props("color=primary")
        return await dlg

    async def _send(host, params):
        try:
            ack = await host.cmd("set_ports", dict(params, by=by), timeout=8.0)
        except Exception as e:
            ui.notify(f"set_ports on {host.serial}: {e}", type="negative", close_button=True)
            return
        res = (getattr(ack, "result", None) or {}) if ack is not None else {}
        if ack is None or ack.status != proto.CmdStatus.OK:
            ui.notify(res.get("err", "set_ports failed"), type="negative",
                      close_button=True, timeout=12000)
            return
        _FULL[host.serial] = (res.get("ports_v"), res.get("ports_full"))
        kept.drop(host.serial)
        state["dirty"] = False
        state["sig"] = None             # draw the read-back on the next tick
        ui.notify(f"port lockout written to {host.serial}", type="positive")
        if res.get("ports_conflict"):
            ui.notify(f"The radio is open on a jack this locks, so it now refuses it. "
                      f"Re-init on an allowed jack. {res['ports_conflict']}",
                      type="warning", close_button=True, timeout=15000)

    def _current_pm(host):
        v = port_view(host, cfg_of(host))
        return v["pm"] if v else None

    async def _apply(_e=None):
        host, _v = _view()
        if host is None or not rows:
            return
        new = _edited()
        try:
            _ports.parse(new)
        except ValueError as e:
            ui.notify(str(e), type="negative", close_button=True)
            return
        old = _current_pm(host)
        lines = change_lines(old, new) or ["no change"]
        opened = _ports.locks_gained(old, new, "tx") + _ports.locks_gained(old, new, "rx")
        warn = (f"This opens up {', '.join(sorted(set(opened)))}. Check what is cabled to it first."
                if opened else None)
        if await _confirm(f"Change the lockout on {host.serial} "
                          f"({_ports.kind_text(state['kind'])})?", lines, warn):
            await _send(host, {"ports": new})

    async def _clear(_e=None):
        host, _v = _view()
        if host is None:
            return
        if await _confirm(f"Remove the lockout on {host.serial}?",
                          ["Every jack can then do TX and RX."],
                          "Check what is cabled to each jack first."):
            await _send(host, {"clear": True})

    def _revert(_e=None):
        host, view = _view()
        if host is not None:
            kept.drop(host.serial)
        _draw(view)
        _show_note()

    apply_btn.on_click(_apply)
    clear_btn.on_click(_clear)
    revert_btn.on_click(_revert)
    ui.timer(1.0, _tick)
