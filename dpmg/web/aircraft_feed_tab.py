"""Aircraft feed: the engineering panel for the platform position feed.

The real flight position arrives over multicast in the same two forms the
position simulator sends (JSON or NMEA). This panel is where that feed is set
up and checked before a flight: group, port, interface and format, saved for
the whole GUI; the last datagrams as received with their arrival times; the
parsed fix with the fix rate, every field with the sentence it came from, and
a line of which NMEA sentences arrive and how often (so a missing VTG, and
with it the heading, is obvious); the feed's own time against this console's
clock; a recorder for the datagrams; and a test sender that plays a path or a
recording back onto the group. There is one feed per aircraft on the saved
list (add, rename and remove here); the picker says which one the fields,
the packets, the recorder and the sender work on. The listeners are the
shared ones (link_tab.FEEDS), so the operator console, the mission planner
and this panel always agree on what is being listened to. Nothing here
touches a radio.
"""

from __future__ import annotations

import glob
import os
import time

from nicegui import ui

from dpmg.link import feed_sender as fs
from dpmg.link import feed_settings
from dpmg.link.aircraft_feeds import PanelFields
from dpmg.link.acft_pos_multicast import (BURST_KINDS, FORMATS, fix_rows, local_ipv4_addresses,
                                          sentence_rates_text, summary_text)
from dpmg.web.link_tab import FEEDS

RECORDS_DIR = (os.environ.get("DPMG_FEED_RECORDS")
               or os.path.join(os.path.expanduser("~"), ".dpmg", "feed_records"))

# the test sender and the recorder outlive a page, like the listeners do
_SENDER = {"path": None, "replay": None, "recorder": None, "rec_lst": None}


def _hms(t_wall, ms=True) -> str:
    if not t_wall:
        return "--:--:--"
    base = time.strftime("%H:%M:%S", time.gmtime(t_wall))
    return base + (f".{int((t_wall % 1) * 1000):03d}" if ms else "") + "Z"


def _recordings() -> list:
    return sorted(glob.glob(os.path.join(RECORDS_DIR, "*.jsonl")))


