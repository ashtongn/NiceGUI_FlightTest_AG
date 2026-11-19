"""
role_fpga_ui.py: GUI pieces for the radio role and the FPGA image panel,
used by the operator console and the engineering SDR tab (link_tab.py).

The role lives on the radio (dpmg_service set_role, reported in get_config),
so every GUI and every script sees the same one. The picker here only sends
set_role and follows what the radio reports.
"""
from __future__ import annotations

import time

from nicegui import ui

from dpmg.link import proto
from dpmg.radio import radio_role as _role

ROLE_COL = {"any": "text-gray-500", "monitor": "text-green-700", "jammer": "text-red-700",
            "playback": "text-purple-700", "transponder": "text-amber-700",
            "bitpipe": "text-blue-700", "dcfe": "text-slate-700"}

# set_role echoes reach get_config on the next poll; keep the picked value
# that long instead of flicking back to the old one
ROLE_HOLD_S = 3.0


def role_text(cfg):
    """(text, classes) for a radio card. Blank when the radio's service is too
    old to report a role."""
    if not cfg or "role" not in cfg:
        return "", "text-xs text-gray-400"
    r = cfg.get("role") or _role.DEFAULT
    return f"Role: {_role.label(r)}", f"text-xs font-bold {ROLE_COL.get(r, 'text-gray-600')}"


def fpga_text(cfg):
    """(text, classes) for a radio card, or blank when the radio has no FPGA
    control or reports nothing yet."""
    f = (cfg or {}).get("fpga") or {}
    if not f:
        return "", "text-xs text-gray-400"
    if f.get("job") == "running":
        return f"FPGA loading: {f.get('step', '')}", "text-xs font-bold text-amber-700"
    img = f.get("img")
    if not img:
        return "", "text-xs text-gray-400"
    ver = f" v{f['ver']}" if f.get("ver") else ""
    return f"FPGA {img}{ver}", "text-xs font-mono text-gray-600"


def caps_stale(caps, cfg, uptime_s=None) -> bool:
    """An X410's caps follow its FPGA image; caps built for another image than
    the radio now reports (get_config fpga.img) are out of date. So are caps
    fetched before the radio's service last restarted (its uptime is now below
    the uptime stored with the caps as _uptime_s): a service restarted with
    other settings can report other caps, e.g. the dCFE B200's TX gain limit."""
    t = (caps or {}).get("_uptime_s")
    if uptime_s is not None and t is not None and uptime_s < t:
        return True
    img = ((cfg or {}).get("fpga") or {}).get("img")
    have = (caps or {}).get("fpga_image")
    return bool(img and "fpga_image" in (caps or {}) and have != img)


async def set_radio_role(host, role, by):
    """(ok, message)."""
    try:
        ack = await host.cmd("set_role", {"role": role, "by": by}, timeout=6.0)
    except Exception as e:
        return False, f"set role: {e}"
    if ack is None or ack.status != proto.CmdStatus.OK:
        return False, ((ack.result or {}) if ack else {}).get("err", "set role failed")
    return True, f"role set to {_role.label(role)}"


class RolePicker:
    """Radio role dropdown for the focused radio. sync(cfg) follows the radio;
    on_role(role, by_user) runs when the shown role changes (by_user True
    when the operator picked it here)."""

    def __init__(self, get_host, by, on_role=None, classes="w-56"):
        self.get_host, self.by, self.on_role = get_host, by, on_role
        self.cur = None
        self._quiet = False
        self._hold = (None, 0.0)
        self.sel = ui.select(_role.choices(), value=None, label="Radio role") \
            .props("dense outlined").classes(classes) \
            .tooltip("What this radio is acting as. The panels follow the role and the "
                     "radio refuses commands the role does not use. Stored on the radio. "
                     "Not changed while TX is running.")
        self.sel.on_value_change(self._changed)

    def _show(self, r):
        self._quiet = True
        try:
            self.sel.value = r
        finally:
            self._quiet = False

    def sync(self, cfg):
        has = bool(cfg) and "role" in cfg
        if has:
            self.sel.enable()
        else:
            self.sel.disable()
        r = (cfg or {}).get("role") if has else None
        held, until = self._hold
        if held is not None and time.monotonic() < until and r != held:
            return
        if r != self.cur or self.sel.value != r:
            changed = r != self.cur
            self.cur = r
            self._show(r)
            if changed and self.on_role:
                self.on_role(r or _role.DEFAULT, False)

    async def _changed(self, e):
        if self._quiet or e.value is None or e.value == self.cur:
            return
        host = self.get_host()
        if host is None:
            self._show(self.cur)
            return
        ok, msg = await set_radio_role(host, e.value, self.by)
        if ok:
            self.cur = e.value
            self._hold = (e.value, time.monotonic() + ROLE_HOLD_S)
            ui.notify(msg, type="positive")
            if self.on_role:
                self.on_role(e.value, True)
        else:
            ui.notify(msg, type="warning")
            self._show(self.cur)


# FPGA image panel

