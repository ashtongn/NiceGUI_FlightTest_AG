"""
scenario_tab.py: Scenario Builder section, rendered inside the encoder tab.

Turns the single-emitter encoder into a multi-emitter scenario author. The operator
authors a waveform with the normal encoder controls, then "Add to Scenario" captures
it (via a get_current_waveform callback the encoder provides) at a typed offset,
bounds-checks it against the selected TX radio's sample rate (channel_plan.lint), and
appends it to a growing list. "Save Scenario JSON" writes the multi-entry channel_plan
that the channelizer reads, plus the composed IQ in the chosen Epiq format.

Integration (one call from encoder_tab, after its authoring controls):

    from scenario_tab import build_scenario_section
    build_scenario_section(ui, get_current_waveform=_scenario_collect,
                           default_center_hz=425_025_000.0, output_dir=OUTPUT_DIR)

where get_current_waveform() returns (svc_fields, settings): exactly the encoder's
_collect() output, or raises/returns None if nothing is authored yet.
"""
from __future__ import annotations
import json, os
# Scenario storage dirs are configurable (deploy_config / env);
# the "RAM (tmpfs)" choice only appears where it can exist
from dpmg.deploy_config import (SCENARIO_DIR as _SCN_DISK_DIR,
                           SCENARIO_RAM_DIR as _SCN_RAM_DIR,
                           SCENARIO_RAM_LABEL as _SCN_RAM_LABEL,
                           scenario_storage_targets as _scn_targets,
                           is_scenario_ram_dir as _scn_is_ram)


# radio: valid TX sample rates (authoritative tables from sdr_core)
def _parse_signed_float(txt, default=0.0):
    """Parse '+5', '5', '-5', '5.5', ' +5 ': float. Handles the leading + that
    ui.number rejects. Returns default on blank/garbage."""
    if txt is None:
        return default
    t = str(txt).strip().lstrip("+")   # drop a leading + (and surrounding space)
    if t == "" or t == "-":
        return default
    try:
        return float(t)
    except Exception:
        return default


def _rate_table(radio: str):
    try:
        from dpmg.radio import sdr_core
        if radio.upper() == "X40":
            return list(sdr_core.X40_VALID_RATES_HZ)
        return list(sdr_core.G40_VALID_RATES_HZ)
    except Exception:
        # conservative fallback if sdr_core unavailable
        return [30_720_000] if radio.upper() == "G40" else [50_000_000]


def _settings_to_entry_params(settings: dict) -> dict:
    """Map the encoder's settings dict into a channel_plan Entry.params bag."""
    p = {
        "symbol_rate": float(settings.get("symbol_rate", 0) or 0),
        "samples_per_bit": int(settings.get("samples_per_bit", 8) or 8),
    }
    if settings.get("dibit_mapping"):
        p["dibit_mapping"] = settings["dibit_mapping"]
    if settings.get("gfsk_bw_hz"):
        p["gfsk_bw_hz"] = float(settings["gfsk_bw_hz"])
    # tones: encoder stores absolute tone0..3; convert to baseband deltas around their mean
    tones = [settings.get(f"tone{i}") for i in range(4)]
    tones = [float(t) for t in tones if t not in (None, 0)]
    if tones:
        mean = sum(tones) / len(tones)
        deltas = [t - mean for t in tones]
        # only meaningful for FSK-family; store as baseband deltas
        p["tone_deltas_hz"] = deltas
    return p