def build_aircraft_feed_tab(is_active=lambda: True):
    st0 = feed_settings.load()
    root = ui.column().classes("w-full gap-2")
    with root:
        ui.label("Aircraft position feed").classes("text-base font-bold")
        ui.label("The platform position arrives on a UDP multicast group as JSON or NMEA "
                 "datagrams. Set where to listen, press Listen, and read the packets as "
                 "they come in. Save keeps the values for the operator header, the mission "
                 "planner and this panel. The planner's location triggers run on this same "
                 "feed.").classes("text-xs text-gray-600")

        # the aircraft list, and the picked aircraft's listener settings
        with ui.card().classes("w-full gap-1"):
            with ui.row().classes("items-center gap-2 flex-wrap"):
                ui.label("Aircraft").classes("text-sm font-bold")
                ac_sel = ui.select(FEEDS.names(), value=FEEDS.first_name(), label="aircraft") \
                    .props("dense outlined").classes("w-48") \
                    .tooltip("the feed the fields, the packets, the recorder and the test "
                             "sender on this page work on; the first on the list is the one "
                             "a plan radio follows unless its row names another")
                ui.button("Add", icon="add", on_click=lambda: _add_dialog()).props("dense outline no-caps") \
                    .tooltip("another aircraft with its own group, port, interface and format")
                ui.button("Rename", icon="edit", on_click=lambda: _rename_dialog()).props("dense outline no-caps")
                ui.button("Remove", icon="delete", on_click=lambda: _remove_dialog()) \
                    .props("dense outline no-caps color=negative")
                list_lbl = ui.label("").classes("text-xs text-gray-600")
            share_lbl = ui.label("").classes("text-xs text-amber-700")
            ui.label("Listener").classes("text-sm font-bold")
            with ui.row().classes("items-center gap-2 flex-wrap"):
                grp_in = ui.input("group", value="").props("dense outlined").classes("w-36")
                prt_in = ui.number("port", value=5005, format="%d", min=1, max=65535) \
                    .props("dense outlined").classes("w-24")
                ifaces = ["0.0.0.0"] + local_ipv4_addresses()
                ifc_in = ui.select(ifaces, value="0.0.0.0", label="interface",
                                   with_input=True, new_value_mode="add-unique") \
                    .props("dense outlined").classes("w-44") \
                    .tooltip("this console's address on the adapter the feed arrives on; "
                             "0.0.0.0 joins on the default route; 127.0.0.1 for a sender "
                             "on this console")
                fmt_in = ui.select(list(FORMATS), value="auto", label="format") \
                    .props("dense outlined").classes("w-28")
                # above the field, so it does not cover the open list
                with fmt_in:
                    ui.tooltip("auto sniffs each datagram; json or nmea accepts only that form. "
                               "Two aircraft may share a group and port and differ in format only") \
                        .props('anchor="top middle" self="bottom middle"')
                ui.button("Save", icon="save", on_click=lambda: _save()).props("dense outline no-caps") \
                    .tooltip("keep these values for this aircraft, for every view")
                listen_btn = ui.button("Listen", icon="hearing", on_click=lambda: _listen()) \
                    .props("dense color=positive no-caps")
                stop_btn = ui.button("Stop", icon="stop", on_click=lambda: _stop()) \
                    .props("dense color=negative outline no-caps")
                lst_lbl = ui.label("").classes("text-xs font-mono")
            saved_lbl = ui.label("").classes("text-xs text-gray-500")

        def L():
            """The picked aircraft's listener."""
            return FEEDS.get(ac_sel.value) or FEEDS.primary

        def _entry():
            return dict(FEEDS.entry(ac_sel.value) or FEEDS.entry(FEEDS.first_name()) or {})

        # what the fields last showed: Save and Listen write back only the
        # fields changed here, and the fields follow a save made on another
        # page (the planner's feed row, the SDR tab panel)
        shown = PanelFields(FEEDS)

        def _field_values():
            return {"group": grp_in.value, "port": prt_in.value,
                    "iface": ifc_in.value, "format": fmt_in.value}

        def _fill(keep_edits=False):
            e = shown.fill(ac_sel.value, _field_values() if keep_edits else None)
            grp_in.set_value(e.get("group", ""))
            prt_in.set_value(e.get("port", 5005))
            if e.get("iface") and e["iface"] not in ifc_in.options:
                ifc_in.set_options(list(ifc_in.options) + [e["iface"]])
            ifc_in.set_value(e.get("iface", "0.0.0.0"))
            fmt_in.set_value(e.get("format", "auto"))
            share_lbl.set_text(FEEDS.socket_note(ac_sel.value))
            shared = FEEDS.shared_sockets()
            n = len(FEEDS)
            list_lbl.set_text(f"{n} aircraft on the list"
                              + (f"; {len(shared)} group and port shared" if shared else ""))
            saved_lbl.set_text(f"saved: {feed_settings.describe()} in {feed_settings.settings_path()}")

        def _sync_list():
            FEEDS.follow_file()
            if list(ac_sel.options) != FEEDS.names():
                ac_sel.set_options(FEEDS.names(), value=FEEDS.resolve(ac_sel.value))
                _fill()
            elif shown.stale():
                _fill(keep_edits=True)

        _fill()
        ac_sel.on_value_change(lambda e: _fill())

        def _pick_format(_e=None):
            # a picked format is saved at once and a running listener takes
            # it on its next datagram; a fill from code is no edit
            try:
                res = shown.apply(_field_values())
            except OSError as ex:
                ui.notify(f"format not saved: {ex}", type="negative")
                return
            if res is not None and res[2]:
                ui.notify(f"{res[0]}: format {res[1]['format']} saved"
                          + (", the listener takes it at once" if FEEDS.running(res[0]) else ""),
                          type="positive")
                _fill(keep_edits=True)
        fmt_in.on_value_change(_pick_format)

        def _values():
            return {"name": ac_sel.value,
                    "group": (grp_in.value or "").strip(),
                    "port": int(prt_in.value or 5005),
                    "iface": (ifc_in.value or "0.0.0.0").strip(),
                    "format": fmt_in.value or "auto",
                    "window_s": L().window_s if L() is not None else 1.5}

        def _save(quiet=False):
            # only the fields changed on this page, on the settings as saved now
            try:
                name, _e, changed = FEEDS.update(ac_sel.value, shown.edits(_values()))
            except OSError as e:
                ui.notify(f"settings not saved: {e}", type="negative")
                _fill()
                return False
            _fill()
            if not quiet:
                ui.notify(f"feed settings saved for {name}" if changed
                          else f"{name}: nothing changed, the saved settings stand",
                          type="positive" if changed else "info")
            return changed

        def _listen():
            # a running listener whose group, port or interface changed has
            # already started again on the new ones
            _save(quiet=True)
            try:
                if not FEEDS.running(ac_sel.value):
                    FEEDS.start(ac_sel.value)
                lst = L()
                ui.notify(f"{ac_sel.value}: listening {lst.group}:{lst.port} on {lst.iface_ip} ({lst.fmt})",
                          type="positive")
            except (ValueError, OSError) as e:
                ui.notify(f"listen failed: {e}", type="negative")

        def _stop():
            FEEDS.stop(ac_sel.value)
            ui.notify(f"{ac_sel.value}: feed listener stopped")

        async def _add_dialog():
            e0 = _entry()
            with ui.dialog() as dlg, ui.card().classes("gap-1 w-[26rem]"):
                ui.label("Add an aircraft").classes("text-base font-bold")
                ui.label("Its own position feed: a name for the console, the map and the "
                         "plan, and where its datagrams arrive.").classes("text-xs text-gray-600")
                nm = ui.input("name", placeholder="N1234").props("dense outlined").classes("w-full")
                gp = ui.input("group", value=e0.get("group", "239.2.1.1")).props("dense outlined").classes("w-full")
                pt = ui.number("port", value=int(e0.get("port", 5005)) + 1, format="%d", min=1, max=65535) \
                    .props("dense outlined").classes("w-full")
                ifc = ui.input("interface", value=e0.get("iface", "0.0.0.0")).props("dense outlined").classes("w-full")
                fm = ui.select(list(FORMATS), value=e0.get("format", "auto"), label="format") \
                    .props("dense outlined").classes("w-full")
                with ui.row().classes("justify-end w-full"):
                    ui.button("Cancel", on_click=lambda: dlg.submit(False)).props("flat")
                    ui.button("Add", on_click=lambda: dlg.submit(True)).props("color=primary")
            if not await dlg:
                return
            entry = {"name": (nm.value or "").strip(), "group": (gp.value or "").strip(),
                     "port": int(pt.value or 5005), "iface": (ifc.value or "0.0.0.0").strip(),
                     "format": fm.value or "auto"}
            if FEEDS.get(entry["name"]) is not None:
                ui.notify(f"there is already an aircraft named {entry['name']}", type="warning")
                return
            st = feed_settings.set_aircraft(feed_settings.load(), entry)
            try:
                feed_settings.save(st)
            except OSError as e:
                ui.notify(f"settings not saved: {e}", type="negative")
                return
            FEEDS.reload(st)
            new_name = st["aircraft"][-1]["name"]
            ac_sel.set_options(FEEDS.names(), value=new_name)
            _fill()
            ui.notify(f"aircraft {new_name} added", type="positive")

        async def _rename_dialog():
            old = ac_sel.value
            with ui.dialog() as dlg, ui.card().classes("gap-1 w-[26rem]"):
                ui.label(f"Rename {old}").classes("text-base font-bold")
                ui.label("The console, the map and the plan rows use the new name from now on; "
                         "a plan radio bound to the old name follows the first aircraft until "
                         "its row is set again.").classes("text-xs text-gray-600")
                nm = ui.input("new name", value=old).props("dense outlined").classes("w-full")
                with ui.row().classes("justify-end w-full"):
                    ui.button("Cancel", on_click=lambda: dlg.submit(False)).props("flat")
                    ui.button("Rename", on_click=lambda: dlg.submit(True)).props("color=primary")
            if not await dlg:
                return
            new = " ".join(str(nm.value or "").split())[:feed_settings.NAME_MAX]
            if not new or new == old:
                return
            if FEEDS.get(new) is not None:
                ui.notify(f"there is already an aircraft named {new}", type="warning")
                return
            st = feed_settings.set_aircraft(feed_settings.load(), dict(_entry(), name=new), old_name=old)
            try:
                feed_settings.save(st)
            except OSError as e:
                ui.notify(f"settings not saved: {e}", type="negative")
                return
            FEEDS.rename(old, new)
            FEEDS.reload(st)
            ac_sel.set_options(FEEDS.names(), value=new)
            _fill()
            ui.notify(f"{old} is now {new}", type="positive")

        async def _remove_dialog():
            name = ac_sel.value
            if len(FEEDS) < 2:
                ui.notify("the last aircraft stays: there is always one feed to listen to", type="warning")
                return
            with ui.dialog() as dlg, ui.card().classes("gap-1 w-[26rem]"):
                ui.label(f"Remove {name}?").classes("text-base font-bold")
                ui.label("Its listener stops. A plan radio bound to it follows the first "
                         "aircraft from then on.").classes("text-xs text-gray-600")
                with ui.row().classes("justify-end w-full"):
                    ui.button("Cancel", on_click=lambda: dlg.submit(False)).props("flat")
                    ui.button("Remove", on_click=lambda: dlg.submit(True)).props("color=negative")
            if not await dlg:
                return
            FEEDS.stop(name)
            st = feed_settings.remove_aircraft(feed_settings.load(), name)
            try:
                feed_settings.save(st)
            except OSError as e:
                ui.notify(f"settings not saved: {e}", type="negative")
                return
            FEEDS.reload(st)
            ac_sel.set_options(FEEDS.names(), value=FEEDS.first_name())
            _fill()
            ui.notify(f"aircraft {name} removed", type="warning")

        # the parsed fix and the feed time
        with ui.row().classes("w-full gap-2 items-stretch flex-wrap"):
            with ui.card().classes("gap-1 grow"):
                ui.label("Parsed fix").classes("text-sm font-bold")
                fix_lbl = ui.label("no fix").classes("text-sm font-mono whitespace-pre")
                cnt_lbl = ui.label("").classes("text-xs font-mono text-gray-600")
                ui.label("Sentences arriving, over the last 10 s").classes("text-xs font-bold mt-1")
                sent_lbl = ui.label("none yet").classes("text-xs font-mono whitespace-pre-wrap") \
                    .tooltip("each NMEA sentence kind the feed sends and its rate; none "
                             "means never seen, last N s ago means it stopped. No VTG and "
                             "no RMC course means no heading; no GSA means no fix mode or "
                             "DOP; no GSV means no satellites in view")
                fix_cols = [{"name": "field", "label": "field", "field": "field", "align": "left"},
                            {"name": "value", "label": "value", "field": "value", "align": "left",
                             "classes": "font-mono"},
                            {"name": "source", "label": "from", "field": "source", "align": "left",
                             "classes": "font-mono"},
                            {"name": "age", "label": "age", "field": "age", "align": "right",
                             "classes": "font-mono"}]
                fix_table = ui.table(columns=fix_cols, rows=fix_rows(None), row_key="field") \
                    .classes("w-full text-xs").props("dense flat bordered hide-bottom") \
                    .tooltip("every field of the combined fix, the sentence (or json) that "
                             "carried it, and the seconds since that sentence arrived")
            with ui.card().classes("gap-1 grow"):
                ui.label("Feed time against this console").classes("text-sm font-bold")
                ui.label("The time stamp inside each datagram minus this console's clock at "
                         "arrival. Steps are keyed from the console clock, so a large offset "
                         "means one of the two clocks needs a look before a UTC plan.") \
                    .classes("text-xs text-gray-600")
                clk_lbl = ui.label("").classes("text-sm font-mono whitespace-pre")
                ui.label("Combine window").classes("text-xs font-bold mt-2")
                ui.label("A feed sends a burst of sentences each second, sometimes over "
                         "several datagrams. The fix keeps the newest value of each field "
                         "from any sentence this many seconds old or less. A receiver that "
                         "sends GSA and GSV every 5 s needs a window of 5 s or more for the "
                         "fix mode, DOP and satellite count to stay in the fix; the "
                         "sentence line above shows each rate.") \
                    .classes("text-xs text-gray-600")
                win_in = ui.number("window (s)", value=L().window_s, min=0.2, max=10.0, step=0.1,
                                   format="%.1f").props("dense outlined").classes("w-28") \
                    .on_value_change(lambda e: L().set_window(float(e.value or 1.5)))

        # raw packets and the recorder
        with ui.card().classes("w-full gap-1"):
            with ui.row().classes("items-center gap-2 w-full flex-wrap"):
                pk_lbl = ui.label("Raw packets, newest first").classes("text-sm font-bold")
                n_in = ui.number("show", value=20, min=5, max=200, format="%d") \
                    .props("dense outlined").classes("w-20")
                ui.button("Clear", icon="clear_all", on_click=lambda: (L().clear_history(), _refresh())) \
                    .props("dense flat no-caps")
                rec_btn = ui.button("Record to file", icon="fiber_manual_record",
                                    on_click=lambda: _record()).props("dense outline no-caps") \
                    .tooltip(f"write every datagram with its arrival time to {RECORDS_DIR}")
                rec_lbl = ui.label("").classes("text-xs font-mono")
            cols = [{"name": "t", "label": "arrival (UTC)", "field": "t", "align": "left"},
                    {"name": "from", "label": "from", "field": "from", "align": "left"},
                    {"name": "n", "label": "bytes", "field": "n", "align": "right"},
                    {"name": "res", "label": "parsed", "field": "res", "align": "left"},
                    {"name": "raw", "label": "datagram", "field": "raw", "align": "left",
                     "classes": "font-mono", "style": "white-space: pre-wrap; max-width: 60rem"}]
            table = ui.table(columns=cols, rows=[], row_key="k").classes("w-full text-xs") \
                .props("dense flat bordered")

        def _record():
            rec = _SENDER.get("recorder")
            if rec is not None:
                if _SENDER.get("rec_lst") is not None:
                    _SENDER["rec_lst"].on_packet = None
                _SENDER["rec_lst"] = None
                rec.close()
                _SENDER["recorder"] = None
                ui.notify(f"recording closed: {rec.path} ({rec.count} datagram(s))", type="positive")
                return
            path = os.path.join(RECORDS_DIR, time.strftime("feed_%Y%m%d_%H%M%SZ.jsonl", time.gmtime()))
            try:
                rec = fs.PacketRecorder(path)
            except OSError as e:
                ui.notify(f"cannot record: {e}", type="negative")
                return
            _SENDER["recorder"] = rec
            _SENDER["rec_lst"] = L()
            L().on_packet = rec.write
            ui.notify(f"recording {ac_sel.value} to {path}", type="positive")

        # the test sender
        with ui.card().classes("w-full gap-1"):
            ui.label("Test sender").classes("text-sm font-bold")
            ui.label("Plays a path onto the group above as the aircraft would, or replays a "
                     "recording with its original spacing. Use interface 127.0.0.1 with the "
                     "listener on 127.0.0.1 to keep it on this console. The planner sees it "
                     "as the aircraft feed, so disarm the location triggers of any plan with "
                     "radios connected before pressing Start.").classes("text-xs text-gray-600")
            snd0 = st0["sender"]
            with ui.row().classes("items-center gap-2 flex-wrap"):
                src_sel = ui.select({"plan": "the autosaved plan's flight path",
                                     "points": "lat,lon points typed below",
                                     "geojson": "a GeoJSON file on this console",
                                     "recording": "a recording from this panel"},
                                    value="plan", label="path from").props("dense outlined").classes("w-64")
                pts_in = ui.input("lat,lon points", value="33.40,-112.07 33.45,-112.07") \
                    .props("dense outlined").classes("w-72")
                gj_in = ui.input("GeoJSON file", value="").props("dense outlined").classes("w-72")
                rec_sel = ui.select(_recordings(), value=None, label="recording") \
                    .props("dense outlined").classes("w-96")
                ui.button(icon="refresh", on_click=lambda: rec_sel.set_options(_recordings())) \
                    .props("dense flat").tooltip("re-read the recordings folder")
            with ui.row().classes("items-center gap-2 flex-wrap"):
                sfmt_in = ui.select(["json", "nmea"], value=snd0["format"], label="format") \
                    .props("dense outlined").classes("w-24")
                rate_in = ui.number("rate (Hz)", value=snd0["rate_hz"], min=0.1, max=50, format="%g") \
                    .props("dense outlined").classes("w-24")
                spd_in = ui.number("speed (m/s)", value=snd0["speed_mps"], min=0.1, format="%g") \
                    .props("dense outlined").classes("w-28")
                alt_in = ui.number("altitude (m)", value=snd0["alt_m"], format="%g") \
                    .props("dense outlined").classes("w-28")
                toff_in = ui.number("time offset (s)", value=0.0, format="%g") \
                    .props("dense outlined").classes("w-32") \
                    .tooltip("added to the time stamp in each datagram, to try a feed whose "
                             "clock is off against this console")
                sifc_in = ui.select(ifaces, value=snd0["iface"] if snd0["iface"] in ifaces else "127.0.0.1",
                                    label="send on", with_input=True, new_value_mode="add-unique") \
                    .props("dense outlined").classes("w-44")
                loop_cb = ui.checkbox("loop", value=snd0["loop"]).props("dense")
            with ui.row().classes("items-center gap-2 flex-wrap"):
                ui.label("NMEA burst").classes("text-xs font-bold")
                hdg_cb = ui.checkbox("send heading", value=True).props("dense") \
                    .tooltip("off: no course at all (JSON without the key, NMEA with blank "
                             "RMC and VTG courses), to show the GUI without a heading")
                drop_sel = ui.select(list(BURST_KINDS), value=[], label="drop sentences", multiple=True,
                                     clearable=True).props("dense outlined use-chips").classes("w-72") \
                    .tooltip("leave these out of each NMEA burst; drop VTG to show the "
                             "heading coming from RMC alone, drop RMC and VTG to show none")
                aux_in = ui.number("GSA/GSV every", value=1, min=1, max=60, format="%d") \
                    .props("dense outlined").classes("w-32") \
                    .tooltip("send GSA and GSV on every nth burst only, as some receivers do "
                             "(5 at 1 Hz shows GSA 0.2 Hz on the sentence line)")
                split_cb = ui.checkbox("one sentence per datagram", value=False).props("dense") \
                    .tooltip("send each sentence of the burst in its own datagram, to try the "
                             "combine window across datagrams")
                ui.button("Start", icon="play_arrow", on_click=lambda: _send_start()) \
                    .props("dense color=primary no-caps")
                ui.button("Stop sender", icon="stop", on_click=lambda: _send_stop()) \
                    .props("dense outline no-caps")
                snd_lbl = ui.label("").classes("text-xs font-mono")

            def _show_src():
                v = src_sel.value
                pts_in.set_visibility(v == "points")
                gj_in.set_visibility(v == "geojson")
                rec_sel.set_visibility(v == "recording")
                for w in (sfmt_in, rate_in, spd_in, alt_in, toff_in, hdg_cb, drop_sel, aux_in, split_cb):
                    w.set_visibility(v != "recording")
            src_sel.on_value_change(lambda e: _show_src())
            _show_src()

        def _path_for(src):
            if src == "points":
                return fs.parse_points(pts_in.value)
            if src == "geojson":
                from dpmg.mission import map_element as me
                with open(gj_in.value, "r", encoding="utf-8") as f:
                    return me.path_from_geojson(f.read())
            from dpmg.mission import map_element as me
            from dpmg.mission import mission_file as mf
            from dpmg.mission.mission_tab import AUTOSAVE_PATH
            return me.flight_path(mf.load(AUTOSAVE_PATH))

        def _send_start():
            _send_stop()
            v = _values()
            st = feed_settings.load()
            st["sender"] = {"rate_hz": float(rate_in.value or 1.0), "speed_mps": float(spd_in.value or 60.0),
                            "alt_m": float(alt_in.value or 0.0), "loop": bool(loop_cb.value),
                            "iface": (sifc_in.value or "127.0.0.1").strip(), "format": sfmt_in.value}
            try:
                feed_settings.save(st)
            except OSError:
                pass
            try:
                if src_sel.value == "recording":
                    if not rec_sel.value:
                        ui.notify("pick a recording first", type="warning")
                        return
                    rp = fs.RecordingReplayer(v["group"], v["port"], st["sender"]["iface"],
                                              loop=bool(loop_cb.value))
                    n = rp.load(rec_sel.value)
                    if not rp.start():
                        ui.notify(rp.error, type="warning")
                        return
                    _SENDER["replay"] = rp
                    ui.notify(f"replaying {n} datagram(s) from {os.path.basename(rec_sel.value)}",
                              type="positive")
                    return
                path = _path_for(src_sel.value)
                ps = fs.PathSender(v["group"], v["port"], st["sender"]["iface"], fmt=sfmt_in.value,
                                   rate_hz=st["sender"]["rate_hz"], speed_mps=st["sender"]["speed_mps"],
                                   alt_m=st["sender"]["alt_m"], loop=bool(loop_cb.value),
                                   time_offset_s=float(toff_in.value or 0.0),
                                   drop=list(drop_sel.value or []), heading=bool(hdg_cb.value),
                                   aux_every=int(aux_in.value or 1), split=bool(split_cb.value))
                n = ps.set_path(path)
                if not ps.start():
                    ui.notify(ps.error, type="warning")
                    return
                _SENDER["path"] = ps
                what = sfmt_in.value if sfmt_in.value == "json" else (
                    "NMEA " + ",".join(k for k in BURST_KINDS if k not in ps.drop))
                ui.notify(f"sending a {n} point path ({ps.length_m() / 1000:.1f} km) to "
                          f"{v['group']}:{v['port']} as {what}"
                          + ("" if ps.heading else ", no heading"), type="positive")
            except (OSError, ValueError) as e:
                ui.notify(f"sender not started: {e}", type="negative")

        def _send_stop():
            for key in ("path", "replay"):
                s = _SENDER.get(key)
                if s is not None:
                    s.stop()
                    _SENDER[key] = None

        # refresh
        def _refresh():
            if not is_active():
                return
            _sync_list()
            ACFT = L()
            if ACFT.running:
                lst_lbl.set_text(f"{ac_sel.value}: listening {ACFT.group}:{ACFT.port} on {ACFT.iface_ip} [{ACFT.fmt}]"
                                 f" since {_hms(ACFT.started_at, ms=False)}")
                lst_lbl.classes(replace="text-xs font-mono text-emerald-700")
            else:
                lst_lbl.set_text("stopped" + (f" ({ACFT.last_error})" if ACFT.last_error else ""))
                lst_lbl.classes(replace="text-xs font-mono text-gray-500")
            listen_btn.set_visibility(not ACFT.running)
            stop_btn.set_visibility(ACFT.running)
            cur = ACFT.latest
            rate = ACFT.rate_hz()
            rate_txt = "--" if rate is None else f"{rate:.2f} Hz"
            if cur is None:
                fix_lbl.set_text("no fix" + (" yet" if ACFT.running else ""))
                fix_lbl.classes(replace="text-sm font-mono whitespace-pre text-gray-500")
            else:
                age = ACFT.age_s
                ft = "none in the datagram" if cur.t_feed_utc is None else _hms(cur.t_feed_utc)
                fix_lbl.set_text(f"{summary_text(cur)}\n"
                                 f"feed time {ft}   arrived {_hms(cur.t_wall)}\n"
                                 f"source {cur.source}   age {age:.1f} s   rate {rate_txt}")
                fix_lbl.classes(replace="text-sm font-mono whitespace-pre " + (
                    "text-red-700" if age > 10.0 else "text-emerald-800"))
            fix_table.rows = fix_rows(cur)
            fix_table.update()
            cnt_lbl.set_text(f"rx {ACFT.rx}   parsed {ACFT.parsed}   bad {ACFT.bad}"
                             f"   bad sentences {ACFT.bad_sentences}   window {ACFT.window_s:.1f} s")
            rates = ACFT.sentence_rates()
            if not rates:
                sent_lbl.set_text("none yet")
                sent_lbl.classes(replace="text-xs font-mono whitespace-pre-wrap text-gray-500")
            else:
                sent_lbl.set_text(sentence_rates_text(rates))
                sent_lbl.classes(replace="text-xs font-mono whitespace-pre-wrap text-gray-800")
            if abs(float(win_in.value or 0) - ACFT.window_s) > 0.01:
                win_in.value = ACFT.window_s
            off = ACFT.feed_offset_s()
            if off is None:
                clk_lbl.set_text(f"console UTC {_hms(time.time())}\nfeed time: none received")
                clk_lbl.classes(replace="text-sm font-mono whitespace-pre text-gray-500")
            else:
                mean, lo, hi, n = off
                clk_lbl.set_text(f"console UTC {_hms(time.time())}\n"
                                 f"feed minus console {mean:+.3f} s (over {n} fixes, "
                                 f"{lo:+.3f} to {hi:+.3f} s)")
                clk_lbl.classes(replace="text-sm font-mono whitespace-pre " + (
                    "text-amber-700" if abs(mean) > 1.0 else "text-emerald-800"))
            pk_lbl.set_text(f"Raw packets of {ac_sel.value}, newest first")
            rows = []
            for i, p in enumerate(reversed(ACFT.recent(int(n_in.value or 20)))):
                if p.pos is None:
                    res = "no"
                else:
                    res = f"{p.pos.source} {p.pos.lat_deg:+.5f} {p.pos.lon_deg:+.5f}"
                txt = p.text
                rows.append({"k": f"{p.t_wall:.6f}-{i}", "t": _hms(p.t_wall), "from": p.addr,
                             "n": len(p.raw), "res": res,
                             "raw": txt if len(txt) <= 300 else txt[:300] + " ..."})
            table.rows = rows
            table.update()
            rec = _SENDER.get("recorder")
            if rec is not None:
                rec_lbl.set_text(f"recording {rec.count} datagram(s) to {os.path.basename(rec.path)}")
                rec_btn.set_text("Stop recording")
            else:
                rec_lbl.set_text("")
                rec_btn.set_text("Record to file")
            parts = []
            ps = _SENDER.get("path")
            if ps is not None and ps.state == "playing":
                last = "" if ps.last is None else f" at {ps.last[1]:.5f} {ps.last[0]:.5f}"
                parts.append(f"path sender: {ps.sent} sent{last}")
            rp = _SENDER.get("replay")
            if rp is not None and rp.state == "playing":
                parts.append(f"replay: {rp.sent} of {len(rp.packets)} sent")
            for s in (ps, rp):
                if s is not None and s.error:
                    parts.append(s.error)
            snd_lbl.set_text("; ".join(parts) or "sender idle")

        timer_state = {"made": False}

        def _start_timer():
            # made in the connect handler, as the planner does, so a page that
            # never gets a socket is pruned without a timer waking on it; the
            # handler runs again on a reconnect, hence the flag
            if timer_state["made"]:
                return
            timer_state["made"] = True
            with root:
                ui.timer(1.0, _refresh)
        ui.context.client.on_connect(_start_timer)
        _refresh()
    return root