_FAM_TXT = {"x4xx": "Ettus X4xx", "b2xx": "Ettus B200 family", "epiq": "Epiq NV100 / X40"}
_HOW_TXT = {"flash": "written to the radio", "on_open": "loaded by UHD at each open",
            "read_only": "read only here"}


def fpga_rows(hosts, cfg_of, name_of):
    rows = []
    for serial in sorted(hosts):
        cfg = cfg_of(hosts[serial]) or {}
        f = cfg.get("fpga")
        if not f:
            continue
        rows.append({"serial": serial, "radio": name_of(serial),
                     "family": _FAM_TXT.get(f.get("fam"), f.get("fam") or ""),
                     "image": f.get("img") or "",
                     "version": f.get("ver") or "",
                     "load": ("loading: " + f.get("step", "")) if f.get("job") == "running"
                     else (f.get("job") or "")})
    return rows


def confirm_lines(name, serial, st, new):
    """The confirm dialog text: radio, current image, new image, warning."""
    fam = (st or {}).get("family")
    cur = (st or {}).get("image") or "unknown"
    ver = (st or {}).get("version")
    lines = [f"Radio: {name}" + (f" ({serial})" if name != serial else ""),
             f"Current image: {cur}" + (f" v{ver}" if ver else ""),
             f"New image: {new}"]
    if fam == "x4xx":
        lines.append("The radio drops off the network while the image loads (about a "
                     "minute on the bench X410). DPMG stops RX and closes the radio first, "
                     "then reads the image back and probes the radio. The rates and "
                     "channels offered follow the new image. Start the radio again "
                     "afterwards.")
    else:
        lines.append("The radio closes and reopens with this file, so RX stops for a few "
                     "seconds. DPMG reads the result back afterwards.")
    lines.append("Refused while TX is running.")
    return lines