def build_scenario_section(ui, get_current_waveform, default_center_hz=425_025_000.0,
                           output_dir="scenarios", save_dialog=None):
    from dpmg.scenario.channel_plan import ChannelPlan, Entry, lint

    state = {"entries": [], "radio": "G40", "rate": None, "center": default_center_hz,
             "offset_mhz": 0.0, "center_mhz": default_center_hz/1e6}

    with ui.expansion("Scenario Builder (multi-emitter)", icon="library_add", value=True).classes("w-full"):
        with ui.row().classes("items-center gap-3 w-full"):
            radio_sel = ui.select(["G40", "X40"], value="G40", label="TX Radio") \
                          .props("dense outlined").classes("w-32")
            rate_sel = ui.select(_rate_table("G40"), label="Sample rate (Hz)",
                                 value=(30_720_000 if 30_720_000 in _rate_table("G40")
                                        else _rate_table("G40")[0])) \
                         .props("dense outlined").classes("w-56")
            # surface compose_plan loop_len_s (default 100 ms)
            loop_len_ms = ui.number("Loop length (ms)", value=100, min=1, max=60000, step=1).props("dense outlined").classes("w-40")
            center_in = ui.number("Center MHz", value=default_center_hz/1e6, format="%.4f") \
                          .props("dense outlined").classes("w-40")
            offset_in = ui.input("Offset MHz (this emitter)", value="0") \
                          .props("dense outlined").classes("w-48")
            offset_in.on_value_change(lambda e: state.__setitem__("offset_mhz", _parse_signed_float(e.value, 0.0)))
            center_in.on_value_change(lambda e: state.__setitem__("center_mhz", e.value if e.value is not None else default_center_hz/1e6))


        bounds_lbl = ui.label("").classes("text-xs text-gray-500 font-mono")

        with ui.row().classes("items-center gap-2"):
            add_btn = ui.button("Add to Scenario").props("color=primary")
            save_btn = ui.button("Save Scenario").props("color=secondary outline")
            clear_btn = ui.button("Clear").props("outline color=negative")
        # two sources: server-side G40 picker + browser upload
        def _compose_and_export(plan, stem, iq_ext, storage_value):
            import numpy as np
            from dpmg.scenario.plan_compose import compose_plan
            iq_dir = str(output_dir)
            if storage_value == _SCN_RAM_LABEL:
                iq_dir = _SCN_RAM_DIR
            os.makedirs(iq_dir, exist_ok=True)
            try:
                from dpmg.web.nicegui_app import sds_precheck
                _n = int(float(plan.sample_rate_hz) * (float(loop_len_ms.value or 100) / 1000.0))
                _n = ((_n + 8188 - 1) // 8188) * 8188
                _need = _n * (8 if iq_ext == 'bin' else 4)
                _ok, _msg = sds_precheck(iq_dir, _need)
                if not _ok:
                    _scn_note('IQ export blocked -- ' + _msg, type='negative')
                    return
            except ImportError:
                pass
            wb, fs, center, crep = compose_plan(plan, loop_len_s=float(loop_len_ms.value or 100) / 1000.0)
            try:
                _ob = crep.get('occ_bw', []) if isinstance(crep, dict) else []
                if _ob:
                    _c0 = float(plan.center_freq_hz)
                    _parts = []
                    for _d in _ob:
                        _rf = (_c0 + (_d['lo_hz'] + _d['hi_hz']) / 2.0) / 1e6
                        if _d['bw_hz'] < 1e3:
                            _lbl = 'CW/line'
                        elif _d['bw_hz'] < 1e6:
                            _lbl = '%.1f kHz' % (_d['bw_hz'] / 1e3)
                        else:
                            _lbl = '%.3f MHz' % (_d['bw_hz'] / 1e6)
                        _parts.append('%s @ %.4f MHz: %s' % (_d['name'], _rf, _lbl))
                    _scn_note('Occupied BW -- ' + ' | '.join(_parts), type='info')
                for _w in (crep.get('warnings', []) if isinstance(crep, dict) else []):
                    _scn_note('Plan lint: ' + _w, type='warning')
            except Exception:
                pass
            iq_fn = os.path.join(iq_dir, stem + "." + iq_ext)
            if iq_ext == "bin":
                np.asarray(wb, dtype=np.complex64).tofile(iq_fn)
                kind = "complex64"
            else:
                from dpmg.ui.fsk_encoder import _save_waveform
                wf_data = [(float(c.real), float(c.imag)) for c in wb]
                kind = _save_waveform(iq_fn, wf_data, fs)
            try:
                from dpmg.web.nicegui_app import sds_free_bytes, sds_fmt
                _fsz = os.path.getsize(iq_fn)
                _scn_note('IQ written: %s (%s) | free at %s: %s' % (
                    os.path.basename(iq_fn), sds_fmt(_fsz), iq_dir,
                    sds_fmt(sds_free_bytes(iq_dir))), type='positive')
            except Exception:
                pass
            _scn_note('Compose complete: %s (%d samples @ %g MSps, %s)' % (
                os.path.basename(iq_fn), len(wb), fs / 1e6, kind), type='positive')
        def _ingest_plan(plan, stem):
            if not getattr(plan, 'entries', None):
                _scn_note('plan has no entries', type='negative')
                return
            stem = (str(stem) or 'imported').replace('.json', '').replace('/', '_') or 'imported'
            rep = lint(plan)
            for _w in rep.get('warnings', []):
                _scn_note('Import lint: ' + _w, type='warning')
            if rep.get('errors'):
                _scn_note('plan has conflicts: ' + rep['errors'][0], type='negative')
                return
            try:
                state['entries'] = list(plan.entries)
                _refresh_table(); _refresh_bounds()
            except Exception:
                pass
            _scn_note('Loaded %s (%d emitters) -- composing...' % (stem, len(plan.entries)), type='info')
            try:  # persist source JSON next to composed IQ
                import os as _os_usj, json as _json_usj
                _os_usj.makedirs(str(output_dir), exist_ok=True)
                _jfn = _os_usj.path.join(str(output_dir), stem + '.json')
                with open(_jfn, 'w') as _jf:
                    _json_usj.dump(plan.to_dict(), _jf, indent=2)
                _scn_note('Saved scenario JSON: %s' % _os_usj.path.basename(_jfn), type='info')
            except Exception as _je:
                _scn_note('JSON save skipped: %s' % _je, type='warning')
            _compose_and_export(plan, stem, import_fmt.value, import_storage.value)
        async def _on_import(e):
            try:
                raw = await e.file.read() 
                if isinstance(raw, bytes):
                    raw = raw.decode('utf-8')
                plan = ChannelPlan.from_dict(json.loads(raw))
            except Exception as _pe:
                _scn_note('Upload parse failed: %s' % _pe, type='negative')
                return
            _ingest_plan(plan, os.path.basename(str(getattr(e.file, 'name', 'imported') or 'imported')))  
        async def _load_g40(_=None):
            fn = g40_sel.value
            if not fn:
                _scn_note('pick a scenario on the G40 first', type='warning')
                return
            try:
                with open(fn) as _f:
                    plan = ChannelPlan.from_dict(json.load(_f))
            except Exception as _pe:
                _scn_note('Load failed: %s' % _pe, type='negative')
                return
            _ingest_plan(plan, os.path.basename(fn))
        def _refresh_g40_list(_=None):
            import glob as _glob
            opts = {}
            for _dir in (str(output_dir), _SCN_DISK_DIR):
                try:
                    for _p in sorted(_glob.glob(os.path.join(_dir, '*.json'))):
                        opts[_p] = os.path.basename(_p)
                except Exception:
                    pass
            g40_sel.options = opts
            if g40_sel.value not in opts:
                g40_sel.value = (next(iter(opts)) if opts else None)
            g40_sel.update()
            pass  #  no log here (runs at build)
        #shared import settings
        with ui.row().classes("items-center gap-2 w-full"):
            import_fmt = ui.select(
                {"bin": "complex64 (.bin)", "g40iq": "G40 IQIQ (.g40iq)",
                 "g40qi": "G40 QIQI (.g40qi)", "x40iq": "X40 IQIQ (.x40iq)",
                 "x40qi": "X40 QIQI (.x40qi)", "wav": "WAV (I only)"},
                value=("g40iq" if state.get("radio", "G40") == "G40" else "x40iq"),
                label="Import IQ format").props("dense outlined").classes("w-56")
            import_storage = ui.select(list(_scn_targets()), value="Disk",
                label="IQ storage").props("dense outlined").classes("w-40")
        # source 1: a scenario already on the G40 (server-side) 
        with ui.row().classes("items-center gap-2 w-full"):
            ui.label("From G40:").classes("text-xs text-gray-500")
            g40_sel = ui.select({}, label="scenario on Jetson").props("dense outlined").classes("w-72")
            ui.button("Load + compose", on_click=_load_g40).props("color=primary dense")
            ui.button(icon="refresh", on_click=_refresh_g40_list).props("flat dense round")
        # source 2: upload from this (possibly remote) computer
        with ui.row().classes("items-center gap-2 w-full"):
            ui.label("From this computer:").classes("text-xs text-gray-500")
            ui.upload(auto_upload=True, on_upload=_on_import) \
                .props("flat bordered hide-upload-btn accept=.json max-files=1") \
                .classes("w-72")

        entry_table = ui.table(
            columns=[
                {"name": "name", "label": "Name", "field": "name", "align": "left"},
                {"name": "mod", "label": "Mod", "field": "mod"},
                {"name": "offset", "label": "Offset MHz", "field": "offset"},
                {"name": "sym", "label": "Sym/s", "field": "sym"},
                {"name": "bits", "label": "Bits", "field": "bits"},
                {"name": "sync", "label": "Sync", "field": "sync"},
            ],
            rows=[], row_key="name").classes("w-full").props("dense")
        import datetime as _dt  # persistent builder-body log
        ui.separator()
        ui.label('Activity log').classes('text-xs text-gray-500')
        scn_log = ui.log(max_lines=300).classes('w-full h-40')
        def _scn_note(msg, type='info', **kw):  # toast + timestamped log
            try:
                scn_log.push('[%s] %s' % (_dt.datetime.now().strftime('%H:%M:%S'), msg))
            except Exception:
                pass
            try:
                ui.notify(msg, type=type, **kw)
            except Exception:
                pass

        # radio change repopulates the rate list
        def _on_radio(e):
            state["radio"] = radio_sel.value
            rates = _rate_table(radio_sel.value)
            rate_sel.options = rates
            rate_sel.value = (30_720_000 if 30_720_000 in rates else rates[0])
            rate_sel.update()
            _refresh_bounds()
        radio_sel.on_value_change(_on_radio)

        def _current_plan(extra_entry=None):
            rate = float(rate_sel.value or 0)
            center = float(state.get("center_mhz") or (center_in.value or 0)) * 1e6
            plan = ChannelPlan(center_freq_hz=center, sample_rate_hz=rate,
                               rf_bandwidth_hz=0.80 * rate, tx_radio=state["radio"].lower())
            plan.entries = list(state["entries"])
            if extra_entry is not None:
                plan.entries = plan.entries + [extra_entry]
            return plan

        def _refresh_bounds():
            plan = _current_plan()
            rep = lint(plan)
            msg = (f"usable {rep['usable_bw_hz']/1e6:.2f} MHz @ {float(rate_sel.value or 0)/1e6:g} MSps"
                   f" | {len(state['entries'])} entries")
            if rep["errors"]:
                msg += f" | CONFLICT: {rep['errors'][0]}"
            elif rep["warnings"]:
                msg += f" | warn: {rep['warnings'][0]}"
            else:
                msg += " | OK"
            bounds_lbl.set_text(msg)

        def _make_entry():
            """Capture the currently-authored waveform into a channel_plan Entry."""
            res = get_current_waveform()
            if not res:
                return None, "author a waveform first (add fields + set modulation)"
            svc_fields, settings = res
            # bits via the encoder service
            try:
                from dpmg.services.encoder_service import build_bit_string
                bits = build_bit_string(svc_fields, settings)
            except Exception as e:
                return None, f"bit build failed: {e}"
            if not bits:
                return None, "no bits produced (check fields)"
            name = f"{settings.get('modulation','SIG')}-{len(state['entries']):02d}"
            # carry the encoder-derived descriptor
            frame = settings.get("frame_descriptor")
            if settings.get("secure"):
                # The authored field bits become the plaintext of one
                # sealed frame (AES-256-GCM-SIV, frame_codec standard sync); the plan
                # entry is marked so the radio opens it. Refuses instead of
                # falling back to a clear frame.
                try:
                    from dpmg.framing import frame_codec as _fc
                    payload = _fc._bits_to_bytes(bits)
                    bits = _fc.encode_secure(payload, None)
                except Exception as e:
                    return None, f"secure seal failed: {e}"
                frame = {"sync": _fc.SYNC_BITS, "sync_bits": _fc.SYNC_BITS, "max_sync_errors": 2, "has_length": False,
                         "secure": dict(settings["secure"]) if isinstance(settings["secure"], dict) else {"on": True}}
            entry = Entry(
                name=name, modulation=str(settings.get("modulation", "BPSK")),
                params=_settings_to_entry_params(settings),
                offset_hz=_parse_signed_float(offset_in.value, state.get("offset_mhz") or 0.0) * 1e6, power_db=0.0,
                bits=bits, fields=[{"desc": f.get("desc",""), "bin": f.get("bin",""),
                                    "encoding": f.get("encoding","None")} for f in svc_fields],
                role="known", decode=False, frame=frame,
            )
            return entry, None

        async def _add():
            # force number inputs to flush their pending values before reading
            # them (NiceGUI ui.number commits on blur; clicking Add reads stale otherwise).
            for _f in (offset_in, center_in):
                try:
                    _f.run_method("blur")
                except Exception:
                    pass
            try:
                import asyncio as _a; await _a.sleep(0.05)   # let the blur round-trip
            except Exception:
                pass
            entry, err = _make_entry()
            if err:
                ui.notify(err, type="negative", timeout=6000); return
            # lint the prospective plan (existing + this entry)
            rep = lint(_current_plan(extra_entry=entry))
            if rep["errors"]:
                ui.notify("Cannot add: " + rep["errors"][0], type="negative", timeout=8000)
                return
            for w in rep["warnings"]:
                ui.notify("Warning: " + w, type="warning", timeout=5000)
            state["entries"].append(entry)
            _refresh_table(); _refresh_bounds()
            ui.notify(f"Added {entry.name} at {entry.offset_hz/1e6:+.3f} MHz")

        def _refresh_table():
            entry_table.rows = [{
                "name": e.name, "mod": e.modulation,
                "offset": f"{e.offset_hz/1e6:+.3f}",
                "sym": f"{e.params.get('symbol_rate',0):g}",
                "bits": f"{len(e.bits)}",
                "sync": (e.frame.get("sync","") if e.frame else "-"),
            } for e in state["entries"]]
            entry_table.update()

        def _write_plan(fn, plan):
            os.makedirs(os.path.dirname(fn) or ".", exist_ok=True)
            with open(fn, "w") as f:
                json.dump(plan.to_dict(), f, indent=2)
            ui.notify(f"Saved {len(plan.entries)}-emitter scenario -> {fn}", timeout=8000)

        async def _save():
            if not state["entries"]:
                ui.notify("scenario is empty", type="warning"); return
            plan = _current_plan()
            rep = lint(plan)
            if rep["errors"]:
                ui.notify("Fix conflicts before saving: " + rep["errors"][0], type="negative"); return
            default_stem = f"scenario_{state['radio'].lower()}_{len(plan.entries)}ch"
            # filename prompt dialog
            with ui.dialog() as dlg, ui.card():
                ui.label("Save scenario as:")
                name_field = ui.input("filename (no extension)", value=default_stem) \
                               .props("outlined").classes("w-80")
                ui.label(f"-> {output_dir}/<name>.json  (+ composed IQ)").classes("text-xs text-gray-500")
                storage_sel = ui.select(list(_scn_targets()), value="Disk", label="IQ storage").props("dense outlined").classes("w-40")  
                storage_free_lbl = ui.label().classes('text-xs text-gray-500')  
                def _storage_refresh(_=None):  
                    try:
                        import shutil as _sh
                        from dpmg.web.nicegui_app import sds_free_bytes, sds_fmt
                        _d = _SCN_RAM_DIR if storage_sel.value == _SCN_RAM_LABEL else str(output_dir)
                        os.makedirs(_d, exist_ok=True)
                        _du = _sh.disk_usage(_d)
                        storage_free_lbl.text = '%s -- used %s, free %s (of %s)' % (
                            _d, sds_fmt(_du.used), sds_fmt(sds_free_bytes(_d)), sds_fmt(_du.total))
                    except Exception as _ex:
                        storage_free_lbl.text = 'storage: %s' % _ex
                storage_sel.on_value_change(_storage_refresh)
                _storage_refresh()
                iq_fmt = ui.select(
                    {"none": "JSON only (no IQ)",
                     "bin": "TX-ready complex64 (.bin) -- loads in G40 tab",
                     "g40iq": "G40 16-bit IQIQ (.g40iq)",
                     "g40qi": "G40 16-bit QIQI (.g40qi)",
                     "x40iq": "X40 14-bit IQIQ (.x40iq)",
                     "x40qi": "X40 14-bit QIQI (.x40qi)",
                     "wav":   "WAV (I only, debug)"},
                    value=("g40iq" if state["radio"]=="G40" else "x40iq"),
                    label="Composed IQ format").props("dense outlined").classes("w-72")
                with ui.row():
                    ui.button("Cancel", on_click=lambda: dlg.submit(None)).props("flat")
                    ui.button("Save", on_click=lambda: dlg.submit({"stem": name_field.value, "fmt": iq_fmt.value})).props("color=primary")
            choice = await dlg
            if not choice:
                return
            stem = choice["stem"]; iq_ext = choice["fmt"]
            if not stem:
                return
            stem = str(stem).strip().replace(".json", "").replace("/", "_") or default_stem
            os.makedirs(output_dir, exist_ok=True)
            fn = os.path.join(str(output_dir), stem + ".json")
            if os.path.exists(fn):
                # confirm overwrite
                with ui.dialog() as ow, ui.card():
                    ui.label(f"{fn} exists. Overwrite?")
                    with ui.row():
                        ui.button("Cancel", on_click=lambda: ow.submit(False)).props("flat")
                        ui.button("Overwrite", on_click=lambda: ow.submit(True)).props("color=negative")
                if not await ow:
                    return
            _write_plan(fn, plan)
            # compose the wideband and write it in the chosen Epiq format
            if iq_ext and iq_ext != "none":
                try:
                    import numpy as np
                    from dpmg.scenario.plan_compose import compose_plan
                    # resolve target dir + pre-write size-vs-free gate
                    iq_dir = str(output_dir)
                    _sd = _SCN_RAM_DIR if storage_sel.value == _SCN_RAM_LABEL else (getattr(save_dialog, 'storage_dir', None) if save_dialog is not None else None)  #local selector wins, dialog fallback
                    if _sd and _scn_is_ram(_sd):
                        iq_dir = str(_sd)
                    os.makedirs(iq_dir, exist_ok=True)
                    try:
                        from dpmg.web.nicegui_app import sds_precheck
                        _n = int(float(plan.sample_rate_hz) * (float(loop_len_ms.value or 100) / 1000.0))
                        _n = ((_n + 8188 - 1) // 8188) * 8188
                        _need = _n * (8 if iq_ext == 'bin' else 4)
                        _ok, _msg = sds_precheck(iq_dir, _need)
                        if not _ok:
                            _scn_note('IQ export blocked -- ' + _msg, type='negative')
                            return
                    except ImportError:
                        pass
                    wb, fs, center, crep = compose_plan(plan, loop_len_s=float(loop_len_ms.value or 100) / 1000.0)  
                    try:  # per-emitter occupied-BW report
                        _ob = crep.get('occ_bw', []) if isinstance(crep, dict) else []
                        if _ob:
                            _c0 = float(plan.center_freq_hz)
                            _parts = []
                            for _d in _ob:
                                _rf = (_c0 + (_d['lo_hz'] + _d['hi_hz']) / 2.0) / 1e6
                                if _d['bw_hz'] < 1e3:
                                    _lbl = 'CW/line'
                                elif _d['bw_hz'] < 1e6:
                                    _lbl = '%.1f kHz' % (_d['bw_hz'] / 1e3)
                                else:
                                    _lbl = '%.3f MHz' % (_d['bw_hz'] / 1e6)
                                _parts.append('%s @ %.4f MHz: %s' % (_d['name'], _rf, _lbl))
                            _scn_note('Occupied BW -- ' + ' | '.join(_parts), type='info')
                        for _w in (crep.get('warnings', []) if isinstance(crep, dict) else []):
                            _scn_note('Plan lint: ' + _w, type='warning')
                    except Exception:
                        pass
                    iq_fn = os.path.join(iq_dir, stem + "." + iq_ext)  
                    if iq_ext == "bin":
                        # raw complex64: exactly what sdr_core.WaveformLoader.load reads
                        np.asarray(wb, dtype=np.complex64).tofile(iq_fn)
                        kind = "complex64"
                        try:  # post-write size + free-space report
                            from dpmg.web.nicegui_app import sds_free_bytes, sds_fmt
                            _fsz = os.path.getsize(iq_fn)
                            _scn_note('IQ written: %s (%s) | free at %s: %s' % (
                                os.path.basename(iq_fn), sds_fmt(_fsz), iq_dir,
                                sds_fmt(sds_free_bytes(iq_dir))), type='positive')
                            _storage_refresh()
                        except Exception:
                            pass
                    else:
                        from dpmg.ui.fsk_encoder import _save_waveform
                        wf_data = [(float(c.real), float(c.imag)) for c in wb]
                        kind = _save_waveform(iq_fn, wf_data, fs)
                        try:  # post-write size + free-space report
                            from dpmg.web.nicegui_app import sds_free_bytes, sds_fmt
                            _fsz = os.path.getsize(iq_fn)
                            _scn_note('IQ written: %s (%s) | free at %s: %s' % (
                                os.path.basename(iq_fn), sds_fmt(_fsz), iq_dir,
                                sds_fmt(sds_free_bytes(iq_dir))), type='positive')
                            _storage_refresh()
                        except Exception:
                            pass
                    ui.notify(f"IQ written: {os.path.basename(iq_fn)} "
                              f"({len(wb)} samples @ {fs/1e6:g} MSps, {kind})", timeout=9000)
                except Exception as _ie:
                    import traceback; traceback.print_exc()
                    ui.notify(f"IQ export failed: {_ie}", type="negative", timeout=9000)

        def _clear():
            state["entries"].clear(); _refresh_table(); _refresh_bounds()

        add_btn.on_click(_add); save_btn.on_click(_save); clear_btn.on_click(_clear)
        _refresh_g40_list()  # populate after _scn_note + g40_sel exist
        center_in.on_value_change(lambda e: _refresh_bounds())
        rate_sel.on_value_change(lambda e: _refresh_bounds())
        _refresh_bounds()