def build_fpga_panel(get_host, get_hosts, cfg_of, name_of=lambda s: s):
    """Engineering view panel: the image every radio runs, and loading another
    one on the selected radio (X410, B200 family). G40/X40 are read only."""
    state = {"serial": None, "st": None, "t": 0.0, "busy": False, "job_seen": None}
    with ui.expansion("FPGA image", icon="memory").classes("w-full border rounded"):
        ui.label("Which FPGA image each radio runs. An X410 image is written to the radio "
                 "with uhd_image_loader; a B200 image is the file UHD opens it with; G40 and "
                 "X40 cards are read only here.").classes("text-xs text-gray-600")
        table = ui.table(columns=[
            {"name": "radio", "label": "radio", "field": "radio", "align": "left"},
            {"name": "family", "label": "type", "field": "family", "align": "left"},
            {"name": "image", "label": "image", "field": "image", "align": "left"},
            {"name": "version", "label": "version", "field": "version", "align": "left"},
            {"name": "load", "label": "last load", "field": "load", "align": "left"}],
            rows=[], row_key="serial").props("dense flat").classes("w-full")
        sel_lbl = ui.label("no radio selected").classes("text-sm font-bold")
        cur_lbl = ui.label("").classes("text-xs font-mono")
        with ui.row().classes("items-center gap-3"):
            img_sel = ui.select([], label="Image to load").props("dense outlined").classes("w-64")
            load_btn = ui.button("Load image", icon="upload").props("dense color=negative")
            probe_btn = ui.button("Read back", icon="refresh").props("dense outline no-caps") \
                .tooltip("ask the radio which image it runs (an X410 that is not started "
                         "is probed with uhd_usrp_probe, about 20 s)")
        job_lbl = ui.label("").classes("text-sm")
        log_lbl = ui.label("").classes("text-xs font-mono whitespace-pre-wrap text-gray-600")

    async def _status(probe=False):
        host = get_host()
        if host is None or state["busy"]:
            return
        state["busy"] = True
        try:
            ack = await host.cmd("fpga_status", {"probe": probe, "refresh": probe},
                                 timeout=150.0 if probe else 20.0)
        except Exception as e:
            cur_lbl.set_text(f"fpga_status: {e}")
            return
        finally:
            state["busy"] = False
        if ack is None or ack.status != proto.CmdStatus.OK:
            cur_lbl.set_text(((ack.result or {}) if ack else {}).get("err", "fpga_status failed"))
            return
        state["st"], state["t"] = ack.result or {}, time.monotonic()
        _draw()

    def _draw():
        st = state["st"] or {}
        fam = st.get("family")
        if not fam:
            cur_lbl.set_text(st.get("note", "no FPGA image control for this radio"))
            img_sel.set_visibility(False); load_btn.set_visibility(False)
            probe_btn.set_visibility(False)
            job_lbl.set_text(""); log_lbl.set_text("")
            return
        parts = [_FAM_TXT.get(fam, fam)]
        if st.get("image") or fam != "epiq":       # an NV100 has no image name to show
            parts.append(f"image {st.get('image') or 'unknown'}")
        if st.get("version"):
            parts.append(f"version {st['version']}")
        if st.get("git_hash"):
            parts.append(f"hash {st['git_hash']}")
        for c, d in sorted((st.get("cards") or {}).items()):
            card = [f"card {c}: v{d.get('version', '?')}"]
            if d.get("git_hash"):
                card.append(f"hash {d['git_hash']}")
            if d.get("build_date"):
                card.append(f"built {d['build_date']}")
            parts.append(" ".join(card))
        if st.get("lib"):
            parts.append(f"libsidekiq {st['lib']}")
        how = _HOW_TXT.get(st.get("how"), "")
        if how and not (st.get("note") or "").startswith(how):
            parts.append(how)
        if st.get("note"):
            parts.append(st["note"])
        cur_lbl.set_text(" | ".join(p for p in parts if p))
        loadable = bool(st.get("loadable"))
        img_sel.set_visibility(loadable); load_btn.set_visibility(loadable)
        probe_btn.set_visibility(fam in ("x4xx", "b2xx", "epiq"))
        ch = list(st.get("choices") or [])
        if img_sel.options != ch:
            img_sel.set_options(ch, value=img_sel.value if img_sel.value in ch else None)
        j = st.get("job") or {}
        running = j.get("state") == "running"
        tx = bool(st.get("tx_active"))
        if running or tx or not loadable:
            load_btn.disable()
        else:
            load_btn.enable()
        load_btn.tooltip("stop TX first" if tx else "")
        if j:
            if j.get("state") == "done":
                res = j.get("result") or {}
                job_lbl.set_text(f"Last load {j.get('prev') or '?'} to {j.get('image')}: verified, "
                                 f"radio reports {res.get('image')}"
                                 + (f" v{res['version']}" if res.get("version") else "")
                                 + f", probe {res.get('probe')}")
                job_lbl.classes(replace="text-sm font-bold text-green-700")
            elif j.get("state") == "failed":
                job_lbl.set_text(f"Last load to {j.get('image')} FAILED: {j.get('err')}")
                job_lbl.classes(replace="text-sm font-bold text-red-600")
            else:
                job_lbl.set_text(f"Loading {j.get('image')}: {j.get('step')}")
                job_lbl.classes(replace="text-sm font-bold text-amber-700")
            log_lbl.set_text("\n".join(j.get("log") or []))
        else:
            job_lbl.set_text(""); log_lbl.set_text("")

    async def _load():
        host, st, new = get_host(), state["st"] or {}, img_sel.value
        if host is None or not new:
            ui.notify("pick an image to load", type="warning"); return
        tx_on = bool(host.status is not None and host.status.flags & proto.Status.FLAG_TX_ACTIVE)
        if tx_on or st.get("tx_active"):
            ui.notify("stop TX before loading an FPGA image", type="warning"); return
        if new == st.get("image"):
            ui.notify(f"the radio already runs {new}", type="info")
        name = name_of(host.serial)
        with ui.dialog() as dlg, ui.card().classes("max-w-xl"):
            ui.label(f"Load FPGA image {new}?").classes("text-lg font-bold")
            for i, line in enumerate(confirm_lines(name, host.serial, st, new)):
                ui.label(line).classes("text-sm" + (" text-red-700 font-bold" if i == 3 else ""))
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=lambda: dlg.submit(False)).props("flat")
                ui.button(f"Load {new}", on_click=lambda: dlg.submit(True)).props("color=negative")
        ok = await dlg
        dlg.delete()
        if not ok:
            return
        try:
            ack = await host.cmd("fpga_load", {"image": new, "confirm": new}, timeout=15.0)
        except Exception as e:
            ui.notify(f"fpga_load: {e}", type="negative"); return
        if ack is None or ack.status != proto.CmdStatus.OK:
            ui.notify(((ack.result or {}) if ack else {}).get("err", "fpga_load failed"),
                      type="warning")
        else:
            ui.notify(f"loading {new} on {name}", type="warning")
        await _status()

    load_btn.on_click(_load)
    probe_btn.on_click(lambda: _status(probe=True))

    async def _tick():
        hosts = get_hosts()
        rows = fpga_rows(hosts, cfg_of, name_of)
        if rows != table.rows:
            table.rows = rows
            table.update()
        host = get_host()
        serial = host.serial if host is not None else None
        if serial != state["serial"]:
            state.update(serial=serial, st=None, t=0.0)
            sel_lbl.set_text(f"Selected radio: {name_of(serial)}" if serial else "no radio selected")
            cur_lbl.set_text(""); job_lbl.set_text(""); log_lbl.set_text("")
        if host is None:
            return
        brief = (cfg_of(host) or {}).get("fpga") or {}
        running = brief.get("job") == "running" or \
            ((state["st"] or {}).get("job") or {}).get("state") == "running"
        age = time.monotonic() - state["t"]
        if state["st"] is None or (running and age > 2.0) or age > 15.0 \
                or brief.get("job") != state["job_seen"]:
            state["job_seen"] = brief.get("job")
            await _status()

    ui.timer(2.0, _tick)
