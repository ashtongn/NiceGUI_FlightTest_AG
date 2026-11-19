#!/usr/bin/env python3
"""
Data Packet Message Generator: NiceGUI port of the original Tkinter app.

Engineering view tabs: Encoder, IQ Validator, MXG Deploy, SDR, Payload, Mission
planner (engineering) and Aircraft feed. The operator view and the mission
planner are separate pages.

This file is the GUI layer only.
Run locally from the repo root:  python -m dpmg.web.nicegui_app, then http://localhost:8080
Run in Docker: see the Dockerfile and launch_gui.sh at the repo root.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path
from typing import Optional, Iterable

from nicegui import ui, app, run
import dpmg.web.timer_patch  # noqa: F401
from dpmg.services.encoder_service import encode_fields_to_bin
from dpmg.web.channelizer_tab import build_channelizer_tab, CHANNELIZER
from dpmg.web.scenario_tab import build_scenario_section

PI = "\u03c0"   # lower-case greek pi symbol

from dpmg.radio import sdr_core as sdr
from dpmg.radio import g40_clock as _g40_clock
from dpmg.dsp import psd_trace as pt
from dpmg.web.link_tab import (build_link_header, build_remote_sdr,
                      build_operator_sdr)


# Begin helpers
import shutil as _sds_shutil
try:
    import psutil as _sds_psutil
except Exception:
    _sds_psutil = None

# Paths come from deploy_config (env-overridable); the RAM option
# is only listed where a tmpfs/RAM folder can exist (see scenario_ram_available)
from dpmg.deploy_config import scenario_storage_targets as _sds_targets, \
    is_scenario_ram_dir as _sds_is_ram
SDS_TARGETS = _sds_targets()

def sds_free_bytes(path):
    try:
        free = _sds_shutil.disk_usage(str(path)).free
    except OSError:
        return 0
    if _sds_is_ram(path) and _sds_psutil is not None:
        free = min(free, _sds_psutil.virtual_memory().available)
    return int(free)

def sds_fmt(n):
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024.0:
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f TiB" % n

def sds_precheck(target_dir, needed_bytes, margin=1.10):
    """Pre-write size-vs-free gate. Returns (ok, human_message)."""
    free = sds_free_bytes(target_dir)
    ok = free >= int(needed_bytes * margin)
    return ok, "need ~%s (+10%% margin), free %s at %s" % (
        sds_fmt(needed_bytes), sds_fmt(free), target_dir)
# End helpers 


# Global hardware/session state.

HW = {
    "mxg": None,    # e.g. a pyvisa / socket connection object
    "b200": None,   # e.g. an SDRBackend instance
    "x40": None,    # e.g. an SDRBackend instance
    "g40": None,    # Epiq G40 (two NV100 cards; card via extras["card"])
}

# Per-SDR FullDuplexScheduler instances (the radios are physically singular).
SCHED: dict = {"b200": None, "x40": None, "g40": None}

OUTPUT_DIR = Path.cwd() / "output"   # where generated .bin files gets saved


# A small server-side file picker.
# Because the app runs on the Jetson and is reached over the browser, the .bin
# and .json files live on the server's filesystem, not the client's.
 
class LocalFilePicker(ui.dialog):
    def __init__(self, start_dir: str = "~", *, only_ext: Optional[Iterable[str]] = None):
        super().__init__()
        self.path = Path(start_dir).expanduser()
        try:
            self.path = self.path.resolve()
        except Exception:
            self.path = Path.home()
        self.only_ext = {e.lower() for e in only_ext} if only_ext else None

        with self, ui.card().classes("w-[680px] max-w-full"):
            self.header = ui.label().classes("text-sm font-mono break-all")
            with ui.row().classes("w-full items-center gap-1"):
                ui.button(icon="arrow_upward", on_click=self._up).props("flat dense").tooltip("Up one level")
                ui.button(icon="home", on_click=self._home).props("flat dense").tooltip("Home")
                ui.space()
                ui.button("Cancel", on_click=lambda: self.submit(None)).props("flat")
            self.listing = ui.column().classes("w-full max-h-80 overflow-auto gap-0")
        self._refresh()

    def _up(self):
        self.path = self.path.parent
        self._refresh()

    def _home(self):
        self.path = Path.home()
        self._refresh()

    def _enter(self, p: Path):
        self.path = p
        self._refresh()

    def _refresh(self):
        self.header.set_text(str(self.path))
        self.listing.clear()
        with self.listing:
            try:
                entries = sorted(self.path.iterdir(),
                                 key=lambda x: (x.is_file(), x.name.lower()))
            except (PermissionError, FileNotFoundError):
                ui.label("Cannot read this directory").classes("text-red-600 p-2")
                return
            for p in entries:
                if p.name.startswith("."):
                    continue
                if p.is_dir():
                    ui.button(p.name, icon="folder", on_click=lambda p=p: self._enter(p)) \
                        .props("flat no-caps align=left").classes("w-full justify-start")
                else:
                    if self.only_ext and p.suffix.lower() not in self.only_ext:
                        continue
                    ui.button(p.name, icon="description", on_click=lambda p=p: self.submit(str(p))) \
                        .props("flat no-caps align=left").classes("w-full justify-start")
                        
                        
                        
class SaveAsDialog(ui.dialog):
    TYPES = {
        "Keysight MXG ARB IQ - signed 16-bit big-endian (*.bin)": ".bin",
        "WAV audio - signed 16-bit PCM (*.wav)": ".wav",
        "Raw uint16 binary (legacy) (*.raw)": ".raw",
        "X40 14-bit DAC IQ - signed, LE, right-justified (*.x40iq)": ".x40iq",
        "X40 14-bit DAC QI - signed, LE, right-justified (*.x40qi)": ".x40qi",
        "G40 16-bit DAC IQ - signed, LE (*.g40iq)": ".g40iq",
        "G40 16-bit DAC QI - signed, LE (*.g40qi)": ".g40qi",
    }
    X40_RATES_HZ = [
        50_000_000, 61_440_000, 100_000_000, 122_880_000, 153_600_000,
    ]
    G40_RATES_HZ = [
        250_000, 541_667, 740_740, 750_000, 1_000_000, 1_920_000, 2_457_600,
        2_500_000, 2_800_000, 3_840_000, 4_000_000, 4_915_200, 5_000_000,
        5_600_000, 7_690_000, 9_830_400, 10_000_000, 11_200_000, 15_360_000,
        16_000_000, 20_000_000, 21_666_700, 22_000_000, 23_040_000, 30_720_000,
        40_000_000, 61_440_000,
    ]
    X40_EXTS = {".x40iq", ".x40qi"}
    G40_EXTS = {".g40iq", ".g40qi"}
    SDR_EXTS = X40_EXTS | G40_EXTS

    def __init__(self, start_dir, default_name="message", min_rate=0):
        super().__init__()
        self.min_rate = int(min_rate)
        self.path = Path(start_dir).expanduser()
        if not self.path.exists():
            self.path = Path.cwd()
        with self, ui.card().classes("w-[680px] max-w-full"):
            ui.label("Save waveform").classes("text-lg font-bold")
            self.header = ui.label().classes("text-sm font-mono break-all")
            with ui.row().classes("items-center gap-1"):
                ui.button(icon="arrow_upward", on_click=self._up).props("flat dense").tooltip("Up")
                ui.button(icon="home", on_click=self._home).props("flat dense").tooltip("Home")
            self.listing = ui.column().classes("w-full max-h-60 overflow-auto gap-0")
            self.name_in = ui.input("File name", value=default_name).props("dense outlined").classes("w-full")
            # Disk/RAM target + live free-space readout
            self.storage_dir = SDS_TARGETS['Disk']
            self._sds_free_lbl = None
            def _sds_refresh(_=None):
                self.storage_dir = SDS_TARGETS.get(self._sds_radio.value, SDS_TARGETS['Disk'])
                if self._sds_free_lbl is not None:
                    self._sds_free_lbl.text = 'Free: %s on %s' % (
                        sds_fmt(sds_free_bytes(self.storage_dir)), self.storage_dir)
            self._sds_radio = ui.radio(list(SDS_TARGETS.keys()), value='Disk',
                                       on_change=_sds_refresh).props('inline')
            self._sds_free_lbl = ui.label()
            _sds_refresh()
            self.type_in = ui.select(
                list(self.TYPES.keys()),
                value=list(self.TYPES.keys())[0],
                label="Save as type",
                on_change=lambda: self._toggle_rate(),       # detect format: show/hide rate
            ).props("dense outlined").classes("w-full")
            self.rate_in = ui.select(
                {hz: f"{hz/1e6:g} MSa/s" for hz in self.X40_RATES_HZ},   # {value: label} dict
                value=self.X40_RATES_HZ[0],
                label="SDR sample rate (known-good)",
            ).props("dense outlined").classes("w-full")
            self.rate_in.set_visibility(False)
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=lambda: self.submit(None)).props("flat")
                ui.button("Save", on_click=self._save).props("color=primary")
        self._refresh()

    def _toggle_rate(self):
        ext = self.TYPES[self.type_in.value]
        if ext in self.X40_EXTS:
            rates = self.X40_RATES_HZ
        elif ext in self.G40_EXTS:
            rates = self.G40_RATES_HZ
        else:
            self.rate_in.set_visibility(False)
            return
        self.rate_in.set_visibility(True)
        
        opts = {}
        first_valid = None
        for hz in rates:
            valid = hz >= self.min_rate
            opts[hz] = f"{hz/1e6:g} MSa/s" + ("" if valid else "  (too low)")
            if valid and first_valid is None:
                first_valid = hz
        self.rate_in.options = opts
        if first_valid is not None:
            self.rate_in.value = first_valid
        self.rate_in.update()

    def _up(self):
        self.path = self.path.parent; self._refresh()

    def _home(self):
        self.path = Path.home(); self._refresh()

    def _enter(self, p):
        self.path = p; self._refresh()

    def _refresh(self):
        self.header.set_text(str(self.path))
        self.listing.clear()
        with self.listing:
            try:
                dirs = sorted([p for p in self.path.iterdir()
                               if p.is_dir() and not p.name.startswith(".")],
                              key=lambda x: x.name.lower())
            except (PermissionError, FileNotFoundError):
                ui.label("Cannot read directory").classes("text-red-600 p-2"); return
            for p in dirs:
                ui.button(p.name, icon="folder", on_click=lambda p=p: self._enter(p)) \
                    .props("flat no-caps align=left").classes("w-full justify-start")

    def _save(self):
        ext = self.TYPES[self.type_in.value]
        target_rate = self.rate_in.value if ext in self.SDR_EXTS else None
        if target_rate is not None and target_rate < self.min_rate:
            ui.notify(f"Selected rate is below the minimum needed "
                      f"({self.min_rate/1e6:g} MSa/s). Pick a higher rate.", type="negative")
            return
        stem = (self.name_in.value or "message").strip()
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
        self.submit((str(self.path), stem, ext, target_rate))


def section(title: str):
    
    card = ui.card().classes("w-full")
    with card:
        ui.label(title).classes("text-base font-bold text-gray-700")
    return card

def build_resampler_section():
    """Resample an IQ file from its native rate to any supported SDR rate, in a
    chosen output format. Runs locally on this GCS; a raw IQ file carries neither
    its format (extension) nor its rate (you supply the native rate).

    The file comes from one of two places. Browse looks in the ground station's
    own folders, which in the Docker container is little more than output/.
    Upload takes a file from the computer the browser is on and keeps it in
    output/resample/, so a file anywhere on the laptop can be resampled. The
    result is saved beside its source and can be downloaded to that computer."""
    import asyncio as _asyncio
    import os as _os
    from dpmg.dsp import iq_resample as _rs
    try:
        from dpmg.radio.sdr_core import G40_VALID_RATES_HZ as _RATES
    except Exception:
        _RATES = [1_920_000, 7_680_000, 15_360_000, 30_720_000, 61_440_000]
    from dpmg.paths import match_folder_owner as _own
    st = {"src": None, "dst": None}
    with ui.row().classes("w-full items-center gap-2 flex-wrap"):
        browse = ui.button("Browse on the ground station...", icon="folder_open").props("outline dense") \
            .tooltip("a file in the ground station's own folders (output/)")
        up_src = ui.upload(label="or upload from this computer", auto_upload=True,
                           max_file_size=2_000_000_000).props("dense flat bordered").classes("w-80") \
            .tooltip("any IQ file on the computer this page is open on; it is copied to output/resample/")
    srclbl = ui.label("no file selected").classes("font-mono text-xs break-all")
    with ui.row().classes("w-full items-center gap-3 flex-wrap"):
        infmt = ui.select(_rs.FORMAT_NAMES, value=_rs.AUTO, label="Input format") \
            .props("dense outlined").classes("w-72") \
            .tooltip("how to read the file's samples: bits, int/float, I/Q order, "
                     "endianness. 'auto' uses the extension.")
        nat = ui.number("Native rate (MSps)", value=10.0, format="%.6g") \
            .props("dense outlined").classes("w-44")
        skip = ui.number("Header skip (bytes)", value=0, format="%.0f") \
            .props("dense outlined").classes("w-40") \
            .tooltip("bytes to drop from the start of the file before the IQ data")
    with ui.row().classes("w-full items-center gap-3 flex-wrap"):
        tgt = ui.select({int(h): f"{h/1e6:g} MSps" for h in _RATES},
                        value=30_720_000, label="Target rate") \
            .props("dense outlined").classes("w-40")
        fmt = ui.select([".iq.bin", ".g40iq", ".x40iq", ".wav"], value=".iq.bin",
                        label="Output format").props("dense outlined").classes("w-36")
        run = ui.button("Resample", icon="sync").props("color=primary")
    with ui.row().classes("w-full items-center gap-3 flex-wrap"):
        stat = ui.label("pick an IQ file, set its native rate, choose a target rate") \
            .classes("font-mono text-xs text-gray-600 break-all")
        dl = ui.button("Download the result", icon="download",
                       on_click=lambda: st["dst"] and ui.download(st["dst"])).props("outline dense")
        dl.set_visibility(False)

    def _take(p, note=""):
        st["src"] = p
        st["dst"] = None
        dl.set_visibility(False)
        srclbl.set_text(p + note)
        # a BLUE file (.cdif, or any name with a BLUE header) and a .wav carry their
        # own rate: fill it in so the operator does not have to know it
        nr = _rs.native_rate(p)
        if nr:
            nat.value = nr / 1e6
            if _rs._is_blue(p):
                infmt.value = "CDIF (BLUE / SignalVu)"
            srclbl.set_text(f"{p}{note}   (native {nr/1e6:g} MSps, read from the file)")

    async def _browse():
        p = await LocalFilePicker(str(OUTPUT_DIR if OUTPUT_DIR.exists() else "~"),
                                  only_ext=set(_rs.IQ_EXTS))
        if p:
            _take(p)
    browse.on_click(_browse)

    async def _uploaded(e):
        # keep only the file's own name: nothing in it may point outside the folder
        name = _os.path.basename(str(e.file.name).replace("\\", "/")) or "upload.bin"
        folder = OUTPUT_DIR / "resample"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            _own(str(folder), str(OUTPUT_DIR))
            data = await e.file.read()
            dst = folder / name
            await _asyncio.to_thread(dst.write_bytes, data)
            _own(str(dst), str(folder))
        except Exception as ex:
            stat.set_text(f"upload failed: {ex}")
            ui.notify(f"upload failed: {ex}", type="negative")
            return
        up_src.reset()
        _take(str(dst), f"   ({len(data) / 1e6:.1f} MB, uploaded)")
        stat.set_text("file uploaded; check the native rate, choose a target rate")
    up_src.on_upload(_uploaded)

    async def _run():
        if not st["src"]:
            stat.set_text("pick an IQ file first"); return
        fs_in = float(nat.value or 0) * 1e6
        fs_out = float(int(tgt.value))
        if fs_in <= 0:
            stat.set_text("set the file's native rate (MSps)"); return
        base = _os.path.splitext(st["src"])[0]
        if base.endswith(".iq"):
            base = base[:-3]
        dst = f"{base}_{fs_out/1e6:g}msps{fmt.value}"
        stat.set_text("resampling...")
        try:
            rep = await _asyncio.to_thread(_rs.resample_file, st["src"], dst,
                                           fs_in, fs_out, infmt.value,
                                           int(skip.value or 0))
            _own(dst, _os.path.dirname(dst) or ".")
            st["dst"] = dst
            dl.set_visibility(True)
            stat.set_text(f"OK: {rep['n_in']} -> {rep['n_out']} samp "
                          f"(x{rep['ratio']:.3f}), saved to {dst}")
            ui.notify("resampled", type="positive")
        except Exception as e:
            stat.set_text(f"error: {e}")
            ui.notify(f"resample failed: {e}", type="negative")
    run.on_click(_run)


# TAB 1: ENCODER

def encoder_tab():
    field_rows: list[dict] = []
    next_id = {"v": 0}

    with section("Field entry"):
        with ui.row().classes("w-full items-end gap-4"):
            value_in = ui.input("Value").props("dense outlined").classes("grow")
            fmt_in = ui.select(["Hex", "Binary"], value="Hex",
                               label="Format").props("dense outlined").classes("w-40")
        with ui.row().classes("w-full items-end gap-4"):
            with ui.column().classes("grow gap-1"):
                desc_in = ui.select(
                    ["Payload", "Preamble", "Sync", "Header", "Delimiter",
                     "Telemetry", "Control", "Other", "Custom"],
                    value="Payload", label="Description",
                    on_change=lambda: desc_custom.set_visibility(desc_in.value == "Custom"),
                ).props("dense outlined").classes("w-full")
                desc_custom = ui.input(placeholder="Enter custom description") \
                    .props("dense outlined").classes("w-full")
                desc_custom.set_visibility(False)
            enc_in = ui.select(["None", "Convolutional Rate 1/2", "Convolutional Rate 1/3", "Hamming(7,4)", "Hamming(13,9)", "Hamming(15,11)", "BPTC(196,96)", "Golay(20,8)", "Rate 3/4 Trellis", "CRC-CCITT", "CRC-9", "CRC-32", "DMR CACH (CRC-9 over 15b)", "DMR Slot Type (Golay(20,8))", "DMR Data Block (CRC-CCITT + BPTC196/96"],
                               value="None", label="Encoding").props("dense outlined").classes("w-56")
        with ui.row().classes("w-full gap-2"):
            ui.button("Add Field", on_click=lambda: add_field()).classes("grow")
            ui.button("Delete Selected", on_click=lambda: delete_selected()).classes("grow")
            ui.button("Clear Fields", on_click=lambda: clear_fields()).classes("grow")
            with ui.dropdown_button("Message Options", auto_close=True).classes("grow"):
                ui.item("Save Message", on_click=lambda: save_message_def())
                ui.item("Load Message", on_click=lambda: load_message_def())
                ui.item("Load DMR Template", on_click=lambda: load_dmr_template())
                ui.item("Edit DMR Template", on_click=lambda: edit_dmr_template())

    with section("Fields"):
        columns = [
            {"name": "bitfield", "label": "Bit Field", "field": "bitfield", "align": "left"},
            {"name": "hex", "label": "Value (Hex)", "field": "hex", "align": "left"},
            {"name": "bin", "label": "Value (Bin)", "field": "bin", "align": "left"},
            {"name": "desc", "label": "Description", "field": "desc", "align": "left"},
            {"name": "enc", "label": "Encoding", "field": "enc", "align": "left"},
        ]
        fields_table = ui.table(columns=columns, rows=field_rows, row_key="id",
                                selection="multiple").classes("w-full").props("dense")

    with ui.row().classes("w-full gap-4 items-stretch"):
        # Modulation
        with ui.card().classes("grow"):
            ui.label("Modulation").classes("text-base font-bold text-gray-700")
            mod_in = ui.select(["FSK", "4FSK", "BPSK", "QPSK", f"{PI}/4-QPSK", "DQPSK", "OQPSK", "GFSK", "8PSK", "8QAM", "16QAM", "16APSK", "32APSK", "BPSK-DSSS", "QPSK-DSSS", "CSS"],
                               value="FSK", label="Modulation",
                               on_change=lambda: tone_settings.refresh()).props("dense outlined").classes("w-40")
            sym_in = ui.number("Symbol rate", value=9600, format="%d",
                               on_change=lambda: _update_native_sr()).props("dense outlined")
            with ui.row().classes("items-end gap-2"):
                mxg_sr_in = ui.input("Native sample rate (Sa/s)", value="355200").props("dense outlined readonly")
                ui.button("Set SR", on_click=lambda: ui.notify(f"Set SR = {mxg_sr_in.value} Sa/s"))
            ui.label("Encoding is selected per field.").classes("text-xs text-gray-500")

        # All PSK variants share the same look. (PSK/8PSK/16PSK kept for future use.)
        PSK_MODS = {"BPSK", "QPSK", f"{PI}/4-QPSK", "DQPSK", "OQPSK", "PSK", "8PSK", "16PSK",
                    "8QAM", "16QAM", "16APSK", "32APSK"}  # Samples/bit + center-freq UI
        tone_els: dict = {}

        # Tone settings (varies by modulation)
        with ui.card().classes("grow"):
            ui.label("Tone settings").classes("text-base font-bold text-gray-700")

            @ui.refreshable
            def tone_settings():
                tone_els.clear()
                mod = mod_in.value

                if mod in PSK_MODS:                      # all PSK variants: samples/bit + center freq
                    tone_els["spb"] = ui.number("Samples per bit", value=37, format="%d",
                                                on_change=lambda: _update_native_sr()).props("dense outlined")
                    with ui.row().classes("items-end gap-2"):
                        tone_els["cf"] = ui.number("Center freq", value=425025, format="%.6g").props("dense outlined")
                        tone_els["cfu"] = ui.select(["MHz", "kHz", "Hz"], value="MHz").props("dense outlined")

                elif mod == "4FSK":
                    with ui.grid(columns=4).classes("items-end gap-2"):
                        for k in ("t0", "t1", "t2", "t3"):
                            tone_els[k] = ui.number(f"Tone {k[1]}", value=0).props("dense outlined")
                            tone_els[k + "u"] = ui.select(["MHz","kHz", "Hz"], value="kHz").props("dense outlined")
                    tone_els["spb"] = ui.number("Samples per bit", value=37, format="%d",
                                                on_change=lambda: _update_native_sr()).props("dense outlined")
                    with ui.row().classes("items-end gap-2"):
                        tone_els["cf"] = ui.number("Center freq", value=425025, format="%.6g").props("dense outlined")
                        tone_els["cfu"] = ui.select(["MHz","kHz", "Hz"], value="kHz").props("dense outlined")
                        tone_els["dev"] = ui.number("4FSK deviation", value=0.648).props("dense outlined")
                        tone_els["devu"] = ui.select(["MHz","kHz", "Hz"], value="kHz").props("dense outlined")
                    with ui.row().classes("items-center gap-2"):
                        ui.button("Generate 4FSK tones", on_click=lambda: gen_4fsk())
                        tone_els["map"] = ui.select(["dmr", "dmr-inverted", "binary", "gray"], value="dmr",
                                                    label="4FSK mapping").props("dense outlined")
                        ui.button("Info", on_click=lambda: ui.notify("4FSK mapping info"))

                elif mod == "GFSK":
                    with ui.row().classes("items-end gap-2"):
                        for k in ("t0", "t1"):
                            tone_els[k] = ui.number(f"Tone {k[1]}", value=0).props("dense outlined")
                            tone_els[k + "u"] = ui.select(["MHz","kHz", "Hz"], value="kHz").props("dense outlined")
                    tone_els["spb"] = ui.number("Samples per bit", value=37, format="%d",
                                                on_change=lambda: _update_native_sr()).props("dense outlined")
                    with ui.row().classes("items-end gap-2"):
                        tone_els["bt"] = ui.number("BT product", value=0.3).props("dense outlined")
                        tone_els["preset"] = ui.select(["Custom", "Bluetooth(BT=0.3)", "BLE(BT=0.5)", "DECT(BT=0.5)", "ZigBee/802.15.4(BT=0.5)", "DMR GFSK(BT=0.5)",
                        "P25 C4FM(BT=0.4", f"TETRA {PI}/4-DQPSK(BT=0.35)"], value="Custom",
                                                       label="Preset",
                                                       on_change=lambda: _apply_gfsk_preset()).props("dense outlined")

                elif mod in ("BPSK-DSSS", "DSSS", "QPSK-DSSS"):
                    tone_els["spb"] = ui.number("Samples per bit", value=124, format="%d",
                                                on_change=lambda: _update_native_sr()).props("dense outlined")
                    with ui.row().classes("items-end gap-2"):
                        tone_els["sf"] = ui.number("Spread factor (chips/bit)", value=31, format="%d").props("dense outlined")
                        tone_els["dsss_modem"] = ui.select(["bpsk", "qpsk"],
                            value=("qpsk" if mod == "QPSK-DSSS" else "bpsk"),
                            label="Spread symbol").props("dense outlined")
                    with ui.row().classes("items-end gap-2"):
                        tone_els["code_family"] = ui.select(
                            ["seed", "m", "gold", "custom"], value="seed",
                            label="Spreading code").props("dense outlined")
                        tone_els["code_n"] = ui.number("n (m/gold)", value=7, format="%d").props("dense outlined")
                        tone_els["code_index"] = ui.number("Gold index", value=0, format="%d").props("dense outlined")
                        tone_els["pn"] = ui.number("Seed", value=1, format="%d").props("dense outlined")
                    tone_els["code_chips"] = ui.input("Custom chips (+/-/1/0)", value="").props("dense outlined").classes("w-full")
                    try:
                        _update_native_sr()
                    except NameError:
                        pass

                elif mod == "CSS":
                    with ui.row().classes("items-end gap-2"):
                        tone_els["css_sf"] = ui.number("Spreading factor (bits/sym)", value=7, format="%d",
                                                       on_change=lambda: _update_native_sr()).props("dense outlined")
                        tone_els["css_bw"] = ui.number("Bandwidth (Hz)", value=125000, format="%d",
                                                       on_change=lambda: _update_native_sr()).props("dense outlined")
                        tone_els["css_os"] = ui.number("Oversample", value=1, format="%d",
                                                       on_change=lambda: _update_native_sr()).props("dense outlined")
                    try:
                        _update_native_sr()
                    except NameError:
                        pass

                else:                                    # FSK / 2FSK / ASK / OOK: two tones + samples/bit
                    with ui.row().classes("items-end gap-2"):
                        for k in ("t0", "t1"):
                            tone_els[k] = ui.number(f"Tone {k[1]}", value=0).props("dense outlined")
                            tone_els[k + "u"] = ui.select(["MHz","kHz", "Hz"], value="kHz").props("dense outlined")
                    tone_els["spb"] = ui.number("Samples per bit", value=37, format="%d",
                                                on_change=lambda: _update_native_sr()).props("dense outlined")
                    try:
                        _update_native_sr()
                    except NameError:
                        pass    

            tone_settings()   # build it once

        # Filter settings 
        with ui.card().classes("grow"):
            ui.label("Filter settings").classes("text-base font-bold text-gray-700")
            filt_in = ui.select(["None", "RRC", "Lowpass"], value="None",
                                label="Filter").props("dense outlined").classes("w-40")
            with ui.row().classes("items-end gap-2"):
                taps_in = ui.number("Filter taps", value=31, format="%d").props("dense outlined").classes("w-32")
                ui.button("Calc", on_click=lambda: calc_taps())

    with ui.row().classes("w-full justify-center gap-3"):
        ui.button("Save", on_click=lambda: save_message()).props("color=primary")
        ui.button("Show Bit String", on_click=lambda: show_bits())

    status = ui.label("Enter fields and choose modulation, symbol rate, samples per bit, and filter.") \
        .classes("text-sm text-gray-600")

    # handlers
    def add_field():
        from dpmg.encoding.dmr_template import bin_to_hex
        raw = (value_in.value or "").strip()
        if not raw:
            ui.notify("Enter a value first", type="warning")
            return
        try:
            if fmt_in.value == "Hex":
                hexv = raw[2:] if raw.lower().startswith("0x") else raw
                int(hexv, 16)                                  # validate
                binv = f"{int(hexv, 16):0{len(hexv) * 4}b}"
                hexv = hexv.upper()
            else:                                              # Binary
                binv = raw.replace(" ", "")
                if any(c not in "01" for c in binv):
                    raise ValueError("Binary field must contain only 0 and 1.")
                hexv = bin_to_hex(binv)
        except ValueError as e:
            ui.notify(f"Invalid {fmt_in.value}: {e}", type="negative") 
            return

        enc = enc_in.value
        if enc != "None":
            from dpmg.encoding.ecc_decoder import encode_field_bits
            try:
                encode_field_bits(binv, enc)
            except Exception as e:
                ui.notify(f"Invalid encoding input: {e}", type="negative")
                return

        field_rows.append({
            "id": next_id["v"],
            "bitfield": "",
            "hex": hexv,
            "bin": binv,
            "desc": (desc_custom.value if desc_in.value == "Custom" else desc_in.value),
            "enc": enc,
        })
        next_id["v"] += 1
        fields_table.rows = field_rows      
        fields_table.update()               
        value_in.value = ""
        status.set_text(f"{len(field_rows)} field(s).")

    def delete_selected(): 
        sel_ids = {r["id"] for r in fields_table.selected}
        if not sel_ids:
            ui.notify("Select one or more rows first", type="warning")
            return
        field_rows[:] = [r for r in field_rows if r["id"] not in sel_ids]
        fields_table.selected.clear()
        fields_table.rows = field_rows
        fields_table.update()
        ui.notify(f"Deleted {len(sel_ids)} field(s)")
        

    def clear_fields():
        field_rows.clear()
        fields_table.selected.clear()
        fields_table.rows = field_rows   
        fields_table.update()
        status.set_text("Fields cleared.")
        
    def _hz(num_key, unit_key):
        if num_key not in tone_els:
            return 0.0
        mult = {"Hz": 1, "kHz": 1e3, "MHz": 1e6}[tone_els[unit_key].value]
        return float(tone_els[num_key].value) * mult
        
    def _validate_signal_params(settings):
        """Return (errors, warnings). errors block generation; warnings are advisory."""
        errors, warnings = [], []
        try:
            symrate = float(settings.get("symbol_rate", 0))
            spb = int(settings.get("samples_per_bit", 0))
            mod = settings.get("modulation", "FSK")
            native_rate = symrate * spb
            nyquist = native_rate / 2.0

            # basic sanity
            if symrate <= 0:
                errors.append("Symbol rate must be positive.")
            if spb < 2:
                errors.append("Samples per bit must be at least 2.")
            if native_rate <= 0:
                errors.append("Native sample rate (symbol_rate x samples/bit) must be positive.")
                return errors, warnings   # can't check Nyquist without a rate

            # gather tones and resolve to baseband (subtract center if absolute RF)
            center = float(settings.get("center_freq_hz", 0) or 0)
            tone_keys = ("tone0", "tone1", "tone2", "tone3") if mod == "4FSK" else ("tone0", "tone1")
            tones = [float(settings.get(k, 0) or 0) for k in tone_keys]
            tones = [t for t in tones if t != 0]
            if not tones:
                # PSK is phase-keyed: the tone-settings UI never
                # creates tone fields for it, so requiring tones blocked all PSK
                # generation. spb>=2 (checked above) already bounds the PSK
                # spectrum below Nyquist, so no replacement check is needed.
                if mod not in PSK_MODS:
                    errors.append(f"{mod} requires tone values.")
            else:
                # if tones look like absolute RF near a center, use baseband deviations
                if center > 0 and all(t > 0 for t in tones) and max(abs(t - center) for t in tones) <= 5_000_000:
                    baseband = [t - center for t in tones]
                else:
                    baseband = tones
                max_bb = max(abs(b) for b in baseband)

                # Nyquist: every baseband tone must be below Fs/2
                if max_bb >= nyquist:
                    errors.append(
                        f"Tone at {max_bb:.0f} Hz baseband exceeds Nyquist ({nyquist:.0f} Hz = "
                        f"native rate {native_rate:.0f}/2). Lower the tone, raise samples/bit, "
                        f"or set a center frequency to subtract.")
                elif max_bb > 0.4 * nyquist:
                    warnings.append(
                        f"Tone at {max_bb:.0f} Hz is close to Nyquist ({nyquist:.0f} Hz); "
                        f"consider a higher sample rate for cleaner reconstruction.")

                # tone spacing vs symbol rate (advisory: affects demod quality)
                if len(baseband) >= 2:
                    spacing = min(abs(baseband[i+1]-baseband[i]) for i in range(len(baseband)-1)) \
                              if len(baseband) > 1 else 0
                    if 0 < spacing < symrate:
                        warnings.append(
                            f"Tone spacing ({spacing:.0f} Hz) is smaller than the symbol rate "
                            f"({symrate:.0f} Hz); tones may be hard to demodulate.")

            # filter taps
            taps = int(settings.get("filter_taps", 0) or 0)
            if settings.get("filter", "None") != "None":
                if taps <= 0 or taps % 2 == 0:
                    errors.append("Filter taps must be a positive odd integer.")

        except (ValueError, TypeError) as e:
            errors.append(f"Invalid parameter value: {e}")
        return errors, warnings
        
    def gen_4fsk():
        try:
            # read center and deviation from the form (with their unit selectors)
            cf_scale = {"Hz": 1.0, "kHz": 1e3, "MHz": 1e6}[tone_els["cfu"].value]
            dev_scale = {"Hz": 1.0, "kHz": 1e3, "MHz": 1e6}[tone_els["devu"].value]
            center_hz = float(tone_els["cf"].value) * cf_scale
            delta_hz = float(tone_els["dev"].value) * dev_scale
        except (KeyError, ValueError, TypeError):
            ui.notify("Set center frequency and deviation first", type="warning")
            return
        # DMR-style 4FSK: center +/- 0.5 delta (inner), center +/- 1.5 delta (outer)
        tones_hz = [
            center_hz - 1.5 * delta_hz,   # tone 0
            center_hz - 0.5 * delta_hz,   # tone 1
            center_hz + 0.5 * delta_hz,   # tone 2
            center_hz + 1.5 * delta_hz,   # tone 3
        ]
        # write back into the tone fields, converting to each field's display unit
        for key, hz in zip(("t0", "t1", "t2", "t3"), tones_hz):
            if key in tone_els:
                unit = tone_els[key + "u"].value if (key + "u") in tone_els else "kHz"
                scale = {"Hz": 1.0, "kHz": 1e-3, "MHz": 1e-6}[unit]
                tone_els[key].value = round(hz * scale, 6)
        ui.notify(f"Generated 4FSK tones (delta {delta_hz:.1f} Hz)")
        
    def _update_native_sr():
        try:
            if mod_in.value == "CSS":
                sr = int(float(tone_els["css_bw"].value) * int(float(tone_els["css_os"].value)))
            else:
                sr = int(float(sym_in.value) * int(tone_els["spb"].value))
            mxg_sr_in.value = str(sr)
            mxg_sr_in.update()
        except Exception as e:
            mxg_sr_in.value = "--"
            mxg_sr_in.update()
        
    def calc_taps():
        if "spb" not in tone_els:
            ui.notify("No samples-per-bit field for this modulation", type="warning")
            return
        spb = int(float(tone_els["spb"].value))
        taps = spb * 10 + 1
        if taps % 2 == 0:
            taps += 1
        taps_in.value = taps
        status.set_text(f"Filter taps set to {taps} ((SR/SymRate)*10+1).")
    
    def _apply_gfsk_preset():
        import re
        label = tone_els["preset"].value if "preset" in tone_els else "Custom"
        if label == "Custom":
            return
        m = re.search(r"BT\s*=\s*([0-9]*\.?[0-9]+)", label)
        if m and "bt" in tone_els:
            tone_els["bt"].value = float(m.group(1))
            
            
    def _required_min_rate(settings):
        native = float(settings.get("sample_rate", 0))       # symbol_rate * spb
        symrate = float(settings.get("symbol_rate", 0))
        # occupied bandwidth: tone spread + modulation skirts (~2x symbol rate)
        tones = [settings.get(k) for k in ("tone0", "tone1", "tone2", "tone3")]
        tones = [float(t) for t in tones if t]
        if len(tones) >= 2:
            # tones may be absolute RF; use spread relative to their own center
            spread = max(tones) - min(tones)
        else:
            spread = 0.0
        occupied = spread + 2.0 * symrate
        nyquist_need = 2.0 * occupied
        return int(max(native, nyquist_need))
    

    def _collect():
        # fields: send bin as authoritative for DMR (non-nibble lengths), else hex
        svc_fields = []
        for r in field_rows:
            binv = (r.get("bin") or "").strip()
            hexv = (r.get("hex") or "").strip()
            if binv:
                svc_fields.append({"hex": "", "bin": binv, "desc": r["desc"], "encoding": r["enc"]})
            else:
                svc_fields.append({"hex": hexv, "bin": "", "desc": r["desc"], "encoding": r["enc"]})

        spb = int(tone_els["spb"].value) if "spb" in tone_els else 1
        symrate = float(sym_in.value)
        settings = {
            "modulation": mod_in.value,
            "symbol_rate": symrate, "baud": symrate,
            "samples_per_bit": spb,
            "tone0": _hz("t0", "t0u"), "tone1": _hz("t1", "t1u"),
            "tone2": _hz("t2", "t2u"), "tone3": _hz("t3", "t3u"),
            "filter": filt_in.value,
            "filter_taps": int(taps_in.value),
            "sample_rate": int(symrate * spb),
        }
        if "cf" in tone_els:
            settings["center_freq_hz"] = _hz("cf", "cfu")
            settings["center_unit"] = tone_els["cfu"].value
        if "dev" in tone_els:
            settings["delta_hz"] = _hz("dev", "devu")
            settings["delta_unit"] = tone_els["devu"].value
        for i, k in enumerate(("t0", "t1", "t2", "t3")):
            if k + "u" in tone_els:
                settings[f"tone{i}_unit"] = tone_els[k + "u"].value
        if "map" in tone_els:
            settings["dibit_mapping"] = tone_els["map"].value
        if "bt" in tone_els:
            bt = float(tone_els["bt"].value)
            settings["gfsk_bt"] = bt
            settings["gfsk_bw_hz"] = bt * symrate
        if "sf" in tone_els:
            settings["spread_factor"] = int(tone_els["sf"].value)
            settings["pn_seed"] = int(tone_els["pn"].value)
        if "code_family" in tone_els:
            settings["dsss_modem"] = tone_els["dsss_modem"].value
            fam = tone_els["code_family"].value
            if fam == "seed":
                settings["code_spec"] = {"family": "seed",
                    "length": int(tone_els["sf"].value), "seed": int(tone_els["pn"].value)}
            elif fam == "m":
                settings["code_spec"] = {"family": "m",
                    "n": int(tone_els["code_n"].value), "seed": int(tone_els["pn"].value)}
            elif fam == "gold":
                settings["code_spec"] = {"family": "gold",
                    "n": int(tone_els["code_n"].value),
                    "index": int(tone_els["code_index"].value), "seed": int(tone_els["pn"].value)}
            else:
                settings["code_spec"] = {"family": "custom",
                    "chips": tone_els["code_chips"].value}
        if "css_sf" in tone_els:
            settings["css_sf"] = int(tone_els["css_sf"].value)
            settings["css_bw"] = float(tone_els["css_bw"].value)
            settings["css_os"] = int(tone_els["css_os"].value)
        return svc_fields, settings

    async def save_message():
        svc_fields, settings = _collect()
        errors, warnings = _validate_signal_params(settings)
        if errors:
            ui.notify("Cannot generate: " + " ".join(errors), type="negative", timeout=8000)
            return
        for w in warnings:
            ui.notify("Warning: " + w, type="warning", timeout=6000)
        native_rate = int(settings.get("sample_rate", 0))     # symbol_rate * spb 
        native_spb = int(tone_els["spb"].value) if "spb" in tone_els else None
        symrate = int(float(sym_in.value)) if sym_in.value else None
        min_rate = _required_min_rate(settings)               # native rate vs bandwidth, whichever is larger
        choice = await SaveAsDialog(start_dir=str(OUTPUT_DIR), min_rate=min_rate)
        if not choice:
            return
        directory, stem, ext, target_rate = choice
        try:
            resampled = target_rate is not None and int(target_rate)
            if target_rate is not None:
                settings["output_sample_rate"] = int(target_rate)   # resample to SDR rate
            
            # tag filename when resampled to an SDR rate
            if resampled:
                stem = f"{stem}_rs{int(target_rate)}"  # add sdr rate resampled
            
            Path(directory).mkdir(parents=True, exist_ok=True)
            out_path = str(Path(directory) / f"{stem}{ext}")
            r = encode_fields_to_bin(svc_fields, settings, out_path)
            
            # metadata: record native vs resampled
            settings["sample_rate"] = r["sample_rate"]  # real rate after resample
            settings["native_sample_rate"] = native_rate
            settings["native_samples_per_bit"] = native_spb
            if resampled:
                settings["resampled"] = True
                settings["resample_rate"] = int(target_rate)
                # sample/bit at files rate (may be fractional): rate /symbol_rate
                if symrate:
                    settings["file_samples_per_bit"] = r["sample_rate"] / float(symrate)
                else:
                    settings["resampled"] = False            
            
            _write_companion_json(r["output_path"], settings)
            ui.notify(f"Saved {r['bit_count']} bits ({r['format']}) -> {r['output_path']}")
            status.set_text(f"Saved {r['output_path']}")
        except ValueError as e:
            ui.notify(f"Encode failed: {e}", type="negative")
        except Exception as e:
            ui.notify(f"Unexpected error: {e}", type="negative")
    def _scenario_collect():
        try:
            svc_fields, settings = _collect()
            # require at least one field with bits
            if not svc_fields:
                return None
            return svc_fields, settings
        except Exception:
            return None
 


    def _write_companion_json(waveform_path, settings):
        import json
        fields_out = [{
            "bit_range": r.get("bitfield", ""),
            "hex": r.get("hex", ""),
            "bin": r.get("bin", ""),
            "desc": r.get("desc", ""),
            "encoding": r.get("enc", "None"),
        } for r in field_rows]
        doc = {"settings": settings, "fields": fields_out}
        json_path = str(Path(waveform_path).with_suffix(".json"))
        Path(json_path).write_text(json.dumps(doc, indent=2))

    def show_bits():
        from dpmg.services.encoder_service import build_bit_string
        svc_fields, settings = _collect()
        try:
            bits = build_bit_string(svc_fields, settings)
        except ValueError as e:
            ui.notify(f"Encode failed: {e}", type="negative")
            return
        with ui.dialog() as dlg, ui.card().classes("w-[720px] max-w-full"):
            ui.label(f"Bit String ({len(bits)} bits)").classes("text-lg font-bold")
            ui.label(bits).classes("font-mono text-xs break-all")
            ui.button("Close", on_click=dlg.close).props("flat")
        dlg.open()
            
    async def save_message_def():
        # save the whole message definition (fields + settings) as JSON
        import json
        svc_fields, settings = _collect()
        OUTPUT_DIR.mkdir(exist_ok=True)
        path = OUTPUT_DIR / "message.json"
        path.write_text(json.dumps({"fields": svc_fields, "settings": settings}, indent=2))
        ui.notify(f"Saved message definition -> {path}")

    async def load_message_def():
        import json
        p = await LocalFilePicker(str(OUTPUT_DIR if OUTPUT_DIR.exists() else "~"),
                                  only_ext={".json"})
        if not p:
            return
        _apply_message_json(json.loads(Path(p).read_text()))
        ui.notify(f"Loaded {Path(p).name}")

    def load_dmr_template():
        from dpmg.encoding.dmr_template import (
            build_tier3_template_fields, tier3_default_values, bin_to_hex)
        template = build_tier3_template_fields(tier3_default_values())

        rows = []
        start = 0
        for k, item in enumerate(template):
            nbits = len(item["bin"])
            end = start + nbits - 1
            rows.append({
                "id": next_id["v"] + k,
                "bitfield": f"{start}-{end}",          # running bit range
                "hex": bin_to_hex(item["bin"]),
                "bin": item["bin"],
                "desc": item["desc"],
                "enc": item["encoding"],
            })
            start = end + 1

        field_rows[:] = rows
        next_id["v"] += len(rows)
        fields_table.rows = field_rows
        fields_table.update()
        ui.notify(f"Loaded DMR template ({len(field_rows)} fields)")

    def edit_dmr_template():
        from dpmg.encoding.dmr_template import (
            build_tier3_template_fields, tier3_default_values, bin_to_hex,
            parse_payload_bytes_bits, parse_int_field)

        current = tier3_default_values()
        # 17 hex fields: (key, label, max_value): matches the Tkinter app
        spec = [
            ("color_code", "Color Code (0-15)", 15),
            ("slot_flag", "Slot Flag (0/1)", 1),
            ("cach_reserved", "CACH Reserved (0-1023)", 1023),
            ("burst_type", "Burst Type (0-15)", 15),
            ("dest_id", "Destination ID (0-16777215)", 0xFFFFFF),
            ("src_id", "Source ID (0-16777215)", 0xFFFFFF),
            ("app_id", "Application ID (0-255)", 0xFF),
            ("msg_type", "Message Type (0-255)", 0xFF),
            ("payload_word", "Payload Word (0-65535)", 0xFFFF),
            ("csbk_header", "CSBK Header (0-65535)", 0xFFFF),
            ("csbk_burst_identifier", "CSBK Burst Identifier (0-15)", 0xF),
            ("csbk_constant", "CSBK Constant (0-255)", 0xFF),
            ("csbk_counter", "CSBK Counter (0-15)", 0xF),
            ("csbk_data_crc", "CSBK Data+DataCRC (0-2^48-1)", 0xFFFFFFFFFFFF),
            ("csbk_crc", "CSBK CRC (0-65535)", 0xFFFF),
            ("csbk_sync", "CSBK Sync (48-bit, 0-2^48-1)", 0xFFFFFFFFFFFF),
            ("csbk_slot_type", "CSBK Slot Type Raw (8-bit, 0-255)", 0xFF),
        ]

        entries: dict = {}
        with ui.dialog() as dlg, ui.card().classes("w-[780px] max-w-full"):
            ui.label("DMR Template Editor").classes("text-lg font-bold")
            with ui.grid(columns=2).classes("gap-x-6 gap-y-1 w-full"):
                for key, label, _max in spec:
                    ui.label(label).classes("self-center")
                    entries[key] = ui.input(value=hex(current[key])) \
                        .props("dense outlined").on("blur", lambda: refresh())
            with ui.grid(columns=2).classes("gap-x-6 gap-y-1 w-full"):
                ui.label("Template Mode").classes("self-center")
                mode_in = ui.select(["structured80", "payload80", "csbk96", "csbk96burst"],
                                    value=current.get("tier3_mode", "structured80"),
                                    on_change=lambda: refresh()).props("dense outlined")
                ui.label("Payload Bytes Hex (optional, up to 10 bytes)").classes("self-center")
                payload_in = ui.input(value=current.get("payload_bytes", "")) \
                    .props("dense outlined").on("blur", lambda: refresh())
            ui.label("Example: 01 02 03 04 or 01020304").classes("text-xs text-gray-500")

            mode_lbl = ui.label("Mode: Structured fields").classes("text-sm")
            bits_lbl = ui.label("Data Raw Bits:").classes("font-mono text-xs break-all")
            hex_lbl = ui.label("Data Raw Hex:").classes("font-mono text-xs break-all")
            err_lbl = ui.label("").classes("text-sm text-red-600")

            with ui.row().classes("w-full justify-start gap-2"):
                apply_btn = ui.button("Apply Template", on_click=lambda: apply_())
                ui.button("Cancel", on_click=dlg.close).props("flat")

        def _collect_editor_values():
            values = {}
            for key, label, max_value in spec:
                values[key] = parse_int_field(entries[key].value, label, max_value)
            values["tier3_mode"] = (mode_in.value or "structured80").strip()
            values["payload_bytes"] = (payload_in.value or "").strip()
            if values["tier3_mode"] == "payload80":
                parse_payload_bytes_bits(values["payload_bytes"])   # validate
            return values

        def refresh():
            try:
                values = _collect_editor_values()
                template = build_tier3_template_fields(values)
                data_bits = template[3]["bin"]
                data_hex = bin_to_hex(data_bits)
                mode = {"csbk96": "CSBK-96 raw", "csbk96burst": "CSBK full burst (264-bit)",
                        "payload80": "Payload bytes"}.get(values["tier3_mode"], "Structured fields")
                mode_lbl.set_text(f"Mode: {mode}")
                bits_lbl.set_text(f"Data Raw Bits ({len(data_bits)}): {data_bits}")
                hex_lbl.set_text(f"Data Raw Hex ({len(data_hex)}): {data_hex}")
                err_lbl.set_text("")
                apply_btn.enable()
            except ValueError as exc:
                mode_lbl.set_text("Mode: Invalid input")
                bits_lbl.set_text("Data Raw Bits:")
                hex_lbl.set_text("Data Raw Hex:")
                err_lbl.set_text(f"Preview error: {exc}")
                apply_btn.disable()

        def apply_():
            try:
                values = _collect_editor_values()
                template = build_tier3_template_fields(values)
            except ValueError as exc:
                ui.notify(f"Invalid template value: {exc}", type="negative")
                return

            rows = []
            start = 0
            for k, item in enumerate(template):
                nbits = len(item["bin"])
                end = start + nbits - 1
                rows.append({
                    "id": next_id["v"] + k,
                    "bitfield": f"{start}-{end}",
                    "hex": bin_to_hex(item["bin"]),
                    "bin": item["bin"],
                    "desc": item["desc"],
                    "enc": item["encoding"],
                })
                start = end + 1

            field_rows[:] = rows
            next_id["v"] += len(rows)
            fields_table.rows = field_rows
            fields_table.update()
            dlg.close()
            ui.notify(f"Applied DMR template ({len(field_rows)} fields)")

        refresh()      # populate preview before showing
        dlg.open()

    def _apply_message_json(data: dict):
        # repopulate the Fields table from a saved/template JSON
        rows = []
        start = 0
        for k, r in enumerate(data.get("fields", [])):
            binv = r.get("bin", "")
            nbits = len(binv)
            if nbits:
                end = start + nbits - 1
                bitfield = f"{start}-{end}"
                start = end + 1
            else:
                bitfield = ""          # no bin: can't compute a range
            rows.append({
                "id": next_id["v"] + k,
                "bitfield": bitfield,
                "hex": r.get("hex", ""),
                "bin": binv,
                "desc": r.get("desc", ""),
                "enc": r.get("encoding", "None"),
            })

        field_rows[:] = rows
        next_id["v"] += len(rows)
        fields_table.rows = field_rows
        fields_table.update()

        s = data.get("settings", {})
        if "modulation" in s:
            mod_in.value = s["modulation"]
        if "symbol_rate" in s:
            sym_in.value = int(s["symbol_rate"])
        if "filter" in s:
            filt_in.value = s["filter"]
        if "filter_taps" in s:
            taps_in.value = int(s["filter_taps"])

        tone_settings.refresh()

        # The tone-card rebuild lands on the next tick, so defer writing tone
        # values until after it's rebuilt: otherwise the rebuild wipes them.
        def _restore_tones():
            def _set_tone(key, hz_val, unit_key):
                if key in tone_els and hz_val is not None:
                    unit = s.get(unit_key, "kHz")
                    if key + "u" in tone_els:
                        tone_els[key + "u"].value = unit if unit in ("Hz", "kHz", "MHz") else "kHz"
                    scale = {"Hz": 1.0, "kHz": 1e-3, "MHz": 1e-6}[tone_els[key + "u"].value]
                    tone_els[key].value = round(float(hz_val) * scale, 6)

            if "spb" in tone_els and "samples_per_bit" in s:
                tone_els["spb"].value = int(s["samples_per_bit"])
            _set_tone("t0", s.get("tone0"), "tone0_unit")
            _set_tone("t1", s.get("tone1"), "tone1_unit")
            _set_tone("t2", s.get("tone2"), "tone2_unit")
            _set_tone("t3", s.get("tone3"), "tone3_unit")
            if "cf" in tone_els and "center_freq_hz" in s:
                tone_els["cfu"].value = s.get("center_unit", "kHz")
                scale = {"Hz": 1.0, "kHz": 1e-3, "MHz": 1e-6}[tone_els["cfu"].value]
                tone_els["cf"].value = round(float(s["center_freq_hz"]) * scale, 6)
            if "dev" in tone_els and "delta_hz" in s:
                tone_els["devu"].value = s.get("delta_unit", "kHz")
                scale = {"Hz": 1.0, "kHz": 1e-3, "MHz": 1e-6}[tone_els["devu"].value]
                tone_els["dev"].value = round(float(s["delta_hz"]) * scale, 6)
            if "bt" in tone_els and "gfsk_bt" in s:
                tone_els["bt"].value = float(s["gfsk_bt"])
            if "bw" in tone_els and "gfsk_bw_hz" in s:
                tone_els["bw"].value = float(s["gfsk_bw_hz"])
            if "map" in tone_els and "dibit_mapping" in s:
                tone_els["map"].value = s["dibit_mapping"]

        ui.timer(0.05, _restore_tones, once=True)
        ui.timer(0.3, _update_native_sr, once=True)  
    # Secure messaging controls.
    # The toggle seals the authored message (AES-256-GCM-SIV) when it is added to a
    # scenario; the key lives in DPMG_KEY_FILE on this host. Everything is greyed
    # out and inert while DPMG_SECURE_MSG is off on the GUI host.
    secure_ui = {"on": False}
    with section("Secure messaging (AES-256-GCM-SIV, 128-bit tag)"):
        from dpmg.secure import api as _sfm
        _flag = _sfm.secure_msg_enabled()
        with ui.row().classes("items-center gap-4 flex-wrap"):
            sec_sw = ui.switch("Seal scenario messages", value=False,
                               on_change=lambda e: secure_ui.__setitem__("on", bool(e.value)))
            sec_state = ui.label("").classes("font-mono text-xs")
            sec_key = ui.label("").classes("font-mono text-xs")
        sec_env = ui.label("").classes("text-xs text-gray-500")
        sec_cap = ui.label("").classes("text-xs text-gray-500")

        def _sec_refresh():
            if not _flag:
                sec_sw.disable()
                sec_state.set_text(f"OFF: set {_sfm.FLAG_SECURE_MSG}=1 (and {_sfm.ENV_KEY_FILE}) on the GUI host")
                sec_env.set_text("")
                sec_cap.set_text("")
                return
            ctx = _sfm.get_context(create=True, tx=True)
            if ctx is None:
                sec_sw.disable()
                sec_state.set_text("FAULT: " + (_sfm.last_error() or "no key loaded")[:90])
                sec_state.classes(replace="font-mono text-xs text-red-600")
                sec_key.set_text("")
            else:
                sec_sw.enable()
                st = ctx.store.status()
                _ons = ctx.on_air_suite()
                sec_state.set_text(f"ready, {_sfm.AEAD_NAME}, nonce {_sfm.NONCE_NAME} "
                                   f"(+{_sfm.overhead_bits(_ons)} bits/frame, "
                                   f"+{_sfm.overhead_bits(_ons, long_header=True)} with the long header)")
                sec_state.classes(replace="font-mono text-xs text-green-700")
                sec_key.set_text(f"key {st['key_id']}  kcv {st['kcv']}  suite {st['suite']}  "
                                 f"net {st['net_id']} sender {st['sender_id']}  "
                                 f"sealed {ctx.counters.get('sealed', 0)} refused {ctx.counters.get('seal_refused', 0)}")
            if "env" not in secure_ui:                              # probe + KAT once per page
                secure_ui["env"] = _sfm.probe(run_selftest=True)
            sec_env.set_text(secure_ui["env"].summary())
            _suite = ctx.on_air_suite() if ctx is not None else _sfm.SUITE_GCMSIV128
            caps = ", ".join(f"{k} {v} B" for k, v in _sfm.capacity_table(_suite, long_header=True).items()
                             if v is not None)
            sec_cap.set_text(f"max plaintext per frame ({_sfm.SUITE_NAME.get(_suite, _suite)}): {caps}; "
                             f"cannot carry the tag: "
                             f"{', '.join(_sfm.unsupported_codings(_suite, long_header=True)) or 'none'}")
        try:
            _sec_refresh()
        except Exception as _se:                                   # never break the Encoder tab
            sec_state.set_text(f"status unavailable: {_se}")
        ui.timer(5.0, _sec_refresh)

    # multi-emitter scenario authoring
    def _scenario_collect():
        try:
            svc_fields, settings = _collect()
            # attach an optional descriptor derived from the encoder's fields
            try:
                from dpmg.framing import frame_integration as _fi
                if svc_fields:
                    settings['frame_descriptor'] = _fi.descriptor_from_encoder_fields(svc_fields, settings)
            except Exception as _de:
                settings['frame_descriptor'] = None
                print('[frame] derive skipped:', _de, flush=True)
            if secure_ui["on"]:
                settings["secure"] = {"on": True}
            return (svc_fields, settings) if svc_fields else None
        except Exception:
            return None
    try:
        with section("Scenario Builder"):
            build_scenario_section(ui, get_current_waveform=_scenario_collect,
                                   default_center_hz=425_025_000.0)
    except Exception as _se:
        print("[scenario] build_scenario_section failed:", _se, flush=True)

    with section("IQ Resampler"):
        build_resampler_section()

# TAB 2: IQ VALIDATOR

def iq_validator_tab():
    last_result = {"data": None}      # holds the most recent validate_iq_with_plots result

    with section("Input files"):
        with ui.row().classes("w-full items-center gap-4"):
            ui.label("IQ file:").classes("w-32")
            iq_lbl = ui.label("(not loaded)").classes("grow text-gray-500")
            ui.button("Load BIN", on_click=lambda: load_bin()).classes("w-48")
        with ui.row().classes("w-full items-center gap-4"):
            ui.label("Expected JSON:").classes("w-32")
            json_lbl = ui.label("(not loaded)").classes("grow text-gray-500")
            ui.button("Load JSON", on_click=lambda: load_json()).classes("w-48")

    with section("Demodulation settings"):
        demod_els: dict = {}
        with ui.grid(columns=2).classes("w-full gap-x-8"):
            mod_in = ui.select(["FSK", "4FSK", "BPSK", "QPSK", f"{PI}/4-QPSK", "DQPSK", "OQPSK", "GFSK", "8PSK", "8QAM", "16QAM", "16APSK", "32APSK", "BPSK-DSSS", "QPSK-DSSS", "CSS"],
                               value="FSK", label="Modulation",
                               on_change=lambda: demod_fields.refresh()).props("dense outlined")
            sr_in = ui.input("Sample rate (Hz)", value="44400").props("dense outlined")
            symrate_in = ui.number("Symbol rate", value=1200, format="%d").props("dense outlined")
            spb_in = ui.number("Samples/bit", value=37, format="%.2f").props("dense outlined")
            siglen_in = ui.input("Signal time length", value="--").props("dense outlined readonly")

        VAL_PSK_MODS = {"BPSK", "QPSK", f"{PI}/4-QPSK", "DQPSK", "OQPSK",
                        "8PSK", "8QAM", "16QAM", "16APSK", "32APSK"}

        @ui.refreshable
        def demod_fields():
            demod_els.clear()
            mod = mod_in.value
            if mod in VAL_PSK_MODS:
                with ui.grid(columns=2).classes("w-full gap-x-8"):
                    demod_els["cf"] = ui.number("Center freq (kHz)", value=425025).props("dense outlined")
            elif mod == "4FSK":
                with ui.grid(columns=2).classes("w-full gap-x-8"):
                    demod_els["t0"] = ui.number("Tone 0 (kHz)", value=1.2).props("dense outlined")
                    demod_els["t1"] = ui.number("Tone 1 (kHz)", value=2.2).props("dense outlined")
                    demod_els["t2"] = ui.number("Tone 2 (kHz)", value=1.4).props("dense outlined")
                    demod_els["t3"] = ui.number("Tone 3 (kHz)", value=2.6).props("dense outlined")
                    demod_els["map"] = ui.select(["dmr", "dmr-inverted", "binary", "gray"], value="dmr",
                                                 label="Dibit mapping").props("dense outlined")
            elif mod == "GFSK":
                with ui.grid(columns=2).classes("w-full gap-x-8"):
                    demod_els["t0"] = ui.number("Tone 0 (kHz)", value=1.2).props("dense outlined")
                    demod_els["t1"] = ui.number("Tone 1 (kHz)", value=2.2).props("dense outlined")
                    demod_els["gfsk_bw"] = ui.number("GFSK BW (Hz)", value=250, format="%d").props("dense outlined")
            elif mod in ("BPSK-DSSS", "DSSS", "QPSK-DSSS"):
                with ui.grid(columns=2).classes("w-full gap-x-8"):
                    demod_els["sf"] = ui.number("Spread factor (chips/bit)", value=31, format="%d").props("dense outlined")
                    demod_els["dsss_modem"] = ui.select(["bpsk", "qpsk"],
                        value=("qpsk" if mod == "QPSK-DSSS" else "bpsk"),
                        label="Spread symbol").props("dense outlined")
                with ui.grid(columns=2).classes("w-full gap-x-8"):
                    demod_els["code_family"] = ui.select(["seed", "m", "gold", "custom"], value="seed",
                        label="Spreading code").props("dense outlined")
                    demod_els["code_n"] = ui.number("n (m/gold)", value=7, format="%d").props("dense outlined")
                    demod_els["code_index"] = ui.number("Gold index", value=0, format="%d").props("dense outlined")
                    demod_els["pn"] = ui.number("Seed", value=1, format="%d").props("dense outlined")
                demod_els["code_chips"] = ui.input("Custom chips (+/-/1/0)", value="").props("dense outlined").classes("w-full")
            elif mod == "CSS":
                with ui.grid(columns=2).classes("w-full gap-x-8"):
                    demod_els["css_sf"] = ui.number("Spreading factor (bits/sym)", value=7, format="%d").props("dense outlined")
                    demod_els["css_bw"] = ui.number("Bandwidth (Hz)", value=125000, format="%d").props("dense outlined")
                    demod_els["css_os"] = ui.number("Oversample", value=1, format="%d").props("dense outlined")
            else:   # FSK / 2FSK
                with ui.grid(columns=2).classes("w-full gap-x-8"):
                    demod_els["t0"] = ui.number("Tone 0 (kHz)", value=1.2).props("dense outlined")
                    demod_els["t1"] = ui.number("Tone 1 (kHz)", value=2.2).props("dense outlined")

        demod_fields()   

    with ui.row().classes("w-full gap-2"):
        ui.button("Validate IQ", on_click=lambda: validate_iq()).classes("grow").props("color=primary")
        ui.button("Show decoded bits", on_click=lambda: show_decoded_bits()).classes("grow")

    with section("Field validation results"):
        with ui.tabs() as res_tabs:
            ui.tab("Field Validation")
            ui.tab("Encoded Symbols")
        with ui.tab_panels(res_tabs, value="Field Validation").classes("w-full"):
            with ui.tab_panel("Field Validation"):
                columns = [
                    {"name": "n", "label": "#", "field": "n", "align": "left"},
                    {"name": "desc", "label": "Description", "field": "desc", "align": "left"},
                    {"name": "exp_hex", "label": "Expected Hex", "field": "exp_hex", "align": "left"},
                    {"name": "dec_hex", "label": "Decoded Hex", "field": "dec_hex", "align": "left"},
                    {"name": "dec_val", "label": "Decoded Value (Hex)", "field": "dec_val", "align": "left"},
                    {"name": "exp_bits", "label": "Expected Bits", "field": "exp_bits", "align": "left"},
                    {"name": "dec_bits", "label": "Decoded Bits", "field": "dec_bits", "align": "left"},
                    {"name": "status", "label": "Status", "field": "status", "align": "left"},
                    {"name": "reason", "label": "Reason", "field": "reason", "align": "left"},
                ]
                results_table = ui.table(columns=columns, rows=[], row_key="n") \
                    .classes("w-full").props("dense")
            with ui.tab_panel("Encoded Symbols"):
                sym_columns = [
                    {"name": "idx", "label": "#", "field": "idx", "align": "left"},
                    {"name": "exp_sym", "label": "Expected Bits", "field": "exp_sym", "align": "left"},
                    {"name": "dec_sym", "label": "Decoded Bits", "field": "dec_sym", "align": "left"},
                    {"name": "exp_freq", "label": "Expected Freq (Hz)", "field": "exp_freq", "align": "left"},
                    {"name": "dec_freq", "label": "Decoded Freq (Hz)", "field": "dec_freq", "align": "left"},
                    {"name": "match", "label": "Match", "field": "match", "align": "left"},
                ]
                symbols_table = ui.table(columns=sym_columns, rows=[], row_key="idx") \
                    .classes("w-full").props("dense")

    status = ui.label("Load an IQ file and expected JSON, then validate.").classes("text-sm text-gray-600")

    with section("Signal Analysis"):
        with ui.tabs() as sig_tabs:
            ui.tab("FFT")
            ui.tab("Symbol Spectrum")
            ui.tab("Symbol Timing")
        with ui.tab_panels(sig_tabs, value="FFT").classes("w-full"):
            with ui.tab_panel("FFT"):
                fft_chart = ui.echart({
                    "tooltip": {"trigger": "axis"},
                    "grid": {"left": 60, "right": 20, "top": 30, "bottom": 50},
                    "xAxis": {"type": "value", "name": "Frequency (Hz)", "scale": True,
                              "nameLocation": "middle", "nameGap": 30},
                    "yAxis": {"type": "value", "name": "Magnitude (dB)", "scale": True,
                              "nameLocation": "middle", "nameGap": 45},
                    "dataZoom": [
                        {"type": "inside", "xAxisIndex": 0, "filterMode": "none"},
                        {"type": "slider", "xAxisIndex": 0, "height": 18, "bottom": 4},
                        {"type": "inside", "yAxisIndex": 0, "filterMode": "none"},
                    ],
                    "series": [{"type": "line", "showSymbol": False, "data": []}],
                }).classes("w-full h-72")
            with ui.tab_panel("Symbol Spectrum"):
                symspec_chart = ui.echart({
                    "tooltip": {"trigger": "axis"},
                    "grid": {"left": 60, "right": 20, "top": 30, "bottom": 50},
                    "xAxis": {"type": "value", "name": "Frequency (Hz)", "scale": True,
                              "nameLocation": "middle", "nameGap": 30},
                    "yAxis": {"type": "value", "name": "Power (dB)", "scale": True,
                              "nameLocation": "middle", "nameGap": 45},
                    "dataZoom": [
                        {"type": "inside", "xAxisIndex": 0, "filterMode": "none"},
                        {"type": "slider", "xAxisIndex": 0, "height": 18, "bottom": 4},
                    ],
                    "series": [{"type": "line", "showSymbol": False, "data": []}],
                }).classes("w-full h-72")
            with ui.tab_panel("Symbol Timing"):
                symtime_chart = ui.echart({
                    "tooltip": {"trigger": "axis"},
                    "legend": {"data": ["Expected", "Decoded"]},
                    "grid": {"left": 70, "right": 20, "top": 40, "bottom": 50},
                    "xAxis": {"type": "value", "name": "Symbol #",
                              "nameLocation": "middle", "nameGap": 30},
                    "yAxis": {"type": "value", "name": "Frequency (Hz)", "scale": True,
                              "nameLocation": "middle", "nameGap": 55},
                    "series": [
                        {"name": "Expected", "type": "line", "showSymbol": False,
                         "lineStyle": {"width": 3, "color": "#5470c6"}, "z": 1, "data": []},
                        {"name": "Decoded", "type": "scatter", "symbolSize": 8,
                         "itemStyle": {"color": "#e07a3f"}, "z": 2, "data": []},
                    ],
                }).classes("w-full h-72")

        with ui.row().classes("w-full items-end gap-4 flex-wrap"):
            window_in = ui.select(["hamming", "hann", "blackman", "rectangular"], value="hamming",
                                  label="FFT Window",
                                  on_change=lambda: validate_iq() if last_result["data"] else None
                                  ).props("dense outlined").classes("w-40")
            fftsize_in = ui.select(["4096", "8192", "16384", "32768", "65536", "131072"], value="65536",
                                   label="FFT Size",
                                   on_change=lambda: validate_iq() if last_result["data"] else None
                                   ).props("dense outlined").classes("w-40")
            ui.label("Mouse wheel: zoom | Double-click: reset").classes("text-xs text-gray-500 self-center")
        ui.label("Run validation to compute FFT from the demodulated signal.").classes("text-sm text-gray-600")

    # handlers
    async def load_bin():
        p = await LocalFilePicker(str(OUTPUT_DIR if OUTPUT_DIR.exists() else "~"),
                                  only_ext={".bin", ".x40iq", ".x40qi", ".g40iq", ".g40qi"})
        if p:
            iq_lbl.set_text(p)
            iq_lbl.classes(replace="grow text-gray-800")
            _update_siglen()
            
    async def load_json():
        p = await LocalFilePicker(str(OUTPUT_DIR if OUTPUT_DIR.exists() else "~"), only_ext={".json"})
        if not p:
            return
        json_lbl.set_text(p)
        json_lbl.classes(replace="grow text-gray-800")
        _apply_demod_settings(p)      # set modulation + demod fields from the JSON
        _update_siglen()

    def _apply_demod_settings(json_path):
        try:
            from dpmg.ui.data_validation import _load_expected_json_for_batch
            settings, _ = _load_expected_json_for_batch(json_path)
        except Exception as e:
            ui.notify(f"Could not read JSON settings: {e}", type="warning")
            return

        # static fields (always present): set directly
        if settings.get("modulation"):
            mod_in.value = settings["modulation"]
        if "sample_rate" in settings:
            sr_in.value = str(int(settings["sample_rate"]))
            sr_in.update()
        if "symbol_rate" in settings:
            symrate_in.value = int(settings["symbol_rate"])
        if "samples_per_bit" in settings:
            spb_in.value = int(settings["samples_per_bit"])

        # rebuild the demod card for the (possibly new) modulation
        demod_fields.refresh()

        # the rebuild lands next tick, so defer writing the conditional fields
        def _restore_demod():
            def khz(hz):                       # demod tone fields are in kHz
                return round(float(hz) / 1000.0, 6)
            if "t0" in demod_els and "tone0" in settings:
                demod_els["t0"].value = khz(settings["tone0"])
            if "t1" in demod_els and "tone1" in settings:
                demod_els["t1"].value = khz(settings["tone1"])
            if "t2" in demod_els and "tone2" in settings:
                demod_els["t2"].value = khz(settings["tone2"])
            if "t3" in demod_els and "tone3" in settings:
                demod_els["t3"].value = khz(settings["tone3"])
            if "cf" in demod_els and "center_freq_hz" in settings:
                demod_els["cf"].value = khz(settings["center_freq_hz"])
            if "gfsk_bw" in demod_els and "gfsk_bw_hz" in settings:
                demod_els["gfsk_bw"].value = float(settings["gfsk_bw_hz"])
            if "map" in demod_els and "dibit_mapping" in settings:
                demod_els["map"].value = settings["dibit_mapping"]

        ui.timer(0.05, _restore_demod, once=True)
            
    def _update_siglen():
        if iq_lbl.text in ("(not loaded)", ""):
            return
        try:
            import struct
            from pathlib import Path as _P
            from dpmg.ui.data_validation import read_iq_file
            data = _P(iq_lbl.text).read_bytes()
            rate = None
            if data[:4] == b"DPMG":
                _m, _v, _f, _r, hdr_sr, _hsize = struct.unpack(">4sBBHII", data[:16])
                rate = float(hdr_sr)
                sr_in.value = str(int(hdr_sr)); sr_in.update()
            elif json_lbl.text not in ("(not loaded)", ""):
                from dpmg.ui.data_validation import _load_expected_json_for_batch
                settings, _ = _load_expected_json_for_batch(json_lbl.text)
                rate = float(settings.get("sample_rate", 0)) or None
            n = len(read_iq_file(iq_lbl.text))
            if rate:
                siglen_in.value = f"{n/rate*1e3:.3f} ms"
            else:
                siglen_in.value = "-- (load JSON for rate)"
            siglen_in.update()
        except Exception as e:
            ui.notify(f"Could not read IQ: {e}", type="warning")

    def validate_iq():
        if iq_lbl.text == "(not loaded)" or json_lbl.text == "(not loaded)":
            ui.notify("Load both an IQ file and the expected JSON first", type="warning")
            return

        def khz_to_hz(key):
            if key in demod_els:
                try:
                    return float(demod_els[key].value) * 1000.0
                except (TypeError, ValueError):
                    return None
            return None

        _code_spec = None
        if "code_family" in demod_els:
            _fam = demod_els["code_family"].value
            _seed = int(demod_els["pn"].value) if "pn" in demod_els else 1
            if _fam == "m":
                _code_spec = {"family": "m", "n": int(demod_els["code_n"].value), "seed": _seed}
            elif _fam == "gold":
                _code_spec = {"family": "gold", "n": int(demod_els["code_n"].value),
                              "index": int(demod_els["code_index"].value), "seed": _seed}
            elif _fam == "custom":
                _code_spec = {"family": "custom", "chips": demod_els["code_chips"].value}
            else:
                _code_spec = {"family": "seed",
                              "length": int(demod_els["sf"].value) if "sf" in demod_els else 31,
                              "seed": _seed}

        overrides = {
            "modulation": mod_in.value,
            "sample_rate": float(sr_in.value) if sr_in.value else None,
            "symbol_rate": float(symrate_in.value),
            "samples_per_bit": int(spb_in.value),
            "tone0": khz_to_hz("t0"),
            "tone1": khz_to_hz("t1"),
            "tone2": khz_to_hz("t2"),
            "tone3": khz_to_hz("t3"),
            "gfsk_bw_hz": float(demod_els["gfsk_bw"].value) if "gfsk_bw" in demod_els else None,
            "spread_factor": int(demod_els["sf"].value) if "sf" in demod_els else None,
            "pn_seed": int(demod_els["pn"].value) if "pn" in demod_els else None,
            "css_sf": int(demod_els["css_sf"].value) if "css_sf" in demod_els else None,
            "css_bw": float(demod_els["css_bw"].value) if "css_bw" in demod_els else None,
            "css_os": int(demod_els["css_os"].value) if "css_os" in demod_els else None,
            "code_spec": _code_spec,
            "dsss_modem": demod_els["dsss_modem"].value if "dsss_modem" in demod_els else None,
            "fft_window": window_in.value,                    
            "fft_size": int(fftsize_in.value),                
        }
        overrides = {k: v for k, v in overrides.items() if v is not None}

        try:
            from dpmg.services.validator_service import validate_iq_with_plots
            result = validate_iq_with_plots(iq_lbl.text, json_lbl.text, overrides)
            last_result["data"] = result 
            # populate the Encoded Symbols tab from per-symbol timing points
            _update_symbols_table(result.get("plots", {}))
        except Exception as e:
            ui.notify(f"Validation failed: {e}", type="negative")
            return
            
        # update samples/bit to the effective value the demod used (rate / symbol_rate)
        try:
            eff_sr = float(sr_in.value) if sr_in.value else 0
            eff_symrate = float(symrate_in.value) if symrate_in.value else 0
            if eff_sr > 0 and eff_symrate > 0:
                eff_spb = eff_sr / eff_symrate
                json_spb = int(spb_in.value) if spb_in.value else None
                if json_spb is None or abs(eff_spb - json_spb) > 0.01:
                    spb_in.value = round(eff_spb, 2)        # show fractional effective spb
                    spb_in.update()
                    ui.notify(f"Samples/bit updated to effective {eff_spb:.2f} "
                              f"(file rate {eff_sr:.0f} / {eff_symrate:.0f} baud)", timeout=5000)
        except (ValueError, TypeError):
            pass

        def bits_to_hex(b):
            if not b:
                return ""
            pad = (-len(b)) % 4
            return f"{int('0'*pad + b, 2):0{(len(b)+pad)//4}X}"

        rows = []
        for n, f in enumerate(result.get("results", [])):
            exp_bits = f.get("expected_bits", "")
            dec_bits = f.get("decoded_bits", "")
            matched = f.get("match", False)
            rows.append({
                "n": n,
                "desc": f.get("desc", ""),
                "exp_hex": bits_to_hex(exp_bits),
                "dec_hex": bits_to_hex(dec_bits),
                "dec_val": bits_to_hex(dec_bits),
                "exp_bits": exp_bits,
                "dec_bits": dec_bits,
                "status": "PASS" if matched else "FAIL",
                "reason": "" if matched else (
                    f"len {f.get('decoded_len','?')}!={f.get('expected_len','?')}"
                    if f.get("decoded_len") != f.get("expected_len") else "bit mismatch"),
            })
        results_table.rows = rows
        results_table.update()

        # IQV-02: same key the service returns ("pass"); the verdict is derived
        # from the field rows, so this line always agrees with the table above
        passed = bool(result.get("pass", result.get("passed", False)))
        mf = result.get("matched_fields", 0)
        fc = result.get("field_count", 0)
        status.set_text(f"{'PASS' if passed else 'FAIL'} - {mf}/{fc} fields matched")
        ui.notify(f"Validation complete: {mf}/{fc} fields matched",
                  type="positive" if passed else "warning")

        _update_plots(result.get("plots", {}))

    def _update_plots(plots):
        # FFT
        fft = plots.get("fft", {})
        f = fft.get("frequencies_hz", [])
        m = fft.get("magnitude_db", [])
        fft_chart.options["series"][0]["data"] = [[float(x), float(y)] for x, y in zip(f, m)]
        tones = fft.get("expected_tones_hz", [])
        if tones:
            fft_chart.options["series"][0]["markLine"] = {
                "symbol": "none", "silent": True,
                "data": [{"xAxis": float(t)} for t in tones],
                "lineStyle": {"color": "#e07a3f", "type": "dashed"},
            }
        fft_chart.update()

        # Symbol Spectrum (frequency histogram of demodulated symbols)
        ss = plots.get("symbol_spectrum", {})
        sf = ss.get("frequencies_hz", [])
        sp = ss.get("power_db", [])
        symspec_chart.options["series"][0]["data"] = [[float(x), float(y)] for x, y in zip(sf, sp)]
        ss_tones = ss.get("expected_tones_hz", [])
        if ss_tones:
            symspec_chart.options["series"][0]["markLine"] = {
                "symbol": "none", "silent": True,
                "data": [{"xAxis": float(t)} for t in ss_tones],
                "lineStyle": {"color": "#e07a3f", "type": "dashed"},
            }
        symspec_chart.update()

        # Symbol Timing (expected vs decoded freq per symbol index)
        st = plots.get("symbol_timing", {})
        points = st.get("points", [])
        exp_data, dec_data = [], []
        for p in points:
            idx = p.get("index")
            ef = p.get("expected_freq_hz")
            df = p.get("decoded_freq_hz")
            if idx is not None and ef is not None:
                exp_data.append([idx, float(ef)])
            if idx is not None and df is not None:
                dec_data.append([idx, float(df)])
        symtime_chart.options["series"][0]["data"] = exp_data
        symtime_chart.options["series"][1]["data"] = dec_data
        symtime_chart.update()
        
    def _update_symbols_table(plots):
        points = plots.get("symbol_timing", {}).get("points", [])
        rows = []
        for p in points:
            exp = p.get("expected_bits", "")
            dec = p.get("decoded_bits", "")
            ef = p.get("expected_freq_hz")
            df = p.get("decoded_freq_hz")
            matched = (exp == dec) and exp != ""
            rows.append({
                "idx": p.get("index", 0),
                "exp_sym": exp or "--",
                "dec_sym": dec or "--",
                "exp_freq": f"{ef:.0f}" if ef is not None else "--",
                "dec_freq": f"{df:.0f}" if df is not None else "--",
                "match": "yes" if matched else "no",
            })
        symbols_table.rows = rows
        symbols_table.update()
        
    def show_decoded_bits():          
        result = last_result["data"]
        if result is None:
            ui.notify("Run Validate IQ first to decode the signal.", type="warning")
            return
        all_decoded = "".join(r.get("decoded_bits", "") for r in result.get("results", []))
        all_expected = "".join(r.get("expected_bits", "") for r in result.get("results", []))
        with ui.dialog() as dlg, ui.card().classes("w-[700px] max-w-full"):
            ui.label("Decoded bits").classes("text-lg font-bold")
            ui.label(f"{len(all_decoded)} decoded / {len(all_expected)} expected").classes("text-sm text-gray-600")
            with ui.column().classes("w-full gap-2"):
                ui.label("Decoded:").classes("text-xs font-bold text-gray-500")
                ui.label(all_decoded or "(none)").classes("font-mono text-xs break-all")
                ui.label("Expected:").classes("text-xs font-bold text-gray-500")
                ui.label(all_expected or "(none)").classes("font-mono text-xs break-all")
                # Sealed-frame inspection (header fields + verdict; the
                # plaintext appears only when this host holds the key and the tag
                # verifies: a failed frame shows its verdict, never its bytes)
                try:
                    from dpmg.secure import api as _sfv
                    if _sfv.secure_msg_enabled():
                        ui.label("Secure frames:").classes("text-xs font-bold text-gray-500")
                        found = _sfv.inspect_bits(all_decoded)
                        if not found:
                            ui.label("none (no CRC-valid sealed frame in the decoded bits)") \
                                .classes("font-mono text-xs text-gray-500")
                        for f in found:
                            h = f.get("header", {})
                            ui.label(f"@{f.get('offset')}  {f.get('verdict')}  {f.get('detail', '')}  "
                                     f"sender {h.get('sender')} gen_lsb {h.get('gen_lsb')} slot_lsb {h.get('slot_lsb')} "
                                     f"ctr {h.get('counter')} suite {h.get('suite')} epoch {h.get('epoch')} "
                                     f"{'long kid ' + str(h.get('kid')) if h.get('long') else 'short'}  "
                                     f"ct {f.get('ciphertext_bytes')} B" +
                                     (f"  -> {f['message']!r}" if f.get("message") is not None else "")) \
                                .classes("font-mono text-xs break-all " +
                                         ("text-green-700" if f.get("verdict") == "AUTH_OK" else "text-red-600"))
                except Exception as _se:                           # noqa: BLE001
                    ui.label(f"secure inspection unavailable: {_se}").classes("text-xs text-gray-500")
            with ui.row().classes("w-full justify-end"):
                ui.button("Close", on_click=dlg.close).props("flat")
        dlg.open()
        
    

# TAB 3: MXG DEPLOY

def mxg_tab():
    with ui.row().classes("w-full gap-4 items-stretch"):
        with ui.card().classes("grow"):
            ui.label("MXG connection").classes("text-base font-bold text-gray-700")
            with ui.grid(columns=2).classes("gap-x-6"):
                host_in = ui.input("Host", value="172.16.0.14").props("dense outlined")
                scpi_in = ui.input("SCPI port", value="5025").props("dense outlined")
                ftpuser_in = ui.input("FTP user", value="anonymous").props("dense outlined")
                ftppass_in = ui.input("FTP password", password=True).props("dense outlined")
                ftpport_in = ui.input("FTP port", value="21").props("dense outlined")
                remote_in = ui.input("Remote dir", value="/USER/BBG/WAVEFORM/").props("dense outlined")

        with ui.card().classes("grow"):
            ui.label("Waveform deployment").classes("text-base font-bold text-gray-700")
            with ui.row().classes("w-full items-end gap-2"):
                # the container's /app/output on Linux; on a Windows console the
                # same file name under this run's output folder
                localbin_in = ui.input("Local .bin",
                                       value=str(OUTPUT_DIR / "dmr_template_4FSK_9600_RRC65taps.bin")
                                       if _sys.platform == "win32"
                                       else "/app/output/dmr_template_4FSK_9600_RRC65taps.bin") \
                    .props("dense outlined").classes("grow")
            with ui.row().classes("gap-2"):
                ui.button("Browse", on_click=lambda: browse_bin())
                ui.button("Latest .bin", on_click=lambda: latest_bin())
                ui.button("Read JSON", on_click=lambda: ui.notify("Read JSON"))
                ui.button("Query *IDN?", on_click=lambda: query_idn())
            with ui.grid(columns=2).classes("gap-x-6 w-full"):
                wfref_in = ui.input("Waveform ref",
                                    value='BBG:WAVEFORM:dmr_template_4FSK_9600_RRC65taps.bin').props("dense outlined")
                samprate_in = ui.input("Sample rate").props("dense outlined")
                freq_in = ui.input("Frequency (Hz)", value="2000000000").props("dense outlined")
                ampl_in = ui.input("Amplitude (dBm)", value="-60").props("dense outlined")
            with ui.card().classes("w-full bg-gray-50"):
                ui.label("Analyzer helper").classes("font-semibold text-gray-600")
                with ui.grid(columns=2).classes("gap-x-6 w-full"):
                    ui.input("Mod").props("dense outlined")
                    ui.input("Center (Hz)").props("dense outlined")
                    ui.input("Tones (Hz)").props("dense outlined")
                    ui.input("Freq Step (Hz)").props("dense outlined")
                    ui.input("Sync").props("dense outlined")
                ui.label("Load JSON to populate helper values.").classes("text-xs text-gray-500")

    with section("SCPI sequence"):
        with ui.row().classes("w-full items-end gap-2"):
            with ui.column().classes("grow gap-2"):
                loadcmd_in = ui.input("Load cmd").props("dense outlined").classes("w-full")
                selcmd_in = ui.input("Select cmd", value='SOUR:RAD:ARB:WAV "{remote_path}"') \
                    .props("dense outlined").classes("w-full")
            ui.button("Refresh Preview", on_click=lambda: ui.notify("Refresh Preview"))

    with section("MXG control panel"):
        with ui.row().classes("w-full items-end gap-2"):
            wf_in = ui.input("Waveform", value="dmr_template_4FSK_9600_RRC65taps.bin") \
                .props("dense outlined").classes("grow")
            ui.button("Select", on_click=lambda: ui.notify(f"Select {wf_in.value}"))
        with ui.card().classes("w-full bg-gray-50"):
            ui.label("Quick controls").classes("font-semibold text-gray-600")
            with ui.row().classes("w-full items-end gap-2 flex-wrap"):
                arbsr_in = ui.input("ARB sample rate (Sa/s)").props("dense outlined").classes("grow")
                ui.button("Set SR", on_click=lambda: ui.notify(f"Set SR {arbsr_in.value}"))
                ui.button("Set RF Freq/Pwr", on_click=lambda: ui.notify("Set RF Freq/Pwr"))
                ui.button("Refresh Status", on_click=lambda: ui.notify("Refresh Status"))
                ui.button("List Waveforms", on_click=lambda: scpi_log.push("> :MMEM:CAT? ..."))
            with ui.row().classes("gap-2"):
                ui.button("ARB ON", on_click=lambda: scpi_send(":SOUR:RAD:ARB:STAT ON"))
                ui.button("ARB OFF", on_click=lambda: scpi_send(":SOUR:RAD:ARB:STAT OFF"))
                ui.button("RF ON", on_click=lambda: scpi_send(":OUTP:STAT ON")).props("color=positive")
                ui.button("RF OFF", on_click=lambda: scpi_send(":OUTP:STAT OFF")).props("color=negative")

    with section("Status / command log"):
        scpi_log = ui.log(max_lines=200).classes("w-full h-40 font-mono text-xs")
        scpi_log.push("*OPC?")
        scpi_log.push("SYST:ERR?")

    with ui.row().classes("w-full justify-center gap-3"):
        ui.button("Upload .bin", on_click=lambda: upload_bin())
        ui.button("Upload + RF On", on_click=lambda: upload_rf_on()).props("color=primary")
        ui.button("Preview Commands", on_click=lambda: preview_cmds())

    ui.label("Select a generated .bin file and configure the MXG connection.").classes("text-sm text-gray-600")

    # handlers
    async def browse_bin():
        p = await LocalFilePicker(str(OUTPUT_DIR if OUTPUT_DIR.exists() else "~"), only_ext={".bin"})
        if p:
            localbin_in.value = p
            wf_in.value = Path(p).name

    def latest_bin():
        # find newest *.bin in OUTPUT_DIR
        try:
            bins = sorted(OUTPUT_DIR.glob("*.bin"), key=lambda p: p.stat().st_mtime, reverse=True)
            if bins:
                localbin_in.value = str(bins[0])
                wf_in.value = bins[0].name
                ui.notify(f"Latest: {bins[0].name}")
            else:
                ui.notify("No .bin files found in output dir", type="warning")
        except FileNotFoundError:
            ui.notify(f"Output dir not found: {OUTPUT_DIR}", type="warning")

    def scpi_send(cmd: str):
        scpi_log.push(f"> {cmd}")
        # HW['mxg'].write(cmd) over pyvisa/socket; push the reply.

    def query_idn():
        scpi_log.push("> *IDN?")
        # HW['mxg'].query('*IDN?'); scpi_log.push(reply)
        ui.notify("Query *IDN? (connect MXG to get a real reply)")

    def upload_bin():
        scpi_log.push(f"> FTP PUT {localbin_in.value} -> {remote_in.value}")
        # FTP the local .bin to the MXG, then send load/select cmds.

    def upload_rf_on():
        upload_bin()
        scpi_send(":OUTP:STAT ON")

    def preview_cmds():
        scpi_log.push("--- preview ---")
        scpi_log.push(selcmd_in.value.replace("{remote_path}", remote_in.value + wf_in.value))

# Reusable SDR tab builder (used by B200 and customized for X40)

def sdr_tab(*, key: str, title: str, defaults: dict, antennas: dict, clock_sources: list[str],
            extra_config=None, sample_rates=None,
            rx_gain_range=(195, 255), tx_gain_range=None, card_max=None,
            rx_gain_label="RX Gain (index 195-255)", tx_gain_label="TX Gain (dB)",
            rx_gain_dropdown=False, tx_atten_dropdown=False, freq_extended_range=True,
            simple_rf=False):
    status_lbl = {"el": None}
    extra_els = {}
    psd_rec = {"obj": None}
    selected_band = {"f_low": None, "f_high": None}
    play_state = {"rec": None, "idx": 0, "timer": None}

    with section("Device Connection"):
        with ui.row().classes("w-full items-center gap-2"):
            init_btn = ui.button(f"Initialize {title}", on_click=lambda: initialize())
            ui.button("Close Device", on_click=lambda: close_device())
            ui.space()
            status_lbl["el"] = ui.label("DISCONNECTED").classes("text-red-600 font-bold")
            rec_lbl = ui.label("monitor only").classes("font-mono text-xs text-gray-500 self-center")
    def _freq_changed():
        be = HW.get(key)
        if be is not None:
            try:
                if be.set_frequency_live(float(cf_in.value)):
                    ui.notify(f"Retuned to {float(cf_in.value):g} Hz", type="positive", timeout=2000)
                    _refresh_gain_ranges()
            except Exception as e:
                ui.notify(f"Retune failed: {e}", type="negative")

    def _gain_changed():
        be = HW.get(key)
        if be is not None:
            try:
                if be.set_gain_live(float(rxg_in.value)):
                    ui.notify(f"Gain set to {int(float(rxg_in.value))}", type="positive", timeout=2000)
            except Exception as e:
                ui.notify(f"Gain change failed: {e}", type="negative")

    def _tx_atten_max_db():
        # 41.75 dB base (<6GHz); +15 dB extra at/above 6 GHz (18 GHz mezzanine).
        try:
            f = float(cf_in.value)
        except Exception:
            f = 0.0
        # extension only applies to radios with the 18 GHz mezzanine (X40);
        # the NV100/G40 caps at 6 GHz so it stays flat at 41.75 dB.
        return 56.75 if (freq_extended_range and f >= 6e9) else 41.75

    def _tx_atten_options():
        mx = _tx_atten_max_db()
        if mx > 41.75:
            vals = [0, 1, 2, 3, 5, 7, 10, 15, 20, 25, 30, 35, 40, 41.75, 45, 50, 56.75]
        else:
            vals = [0, 1, 2, 3, 5, 7, 10, 15, 20, 25, 30, 35, 40, 41.75]
        return {str(d): f"{d} dB" for d in vals}

    def _atten_changed():
        be = HW.get(key)
        if be is not None and hasattr(be, "set_tx_attenuation_qdb"):
            try:
                db = float(txg_in.value)
                _maxq = int(round(_tx_atten_max_db() * 4))
                qdb = int(max(0, min(_maxq, round(db * 4))))
                if be.set_tx_attenuation_qdb(qdb):
                    ui.notify(f"TX attenuation set to {db:g} dB", type="positive", timeout=2000)
            except Exception as e:
                ui.notify(f"TX attenuation change failed: {e}", type="negative")

    def _refresh_gain_ranges():
        # rebuild TX + RX dropdowns when frequency changes (ranges are freq-dependent)
        be = HW.get(key)
        try:
            if tx_atten_dropdown:
                _opts = _tx_atten_options()
                _cur = txg_in.value if txg_in.value in _opts else "0"
                txg_in.set_options(_opts, value=_cur)
        except Exception:
            pass
        try:
            if rx_gain_dropdown and be is not None and hasattr(be, "_skiq"):
                # Prefer real dB labels when the radio provides gain cal (NV100 does;
                # X40 returns None: index-only). Keys stay the bare index string so
                # _gain_changed/set_rx_gain_index get a clean integer.
                _table = None
                if hasattr(be, "rx_gain_table"):
                    try:
                        _table = be.rx_gain_table()   # [(index, db_or_None), ...]
                    except Exception:
                        _table = None
                _ro = {}
                if _table:
                    _los = _table[0][0]; _his = _table[-1][0]
                    _has_db = any(db is not None for _, db in _table)
                    for _ix, _db in _table:
                        if _db is not None:
                            _lab = f"{_ix} ({_db:+.1f} dB)"
                        else:
                            _lab = str(_ix)
                        if _ix == _los:
                            _lab += " - min" if _has_db else " (min gain)"
                        elif _ix == _his:
                            _lab += " - max" if _has_db else " (max gain)"
                        _ro[str(_ix)] = _lab
                else:
                    lo, hi = be._skiq.rx_gain_index_range(be._card, be._rx_hdl)
                    for _ix in range(int(lo), int(hi) + 1):
                        if _ix == int(lo):
                            _ro[str(_ix)] = f"{_ix} (min gain)"
                        elif _ix == int(hi):
                            _ro[str(_ix)] = f"{_ix} (max gain)"
                        else:
                            _ro[str(_ix)] = str(_ix)
                _firstkey = next(iter(_ro)) if _ro else "0"
                _cur = rxg_in.value if rxg_in.value in _ro else _firstkey
                rxg_in.set_options(_ro, value=_cur)
        except Exception:
            pass

    def _reinit_needed():
        if HW.get(key) is not None:
            ui.notify("Sample rate / antenna change needs re-Initialize to apply",
                      type="warning", timeout=4000)

    with section("Configuration"):
        with ui.row().classes("w-full gap-12 items-start"):
            with ui.column().classes("gap-2"):
                if sample_rates:
                    sr_in = ui.select(
                        {str(int(r)): f"{r/1e6:g} MSa/s" for r in sample_rates},
                        value=str(int(float(defaults["sample_rate"]))),
                        label="Sample Rate").props("dense outlined").classes("w-56")
                    sr_in.on_value_change(_reinit_needed) 
                else:
                    sr_in = ui.input("Sample Rate (Hz)", value=defaults["sample_rate"]).props("dense outlined")
                    sr_in.on_value_change(_reinit_needed)
                cf_in = ui.input("Center Freq (Hz)", value=defaults["center_freq"]).props("dense outlined")
                cf_in.on_value_change(_freq_changed)

                if rx_gain_dropdown and rx_gain_range:
                    # X40 has no factory dB cal: plain index dropdown.
                    _lo, _hi = int(rx_gain_range[0]), int(rx_gain_range[1])
                    _opts = {}
                    for _ix in range(_lo, _hi + 1):
                        if _ix == _lo:
                            _opts[str(_ix)] = f"{_ix} (min gain)"
                        elif _ix == _hi:
                            _opts[str(_ix)] = f"{_ix} (max gain)"
                        else:
                            _opts[str(_ix)] = str(_ix)
                    _rxdef = str(int(defaults["rx_gain"]))
                    if _rxdef not in _opts:
                        # default not in this radio's index range: use min-gain end
                        _rxdef = str(_lo)
                    rxg_in = ui.select(_opts, value=_rxdef,
                                       label="RX gain index (higher = more gain)") \
                        .props("dense outlined").classes("w-72")
                    rxg_in.on_value_change(_gain_changed)
                elif rx_gain_range:
                    rxg_in = ui.number(rx_gain_label, value=defaults["rx_gain"], format="%d",
                                       min=rx_gain_range[0], max=rx_gain_range[1]).props("dense outlined")
                    rxg_in.on_value_change(_gain_changed)
                else:
                    rxg_in = ui.number(rx_gain_label, value=defaults["rx_gain"], format="%d").props("dense outlined")
                    rxg_in.on_value_change(_gain_changed)
                if tx_atten_dropdown:
                    # TX attenuation in real dB (0 = max power). 0-41.75 dB <6GHz,
                    # +15 dB extra >=6GHz (18 GHz mezzanine). Rebuilds on retune.
                    _init_opts = _tx_atten_options()
                    _defdb = round(float(defaults["tx_gain"]) / 4, 2)
                    txg_in = ui.select(_init_opts, value=str(_defdb if str(_defdb) in _init_opts else 0),
                                       label="TX attenuation (dB), 0 = max power (max 41.75 dB <6GHz, 56.75 dB >=6GHz)") \
                        .props("dense outlined").classes("w-96")
                    txg_in.on_value_change(_atten_changed)
                elif tx_gain_range:
                    txg_in = ui.number(tx_gain_label, value=defaults["tx_gain"], format="%d",
                                       min=tx_gain_range[0], max=tx_gain_range[1]).props("dense outlined")
                else:
                    txg_in = ui.number(tx_gain_label, value=defaults["tx_gain"], format="%d").props("dense outlined")
            with ui.column().classes("gap-2"):
                clk_in = ui.select(clock_sources, value=next(iter(clock_sources)),
                                   label="Clock Source").props("dense outlined").classes("w-64")
                _rx0 = antennas["rx"][0] if isinstance(antennas["rx"], list) else next(iter(antennas["rx"]))
                _tx0 = antennas["tx"][0] if isinstance(antennas["tx"], list) else next(iter(antennas["tx"]))
                rxant_in = ui.select(antennas["rx"], value=_rx0,
                                     label="RX Antenna").props("dense outlined").classes("w-56")
                txant_in = ui.select(antennas["tx"], value=_tx0,
                                     label="TX Antenna").props("dense outlined").classes("w-56")
                if simple_rf:
                    # NV100 ports are dual-use; card + port (TRx1/TRx2) selects the SMA
                    # connector. Hide the X40-style separate antenna selects and
                    # drive them from one RF Port dropdown (TRx1 then A1, TRx2 then A2).
                    # Clock select stays visible (internal/external/GPSDO), like the X40
                    rxant_in.set_visibility(False)
                    txant_in.set_visibility(False)
                    def _port_changed(e):
                        port = e.value
                        if port == "TRx2":
                            rxant_in.set_value("RXA2"); txant_in.set_value("TXA2")
                        else:
                            rxant_in.set_value("RXA1"); txant_in.set_value("TXA1")
                    rfport_in = ui.select(["TRx1", "TRx2"], value="TRx1",
                                          label="RF Port (TRx1=J1, TRx2=J2)",
                                          on_change=_port_changed).props("dense outlined").classes("w-72")
                    rxant_in.set_value("RXA1"); txant_in.set_value("TXA1")
                chunk_default = int(defaults["rx_chunk"])  # Not user-facing
                if extra_config:
                    extra_config(extra_els)
 
    with section("TX Waveform Source"):
        with ui.row().classes("items-center gap-3"):
            ui.button("Select File", on_click=lambda: select_file())
            file_lbl = ui.label("(none selected)").classes("text-gray-500 italic")
        with ui.row().classes("items-center gap-6"):
            txmode_in = ui.radio({"SINGLE_SHOT": "Single-shot (one pass)", "CONTINUOUS": "Continuous (repeat)"},
                                  value="CONTINUOUS").props("inline")  # Read live at Start TX
        with ui.row().classes("gap-2 items-center"):  # TX controls live with the TX source
            ui.button("Start TX", on_click=lambda: start_tx()).props("color=positive")
            ui.button("Stop TX", on_click=lambda: stop_tx()).props("color=negative")
 
    with section("RX Waveform"):
        with ui.row().classes("gap-2 items-center"):
            ui.button("Start RX", on_click=lambda: start_rx()).props("color=positive")
            ui.button("Stop RX", on_click=lambda: stop_rx()).props("color=negative outline")
        with ui.row().classes("items-center gap-3"):
            # RX capture is a live toggle like trace/raw IQ:
            # attach/detach works mid-stream (scheduler reads .capture per frame)
            cap_btn = ui.button("Start RX Capture", on_click=lambda: toggle_capture()).props("color=positive")
            cap_state = ui.label("idle").classes("font-mono self-center text-gray-500")
            # Capture format; Epiq int16 uses the encoder convention
            capfmt_in = ui.select({"c64": "complex64 (.bin)",
                                   "iq16": "int16 IQ (I hi half)",
                                   "qi16": "int16 QI (Q hi half)"},
                                  value="c64", label="Format") \
                .props("dense outlined").classes("w-52")
            capture_path = ui.input("Capture path", value=str(OUTPUT_DIR / "rx_capture.bin")) \
                .props("dense outlined").classes("grow")

            def _cap_ext():
                if capfmt_in.value == "c64":
                    return ".bin"
                tag = "iq" if capfmt_in.value == "iq16" else "qi"
                base = {"g40": "g40", "x40": "x40"}.get(key)
                return f".{base}{tag}" if base else f".{tag}16"

            def _cap_fmt_changed():
                p = Path(capture_path.value)
                capture_path.value = str(p.with_suffix(_cap_ext()))
            capfmt_in.on_value_change(_cap_fmt_changed)
 
    # Signal Analysis / PSD / Waterfall as sub-tabs; _panel() re-parents the
    # existing bodies so they keep their indentation.
    import contextlib as _ctl
    with section("Analysis Displays"):
        _anal_tabs = ui.tabs().classes("w-full")
        with _anal_tabs:
            ui.tab("Signal Analysis")
            ui.tab("Power Spectral Density")
            ui.tab("Waterfall")
            ui.tab("Survey (blind)")
        _anal_panels = ui.tab_panels(_anal_tabs, value="Signal Analysis").classes("w-full")
    def _panel(_name):
        @_ctl.contextmanager
        def _cm():
            with _anal_panels:
                with ui.tab_panel(_name):
                    yield
        return _cm()
    with _panel("Signal Analysis"):
        with ui.row().classes("w-full items-end gap-4 flex-wrap"):
            window_in = ui.select(["hann", "hamming", "blackman", "rectangular"], value="hann",
                                  label="Window").props("dense outlined").classes("w-40")
            nfft_in = ui.select(["1024", "4096", "16384", "65536"], value="4096",
                                label="FFT Size").props("dense outlined").classes("w-40")
        with ui.row().classes("w-full gap-6 flex-wrap"):
            snr_lbl = ui.label("SNR: -- dB").classes("font-mono")
            foff_lbl = ui.label("Freq offset: -- Hz").classes("font-mono")
            baud_lbl = ui.label("Baud est: --").classes("font-mono")
            mod_lbl = ui.label("Modulation: --").classes("font-mono font-bold")
        with ui.tabs() as sig_tabs:
            ui.tab("Spectrum")
            ui.tab("Constellation")
        with ui.tab_panels(sig_tabs, value="Spectrum").classes("w-full"):
            with ui.tab_panel("Spectrum"):
                spectrum_chart = ui.echart({
                    "tooltip": {"trigger": "axis"},
                    "xAxis": {"type": "value", "name": "Hz", "scale": True},
                    "yAxis": {"type": "value", "name": "dB"},
                    "dataZoom": [
                        {"type": "slider", "xAxisIndex": 0, "filterMode": "none", "throttle": 200},
                        {"type": "inside", "xAxisIndex": 0, "filterMode": "none"},
                    ],
                    "series": [{"type": "line", "showSymbol": False, "data": []}],
                }).classes("w-full h-64")
                band_lbl = ui.label("Band: full span (drag the zoom bar under the spectrum to select)").classes("font-mono text-sm")

                def _on_zoom(e):
                    try:
                        a = e.args if hasattr(e, "args") else {}
                        if isinstance(a, dict) and a.get("batch"):
                            a = a["batch"][0]
                        lo = a.get("startValue")
                        hi = a.get("endValue")
                        if lo is None or hi is None:
                            data = spectrum_chart.options["series"][0]["data"]
                            if not data:
                                return
                            fmin, fmax = data[0][0], data[-1][0]
                            span = fmax - fmin
                            lo = fmin + span * a.get("start", 0) / 100.0
                            hi = fmin + span * a.get("end", 100) / 100.0
                        selected_band["f_low"] = float(min(lo, hi))
                        selected_band["f_high"] = float(max(lo, hi))
                        bw = selected_band["f_high"] - selected_band["f_low"]
                        band_lbl.set_text(
                            f"Band: {selected_band['f_low']/1e6:.4f} - "
                            f"{selected_band['f_high']/1e6:.4f} MHz  (BW {bw/1e3:.1f} kHz)")
                    except Exception as ex:
                        band_lbl.set_text(f"zoom parse error: {ex}")

                spectrum_chart.on("datazoom", _on_zoom)
            with ui.tab_panel("Constellation"):
                constellation_chart = ui.echart({
                    "tooltip": {"show": False},
                    "xAxis": {"type": "value", "min": -1.2, "max": 1.2, "name": "I"},
                    "yAxis": {"type": "value", "min": -1.2, "max": 1.2, "name": "Q"},
                    "series": [{"type": "scatter", "symbolSize": 4, "data": []}],
                }).classes("w-full h-64")
 
    with _panel("Power Spectral Density"):
        with ui.row().classes("w-full items-end gap-4 flex-wrap"):
            psd_nfft_in = ui.select(["1024", "4096", "16384", "65536"], value="4096",
                                    label="PSD FFT Size").props("dense outlined").classes("w-36")
            psd_win_in = ui.select(["hann", "hamming", "blackman", "rectangular"], value="hann",
                                   label="PSD Window").props("dense outlined").classes("w-36")
            psd_buf_in = ui.select({"16384": "16k", "65536": "64k", "262144": "256k", "1048576": "1M"},
                                   value="65536", label="Buffer samples").props("dense outlined").classes("w-36")
        # Trace and raw IQ are independent recorders, one toggle
        # each with a live REC indicator.
        with ui.row().classes("w-full items-center gap-2 flex-wrap"):
            trace_btn = ui.button("Start Trace Rec", on_click=lambda: toggle_trace()).props("color=positive")
            trace_state = ui.label("idle").classes("font-mono self-center text-gray-500")
            trace_path = ui.input("Trace path", value=str(OUTPUT_DIR / "x40_psd_trace.json")) \
                .props("dense outlined").classes("grow")
            trace_count = ui.label("traces: 0").classes("font-mono self-center")
        with ui.row().classes("w-full items-center gap-2 flex-wrap"):
            raw_btn = ui.button("Start Raw IQ", on_click=lambda: toggle_raw()).props("color=positive")
            raw_state = ui.label("idle").classes("font-mono self-center text-gray-500")
            rawcap_path = ui.input("Raw IQ path", value=str(OUTPUT_DIR / "x40_rx.iqf32")) \
                .props("dense outlined").classes("grow")
        with ui.row().classes("w-full items-center gap-2 flex-wrap"):
            ui.button("Load Trace File", on_click=lambda: load_trace())
            ui.button("Play", on_click=lambda: play_trace()).props("color=primary")
            ui.button("Stop Play", on_click=lambda: stop_play())
            play_pos = ui.label("").classes("font-mono self-center")
        psd_chart = ui.echart({
            "tooltip": {"trigger": "axis"},
            "grid": {"left": 70, "right": 20, "top": 30, "bottom": 60},
            "xAxis": {"type": "value", "name": "Frequency (Hz)", "scale": True,
                      "nameLocation": "middle", "nameGap": 32},
            "yAxis": {"type": "value", "name": "PSD (dB/Hz)", "scale": True,
                      "nameLocation": "middle", "nameGap": 50},
            "dataZoom": [
                {"type": "inside", "xAxisIndex": 0, "filterMode": "none"},
                {"type": "slider", "xAxisIndex": 0, "height": 16, "bottom": 6},
            ],
            "series": [{"type": "line", "showSymbol": False, "data": []}],
        }).classes("w-full h-72")
        psd_time_lbl = ui.label("t = -- s").classes("font-mono text-sm")

 
    with _panel("Waterfall"):
        _wf = {"rows": [], "freqs": None, "max_rows": 60, "bins": 150}
        ui.label("Live waterfall from PSD traces (start RX; updates only while this tab is visible)") \
            .classes("text-sm text-gray-600")
        wf_chart = ui.echart({
            "tooltip": {"show": False},
            "grid": {"left": 70, "right": 20, "top": 10, "bottom": 60},
            "xAxis": {"type": "category", "data": [], "name": "Frequency (MHz)",
                      "nameLocation": "middle", "nameGap": 28},
            "yAxis": {"type": "category", "data": [], "name": "trace"},
            "visualMap": {"min": -120, "max": -20, "calculable": True,
                          "orient": "horizontal", "left": "center", "bottom": 0,
                          "inRange": {"color": ["#0d0887", "#7e03a8", "#cc4778",
                                                 "#f89441", "#f0f921"]}},
            "series": [{"type": "heatmap", "data": [], "progressive": 4000}],
        }).classes("w-full h-72")

        def _wf_push(_freqs, _psd):
            # accumulate waterfall rows from any PSD source (live RX
            # or trace playback); render only while the Waterfall tab is open.
            try:
                import numpy as _np
                _f = _np.asarray(_freqs); _p = _np.asarray(_psd)
                if not len(_f):
                    return
                _st = max(1, len(_f) // _wf["bins"])
                _wf["freqs"] = _f[::_st][:_wf["bins"]]
                _wf["rows"].append(_p[::_st][:_wf["bins"]])
                if len(_wf["rows"]) > _wf["max_rows"]:
                    _wf["rows"].pop(0)
                if _anal_panels.value == "Waterfall":
                    _rows = _wf["rows"]
                    _dataw = [[fi, ti, round(float(v), 1)]
                              for ti, _r in enumerate(_rows) for fi, v in enumerate(_r)]
                    _all = _np.concatenate(_rows)
                    wf_chart.options["xAxis"]["data"] = [f"{x/1e6:.3f}" for x in _wf["freqs"]]
                    wf_chart.options["yAxis"]["data"] = list(range(len(_rows)))
                    wf_chart.options["visualMap"]["min"] = float(_np.percentile(_all, 5))
                    wf_chart.options["visualMap"]["max"] = float(_np.percentile(_all, 99))
                    wf_chart.options["series"][0]["data"] = _dataw
                    wf_chart.update()
            except Exception:
                pass
    with _panel("Survey (blind)"):
        from dpmg.web.survey_ui import build_survey_panel
        build_survey_panel(ui, lambda: CHANNELIZER.get("g40"))

    with section("Stream Health"):
        with ui.row().classes("w-full gap-6 flex-wrap"):
            m_state = ui.label("state: DISCONNECTED").classes("font-mono")
            m_frames = ui.label("rx frames: 0").classes("font-mono")
            m_fps = ui.label("fps: 0").classes("font-mono")
            m_xrun = ui.label("over/underruns: 0/0").classes("font-mono")
            m_uptime = ui.label("uptime: 0.0 s").classes("font-mono")
        health = ui.log(max_lines=300).classes("w-full h-48 font-mono text-xs text-blue-700")
        health.push(f"{title} Tab initialized (DISCONNECTED)")

    def _ts():
        import datetime as _dt
        return _dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]

    def _log(msg):
        # Every health line gets a timestamp for the test record
        health.push(f"[{_ts()}] {msg}")

    def _log_full_config(cfg, event="config"):
        """Log every config field with a timestamp: complete test record."""
        ex = getattr(cfg, "extras", {}) or {}
        _log(f"=== {event} :: {title} ===")
        _log(f"  sample_rate = {getattr(cfg,'sample_rate','?'):g} Hz")
        _log(f"  center_freq = {getattr(cfg,'center_freq','?'):g} Hz")
        _log(f"  rx_gain = {getattr(cfg,'rx_gain','?')}   tx_gain/atten = {getattr(cfg,'tx_gain','?')}")
        _log(f"  rx_antenna = {getattr(cfg,'rx_antenna','?')}   tx_antenna = {getattr(cfg,'tx_antenna','?')}")
        _log(f"  clock_source = {getattr(cfg,'clock_source','?')}   rx_chunk = {getattr(cfg,'rx_chunk','?')}")
        if ex:
            for k in sorted(ex):
                _log(f"  extra.{k} = {ex[k]}")

    # helpers 
    def _cfg() -> "sdr.SDRConfig":
        extras = {}
        if "card" in extra_els:
            extras["card"] = int(extra_els["card"].value)
        pass  # no Bw input; the backend derives rf_bandwidth_hz
        if "ref" in extra_els:
            extras["reference"] = extra_els["ref"].value
        if "iq_order" in extra_els:
            extras["iq_order_iq"] = False  # QI_PIN: wire order pinned QI (see sdr_core.py)
        return sdr.SDRConfig(
            sample_rate=float(sr_in.value), center_freq=float(cf_in.value),
            rx_gain=float(rxg_in.value), tx_gain=float(txg_in.value),
            clock_source=clk_in.value, rx_antenna=rxant_in.value, tx_antenna=txant_in.value,
            rx_chunk=int(chunk_default), extras=extras,
        )
 
    def set_status(state: "sdr.SDRDeviceState"):
        ok = state in (sdr.SDRDeviceState.IDLE, sdr.SDRDeviceState.STREAMING)
        color = "text-green-600" if ok else ("text-amber-600" if state == sdr.SDRDeviceState.ERROR else "text-red-600")
        status_lbl["el"].set_text(state.value)
        status_lbl["el"].classes(replace=f"font-bold {color}")
 
    # these parameters are consumed only by open(); changing them
    # mid-session has no effect until Close + Initialize, so they lock while a
    # session is open. Live controls (center freq: RX LO, RX gain, TX atten,
    # File IQ order) stay editable.
    def _set_init_lock(locked: bool) -> None:
        els = [sr_in, clk_in, rxant_in, txant_in]
        try:
            els.append(rfport_in)              # simple_rf tabs (G40) only
        except NameError:
            pass
        for _k in ("card", "ref"):
            if _k in extra_els:
                els.append(extra_els[_k])
        for _el in els:
            try:
                (_el.disable() if locked else _el.enable())
            except Exception:
                pass

    # handlers 
    async def initialize():
        try:
            cfg = _cfg().validate()
        except sdr.SDRConfigError as e:
            ui.notify(f"Config error: {e}", type="negative")
            return
        # Reflect the multi-second RFIC-cal open in the UI immediately,
        # and prevent a second click mid-open (skiq_init is process-global).
        set_status(sdr.SDRDeviceState.CONNECTING)
        status_lbl["el"].set_text("INITIALIZING...")
        init_btn.disable()
        # close any previous session first (libsidekiq is a process-global singleton;
        # a second skiq_init without a prior skiq_exit triggers "already initialized")
        old = HW.get(key)
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
            HW[key] = None
        backend = sdr.make_backend(key, cfg, simulate=False)

        # offload the multi-second RFIC-calibrating open() to a worker thread so the
        # event loop / websocket stays alive during NV100 init (else 'connection lost').
        try:
            await run.io_bound(backend.open)
        except sdr.SDRConnectionError as e:
            health.push(f"Hardware open failed: {e}")
            init_btn.enable()
            set_status(sdr.SDRDeviceState.ERROR)
            _set_init_lock(False)
            ui.notify("Hardware unavailable: check card power/enumeration and retry",
                      type="negative")
            return
        HW[key] = backend
        _set_init_lock(True)
        # populate RX gain (real dB on NV100) + TX atten ranges from the live radio
        try:
            _refresh_gain_ranges()
        except Exception:
            pass
        init_btn.enable()
        set_status(backend.health.state)
        _log(f"Initialized {title}")
        _log_full_config(cfg, event="INIT")
 
    def close_device():
        stop_stream()
        if HW[key] is not None:
            HW[key].close()
            HW[key] = None
        set_status(sdr.SDRDeviceState.DISCONNECTED)
        health.push("Device closed.")
        _set_init_lock(False)
 
    async def select_file():
        p = await LocalFilePicker(str(OUTPUT_DIR if OUTPUT_DIR.exists() else "~"),
                                  only_ext={".bin", ".wav", ".x40iq", ".x40qi", ".g40iq", ".g40qi"})
        if p:
            file_lbl.set_text(p)
            file_lbl.classes(replace="text-gray-800")
            src_in.value = "Load from File (.bin)"
    def _ensure_recorder():
        obj = psd_rec["obj"]
        nfft = int(psd_nfft_in.value)
        win = psd_win_in.value
        bufn = int(psd_buf_in.value)
        if obj is None:
            obj = pt.PSDRecorder(nfft=nfft, window=win, buffer_samples=bufn)
            psd_rec["obj"] = obj
        else:
            obj.nfft = nfft
            obj.window = win
            obj.buffer_samples = bufn
        return obj
 
    def toggle_trace():  # One toggle per recorder, state visible
        rec = psd_rec["obj"]
        if rec is not None and rec.recording is not None:
            recording = rec.stop_trace_recording()
            trace_btn.set_text("Start Trace Rec")
            trace_btn.props("color=positive")
            trace_state.set_text("idle")
            trace_state.classes(replace="font-mono self-center text-gray-500")
            try:
                out = recording.save(trace_path.value)
                health.push(f"Trace saved: {out} ({len(recording.traces)} traces)")
                ui.notify(f"Saved {len(recording.traces)} traces -> {out}", type="positive")
            except ValueError as e:
                ui.notify(f"Trace save failed: {e}", type="warning")
            return
        if HW[key] is None:
            ui.notify("Initialize + start streaming first", type="warning")
            return
        rec = _ensure_recorder()
        rec.start_trace_recording(float(sr_in.value), float(cf_in.value))
        trace_btn.set_text("Stop + Save Trace")
        trace_btn.props("color=negative")
        trace_state.set_text("REC")
        trace_state.classes(replace="font-mono self-center text-red-600 font-bold")
        health.push("PSD trace recording started.")

    def toggle_raw():  # Raw IQ capture independent of the trace
        rec = psd_rec["obj"]
        if rec is not None and rec._raw_fh is not None:
            rawp = rec.stop_raw_capture()
            raw_btn.set_text("Start Raw IQ")
            raw_btn.props("color=positive")
            raw_state.set_text("idle")
            raw_state.classes(replace="font-mono self-center text-gray-500")
            if rawp:
                health.push(f"Raw IQ saved: {rawp}")
                ui.notify(f"Raw IQ saved -> {rawp}", type="positive")
            return
        if HW[key] is None:
            ui.notify("Initialize + start streaming first", type="warning")
            return
        rec = _ensure_recorder()
        try:
            rec.start_raw_capture(rawcap_path.value)
        except OSError as e:
            ui.notify(f"Raw capture open failed: {e}", type="negative")
            return
        raw_btn.set_text("Stop + Save Raw IQ")
        raw_btn.props("color=negative")
        raw_state.set_text("REC")
        raw_state.classes(replace="font-mono self-center text-red-600 font-bold")
        health.push(f"Raw IQ capture -> {rawcap_path.value}")
 
    def _draw_trace(tr):
        data = [[float(f), float(p)] for f, p in zip(tr.freqs_hz, tr.psd_db)]
        psd_chart.options["series"][0]["data"] = data
        psd_chart.update()
        _wf_push(tr.freqs_hz, tr.psd_db)
        psd_time_lbl.set_text(f"t = {tr.timestamp_sec:.6f} s  (seq {tr.seq})")
 
    def load_trace():
        async def _pick():
            p = await LocalFilePicker(str(OUTPUT_DIR if OUTPUT_DIR.exists() else "~"),
                                      only_ext={".json"})
            if not p:
                return
            try:
                rec = pt.TraceRecording.load(p)
            except Exception as e:
                ui.notify(f"Not a trace file: {e}", type="negative")
                return
            play_state["rec"] = rec
            play_state["idx"] = 0
            health.push(f"Loaded trace: {len(rec.traces)} frames, fs={rec.sample_rate:g}")
            ui.notify(f"Loaded {len(rec.traces)} traces")
        ui.timer(0.01, _pick, once=True)
 
    def play_trace():
        rec = play_state["rec"]
        if rec is None or not rec.traces:
            ui.notify("Load a trace file first", type="warning")
            return
        if play_state["timer"] is not None:
            return
 
        def _step():
            i = play_state["idx"]
            if i >= len(rec.traces):
                stop_play()
                return
            tr = rec.traces[i]
            _draw_trace(tr)
            play_pos.set_text(f"{i + 1}/{len(rec.traces)}")
            play_state["idx"] = i + 1
 
        play_state["timer"] = ui.timer(0.1, _step)
 
    def stop_play():
        if play_state["timer"] is not None:
            play_state["timer"].cancel()
            play_state["timer"] = None
 
    # independent RX/TX using the full-duplex scheduler
    def _ensure_sched():
        backend = HW.get(key)
        if backend is None:
            ui.notify(f"Initialize {title} first", type="warning"); return None
        if SCHED.get(key) is None:
            SCHED[key] = sdr.FullDuplexScheduler(backend, tx_mode=sdr.TXMode.STOPPED)
            SCHED[key].frame_hook = getattr(SCHED[key], "frame_hook", None)
        return SCHED[key]

    def _capture_ui_reset():
        cap_btn.set_text("Start RX Capture")
        cap_btn.props("color=positive")
        cap_state.set_text("idle")
        cap_state.classes(replace="font-mono self-center text-gray-500")

    def toggle_capture():  # Owns the RX capture lifecycle
        sched = SCHED.get(key)
        if sched is not None and sched.capture is not None:
            try:
                sched.capture.close()
            except Exception:
                pass
            sched.capture = None
            _capture_ui_reset()
            _log(f"RX capture saved: {capture_path.value}")
            ui.notify(f"RX capture saved -> {capture_path.value}", type="positive")
            return
        if sched is None:
            ui.notify("Initialize first (capture attaches to the RX stream)", type="warning")
            return
        try:
            # pass fmt through so the file format matches the Format select
            sched.capture = sdr.RXCapture(capture_path.value, fmt=capfmt_in.value)
        except OSError as e:
            ui.notify(f"Cannot open capture file: {e}", type="negative")
            return
        cap_btn.set_text("Stop + Save RX Capture")
        cap_btn.props("color=negative")
        cap_state.set_text("REC")
        cap_state.classes(replace="font-mono self-center text-red-600 font-bold")
        _log(f"RX capture -> {capture_path.value} ({capfmt_in.value})")

    def start_rx():
        sched = _ensure_sched()
        if sched is None: return
        try:
            sched.start_rx()
            set_status(sdr.SDRDeviceState.STREAMING)
            _log("RX started")
        except Exception as e:
            ui.notify(f"Start RX failed: {e}", type="negative")

    def stop_rx():
        sched = SCHED.get(key)
        if sched is not None:
            sched.stop_rx(); _log("RX stopped")
            if sched.capture is not None:  # Sched closed the file
                sched.capture = None
                _capture_ui_reset()
                _log(f"RX capture saved: {capture_path.value}")
        if HW.get(key) is not None:
            set_status(HW[key].health.state)

    def start_tx():
        sched = _ensure_sched()
        if sched is None: return
        # File is the only TX source
        if file_lbl.text in ("(none selected)", ""):
            ui.notify("Select a waveform file first", type="warning")
            return
        try:
            wave, meta = sdr.WaveformLoader.load(file_lbl.text, HW[key].config.sample_rate)
            _log(f"TX waveform: {Path(meta.path).name} ({meta.duration*1e3:.1f} ms)")
            sched.tx_mode = sdr.TXMode[txmode_in.value]  # Live mode, not baked at Start RX
            sched.set_tx_waveform(wave)  # File amplitude is authoritative
            sched.start_tx()
            set_status(sdr.SDRDeviceState.STREAMING)
            _log("TX started")
        except sdr.WaveformError as e:
            ui.notify(f"Waveform error: {e}", type="negative")
        except Exception as e:
            ui.notify(f"Start TX failed: {e}", type="negative")

    def stop_tx():
        sched = SCHED.get(key)
        if sched is not None:
            sched.stop_tx(); _log("TX stopped (radio TX datapath halted)")
        if HW.get(key) is not None:
            set_status(HW[key].health.state)

    def stop_stream():
        sched = SCHED[key]
        if sched is not None:
            sched.stop()
            SCHED[key] = None
            health.push(f"Streaming stopped. {sched.metrics.frames} RX frames, "
                        f"{sched.metrics.repeats} TX repeats.")
        if HW[key] is not None:
            set_status(HW[key].health.state)
 
    def refresh():
        sched = SCHED[key]
        backend = HW[key]
        # Always-visible chip: red while any recorder is
        # active, grey "monitor only" while only watching the live PSD
        _active = []
        _rec = psd_rec["obj"]
        if _rec is not None and _rec.recording is not None:
            _active.append("trace")
        if _rec is not None and _rec._raw_fh is not None:
            _active.append("raw IQ")
        if sched is not None and sched.capture is not None:
            _active.append("rx capture")
        if "rx capture" not in _active and cap_state.text == "REC":
            _capture_ui_reset()      # stream torn down under a capture
        if _active:
            rec_lbl.set_text("REC: " + " + ".join(_active))
            rec_lbl.classes(replace="font-mono text-xs text-red-600 font-bold self-center")
        else:
            rec_lbl.set_text("monitor only")
            rec_lbl.classes(replace="font-mono text-xs text-gray-500 self-center")
        if backend is not None:
            h = backend.health
            m_state.set_text(f"state: {h.state.value}")
            m_xrun.set_text(f"over/underruns: {h.overruns}/{h.underruns}")
        if sched is None:
            return
        m_frames.set_text(f"rx frames: {sched.metrics.frames}")
        m_fps.set_text(f"fps: {sched.metrics.fps:.0f}")
        m_uptime.set_text(f"uptime: {sched.metrics.uptime:.1f} s")
        frame = sched.latest()
        if frame is None or len(frame.samples) == 0:
            return
        # everything below needs a live backend (backend.config
        # is read by ConstellationExtractor). If backend is None, skip the RX
        # DSP entirely: prevents the 'NoneType has no attribute config' crash
        # loop and stops RX DSP from running when there is no active RX.
        if backend is None:
            return

        import numpy as np
        
        spec = sdr.SpectrumAnalyzer(nfft=int(nfft_in.value), window=window_in.value).analyze(frame)
        step = max(1, len(spec.freqs) // 512)
        spectrum_chart.options["series"][0]["data"] = [
            [float(f), float(m)] for f, m in zip(spec.freqs[::step], spec.mag_db[::step])
        ]
        spectrum_chart.update()
        cd = sdr.ConstellationExtractor(int(backend.config.sample_rate // 9600)).extract(frame)
        constellation_chart.options["series"][0]["data"] = [
            [float(i), float(q)] for i, q in zip(cd.i, cd.q)
        ]
        constellation_chart.update()
        rec = _ensure_recorder()
        rec.push_frame(frame.samples, frame.sample_rate, frame.center_freq,
                       rf_timestamp=getattr(frame, "rf_timestamp", 0), seq=frame.seq)
        if play_state["timer"] is None:
            f, p = rec.live_psd(frame.sample_rate, frame.center_freq)
            if len(f):
                pstep = max(1, len(f) // 1024)
                psd_chart.options["series"][0]["data"] = [
                    [float(a), float(b)] for a, b in zip(f[::pstep], p[::pstep])
                ]
                psd_chart.update()
                _wf_push(f, p)  # Waterfall from live RX
                if rec.recording is not None:
                    trace_count.set_text(f"traces: {len(rec.recording.traces)}")
                if getattr(frame, "rf_timestamp", 0) and frame.sample_rate:
                    psd_time_lbl.set_text(f"t = {frame.rf_timestamp / frame.sample_rate:.6f} s (live)")
        snr_lbl.set_text(f"SNR: {sdr.SignalEstimator.estimate_snr(frame):.1f} dB")
        foff_lbl.set_text(f"Freq offset: {sdr.SignalEstimator.estimate_frequency_offset(frame):.0f} Hz")
        baud_lbl.set_text(f"Baud est: {sdr.SignalEstimator.estimate_baud_rate(frame):.0f}")
        cls = sdr.ModulationClassifier().classify(frame, spec)
        mod_lbl.set_text(f"Modulation: {cls.primary} ({cls.confidence:.0%})")
 
    ui.timer(0.25, refresh)
 

# SDR tab; selector picks the radio + rebuilds its config

def unified_sdr_tab():
    def _x40_extra(extra_els):
        # Single X4 card in the X40 host: pinned card 0
        # (port selects the RF handle on the card, not the card). There is no
        # "Reference" select: extras["reference"] is not consumed.
        extra_els["card"] = ui.select({0: "card0 (single X4 card)"}, value=0,
                                      label="Card").props("dense outlined").classes("w-56")
        extra_els["iq_order"] = ui.select({"auto": "Auto (from extension)", "iq": "IQ (I first)",
                                                      "qi": "QI (Q first)"},  # FILE_IQORDER
                                           value="auto",
                                           on_change=lambda e: sdr.set_file_iq_order(e.value),
                                           label="File IQ order").props("dense outlined").classes("w-56")

    def _g40_extra(extra_els):
        extra_els["card"] = ui.select({0: "card0 (NV100-1, TRx1A/2A)", 1: "card1 (NV100-2, TRx1B/2B)"},
                                      value=0, label="NV100 card").props("dense outlined").classes("w-72")
        extra_els["iq_order"] = ui.select({"auto": "Auto (from extension)", "iq": "IQ (I first)",
                                                     "qi": "QI (Q first)"},
                                          value="auto",
                                          on_change=lambda e: sdr.set_file_iq_order(e.value),
                                          label="File IQ order").props("dense outlined").classes("w-72")

    PROFILES = {
        "B200": dict(
            key="b200", title="B200",
            defaults={"sample_rate": "1000000", "center_freq": "900000000",
                      "rx_gain": 30, "tx_gain": 20, "rx_chunk": 4096},
            antennas={"rx": ["RX2", "TX/RX"], "tx": ["TX/RX"]},
            clock_sources=["internal", "external", "gpsdo"],
        ),
        "Epiq X40": dict(
            key="x40", title="Epiq X40 (libsidekiq v4.21)",
            defaults={"sample_rate": "50000000", "center_freq": "425025000",
                      "rx_gain": 200, "tx_gain": 0, "rx_chunk": 16384},
            antennas={"rx": {"RXA1": "RXA1: J2 (RFIC A Rx2)",
                         "RXA2": "RXA2: J1 (RFIC A Rx1)",
                         "RXB1": "RXB1: J4 (RFIC B Rx2)",
                         "RXB2": "RXB2: J3 (RFIC B Rx1)"},
                  "tx": {"TXA1": "TXA1: J5"}},  
                  # no TXB1/J6: no _SKIQ_TX_HDL mapping exists and sdr_core
                  # silently coerces unknown TX antennas to TXA1 (J6 would key J5).
            clock_sources=_g40_clock.labels("x40"),     # no FPGA GPSDO on an X40
            extra_config=_x40_extra, sample_rates=sdr.X40_VALID_RATES_HZ,
            rx_gain_dropdown=True, tx_atten_dropdown=True,
        ),
        "Epiq G40": dict(
            key="g40", title="Epiq G40 (libsidekiq v4.19.1)",
            defaults={"sample_rate": "30720000", "center_freq": "425025000",
                      "rx_gain": 30, "tx_gain": 0, "rx_chunk": 16384},
            antennas={"rx": ["RXA1", "RXA2", "RXB1"], "tx": ["TXA1", "TXA2"]},
            clock_sources=_g40_clock.labels("g40"),     # tokens with their labels
            extra_config=_g40_extra, sample_rates=sdr.G40_VALID_RATES_HZ,
            rx_gain_dropdown=True, tx_atten_dropdown=True,
            freq_extended_range=False, simple_rf=True,
        ),
    }

    with ui.row().classes("items-center gap-3 w-full"):
        ui.label("SDR:").classes("font-bold")
        sel = ui.select(list(PROFILES.keys()), value="Epiq G40",
                        label="Select radio").props("outlined").classes("w-72")
    body = ui.column().classes("w-full")

    def _build():
        body.clear()
        with body:
            sdr_tab(**PROFILES[sel.value])
    sel.on_value_change(lambda e: _build())
    _build()

# PAGE

# Adaptive Coding & Modulation panel (dpmg.fec Tier 5). Shows the
# MODCOD ladder and a live SNR: MODCOD selection so an operator can see which
# modulation/FEC the link would pick at a given measured Es/N0. Read-only /
# planning aid; the actual switch logic lives in dpmg.fec.acm.AcmController.
#
# Currently unmounted: the tab and panel are commented out in main_page under
# ACM_MOTHBALL. Left in place, and still works if wired back in; the ACM engine
# itself ships in dpmg/fec/acm.py (test_fec_acm.py covers it). Kept off the tab
# bar because it's a planning aid rather than part of the encode/validate/SDR work.
def build_acm_tab():
    try:
        from dpmg.fec import acm
    except Exception as exc:                       # pragma: no cover
        ui.label(f"ACM engine unavailable: {exc}").classes("text-red-600")
        return

    rows = [
        {"rung": mc.idx, "modcod": mc.name, "mod": mc.modulation,
         "fec": f"{mc.fec_family} {mc.rate}", "req": f"{mc.req_esn0_db:.1f}",
         "eff": f"{mc.spectral_eff:.2f}"}
        for mc in acm.ACM_TABLE
    ]
    columns = [
        {"name": "rung", "label": "#", "field": "rung", "align": "left"},
        {"name": "modcod", "label": "MODCOD", "field": "modcod", "align": "left"},
        {"name": "mod", "label": "Modulation", "field": "mod", "align": "left"},
        {"name": "fec", "label": "FEC", "field": "fec", "align": "left"},
        {"name": "req", "label": "Req Es/N0 (dB)", "field": "req", "align": "right"},
        {"name": "eff", "label": "Eff (b/sym)", "field": "eff", "align": "right"},
    ]

    with ui.card().classes("w-full"):
        ui.label("Adaptive Coding & Modulation").classes("text-lg font-bold")
        ui.label("MODCOD ladder (nominal Es/N0 thresholds; tune from a BER "
                 "campaign before flight).").classes("text-sm text-gray-500")
        with ui.row().classes("items-center gap-4"):
            ui.label("Measured Es/N0 (dB):")
            snr_in = ui.number(value=8.0, min=-5.0, max=30.0, step=0.5).classes("w-32")
            ui.label("Margin (dB):")
            margin_in = ui.number(value=1.0, min=0.0, max=6.0, step=0.5).classes("w-24")
        sel_label = ui.label().classes("text-base font-mono")
        table = ui.table(columns=columns, rows=rows, row_key="modcod").classes("w-full")

        def _refresh():
            try:
                snr = float(snr_in.value)
                margin = float(margin_in.value)
            except (TypeError, ValueError):
                return
            mc = acm.select_modcod(snr, margin_db=margin)
            sel_label.set_text(
                f"-> selected: {mc.name}   {mc.modulation} + {mc.fec_family} "
                f"{mc.rate}   throughput {mc.spectral_eff:.2f} info-bit/symbol")
            table.selected = [r for r in rows if r["modcod"] == mc.name]

        snr_in.on("update:model-value", lambda _e: _refresh())
        margin_in.on("update:model-value", lambda _e: _refresh())
        table.props("selection=single")
        _refresh()


from dpmg.web.theme import apply_theme, theme_toggle  # noqa: E402


@ui.page("/engineering")                                # Operator view is the default ("/")
def main_page():
    apply_theme()
    ui.add_head_html('<link rel="icon" type="image/svg+xml" href="/favicon.ico?v=2">')
    with ui.header().classes("items-center justify-between bg-slate-700"):
        with ui.row().classes("items-center gap-3"):
            ui.label("Data Packet Message Generator").classes("text-lg font-bold text-white")
            ui.link("operator view", "/").classes("text-xs text-gray-300")
            ui.link("mission view", "/mission").classes("text-xs text-gray-300")  # MISSION_VIEW
        build_link_header()  # Link + ACFT pos strip
        theme_toggle()

    with ui.tabs().classes("w-full") as tabs:
        ui.tab("Encoder")
        ui.tab("IQ Validator")
        ui.tab("MXG Deploy")
        ui.tab("SDR")
        ui.tab("Payload")                      # Payload RF chain, EIRP, RX protection
        ui.tab("Mission planner (engineering)")  # the full planner, motion included
        ui.tab("Aircraft feed")                # feed settings, raw packets, test sender
        # The ACM MODCOD panel is a read-only planning aid, not part
        # of the generate/validate/SDR flow, so it's off the tab bar. The engine
        # (dpmg/fec/acm.py) and build_acm_tab() below are kept and tested:
        # uncomment this tab and its panel to bring it back.
        # ui.tab("ACM")                          # ACM_GUI: Tier-5 MODCOD panel
        # The Watch List tab drove the local in-process
        # channelizer (GUI running on the radio host). The ground station is
        # x86-only: the remote G40 channelizer lives under SDR, then
        # "Channelizer decode". Re-enable this tab (and its panel below) only
        # for a local-hardware diagnostic build.
        # ui.tab("Channelizer", label="Watch List")
    with ui.tab_panels(tabs, value="Encoder").classes("w-full"):
        with ui.tab_panel("Encoder"):
            encoder_tab()
        with ui.tab_panel("IQ Validator"):
            iq_validator_tab()
        with ui.tab_panel("MXG Deploy"):
            mxg_tab()
        with ui.tab_panel("SDR"):
            # The ground station is remote-only: the x86 GCS has no
            # radio hardware, so everything is a discovered remote SDR.
            build_remote_sdr()
        with ui.tab_panel("Payload"):          # Polls only while this tab is shown
            from dpmg.web.payload_tab import build_payload_page
            build_payload_page(is_active=lambda: tabs.value == "Payload")
        with ui.tab_panel("Mission planner (engineering)"):
            # the same planner as /mission (one plan session per GUI), with
            # every control on: the archive of the full tool, simulated motion
            # included. The flight view at /mission shows the live feed instead.
            from dpmg.mission.mission_tab import build_mission_tab, MODE_ENGINEERING
            from dpmg.web.link_tab import MGR as _link_mgr
            build_mission_tab(_link_mgr, mode=MODE_ENGINEERING)
        with ui.tab_panel("Aircraft feed"):
            from dpmg.web.aircraft_feed_tab import build_aircraft_feed_tab
            build_aircraft_feed_tab()
        # Panel for the ACM tab (see the tab bar above)
        # with ui.tab_panel("ACM"):              # ACM_GUI
        #     build_acm_tab()
        # Panel for the local-channelizer tab (see above)
        # with ui.tab_panel("Channelizer"):
        #     build_channelizer_tab(key="g40", HW=HW, SCHED=SCHED, section=section)


@ui.page("/")                                           # Operator view is the default landing
@ui.page("/operator")                                   # Alias kept for existing bookmarks
def operator_page():
    ui.page_title("DPMG Operator")
    apply_theme()
    ui.add_head_html('<link rel="icon" type="image/svg+xml" href="/favicon.ico?v=2">')
    with ui.header().classes("items-center justify-between bg-slate-800"):
        with ui.row().classes("items-center gap-3"):
            ui.label("DPMG Operator").classes("text-lg font-bold text-white")
            ui.link("engineering view", "/engineering").classes("text-xs text-gray-300")
            ui.link("mission view", "/mission").classes("text-xs text-gray-300")  # MISSION_VIEW
        build_link_header()
        theme_toggle()
    build_operator_sdr()


@ui.page("/mission")                                    # MISSION_VIEW: the planner as its own view
def mission_page():
    # A peer of the operator and engineering views, built the same way: the
    # app header with the Link + ACFT strip, then the planner as the whole
    # body. It shares the SDR tab's LinkManager (link_tab.MGR), so it lists
    # the same radios (discovered and Connect by IP) and needs no second
    # discovery port. Plan autosave and the map store live under ~/.dpmg,
    # which launch_gui.sh bind-mounts from the host.
    ui.page_title("DPMG Mission")
    apply_theme()
    ui.add_head_html('<link rel="icon" type="image/svg+xml" href="/favicon.ico?v=2">')
    with ui.header().classes("items-center justify-between bg-slate-700"):
        with ui.row().classes("items-center gap-3"):
            ui.label("Data Packet Message Generator").classes("text-lg font-bold text-white")
            ui.link("operator view", "/").classes("text-xs text-gray-300")  # MISSION_VIEW
            ui.link("engineering view", "/engineering").classes("text-xs text-gray-300")  # MISSION_VIEW
        build_link_header()  # Link + ACFT pos strip
        theme_toggle()
    from dpmg.mission.mission_tab import build_mission_tab, MODE_FLIGHT
    from dpmg.web.link_tab import MGR as link_mgr
    build_mission_tab(link_mgr, mode=MODE_FLIGHT)         # the flight view


@ui.page("/bitpipe")                                    # BITPIPE_VIEW: the stream link panel
def bitpipe_page():
    # The link panel for the laptop's bitpipe container (dpmg/bitpipe), which
    # runs beside the GUI (python3 -m dpmg.bitpipe.peer). The GUI only talks to
    # the container's local faces: control, chat and files. Where they are:
    # DPMG_BITPIPE_CTL / _CHAT / _FILE, default the peer's TCP ports on
    # 127.0.0.1 (the GUI container uses host networking).
    ui.page_title("DPMG Bitpipe")
    apply_theme()
    ui.add_head_html('<link rel="icon" type="image/svg+xml" href="/favicon.ico?v=2">')
    with ui.header().classes("items-center justify-between bg-slate-700"):
        with ui.row().classes("items-center gap-3"):
            ui.label("Data Packet Message Generator").classes("text-lg font-bold text-white")
            ui.link("operator view", "/").classes("text-xs text-gray-300")  # BITPIPE_VIEW
            ui.link("engineering view", "/engineering").classes("text-xs text-gray-300")  # BITPIPE_VIEW
        build_link_header()
        theme_toggle()
    import os as _bp_os
    from dpmg.bitpipe.link_panel import build_link_panel
    build_link_panel(_bp_os.environ.get("DPMG_BITPIPE_CTL", "tcp:127.0.0.1:47898"),
                     _bp_os.environ.get("DPMG_BITPIPE_CHAT", "tcp:127.0.0.1:47901"),
                     _bp_os.environ.get("DPMG_BITPIPE_FILE", "tcp:127.0.0.1:47902"),
                     title="Bitpipe link (laptop container)")


# ui.run must be at module level (NiceGUI re-imports under __mp_main__).
# Browser-tab identity. Icon lives at the repo root;
# fall back to the NiceGUI default favicon if the file is missing.
from dpmg.paths import REPO_ROOT as _REPO_ROOT  # noqa: E402
_MOTHBALL_ICON = _REPO_ROOT / "mothball_fury.svg"
_MOTHBALL_ICON = str(_MOTHBALL_ICON) if _MOTHBALL_ICON.exists() else None
if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        host="0.0.0.0",
        port=8080,
        title="MOTHBALL FURY",
        favicon=_MOTHBALL_ICON,
        reload=False,        # set True only for local dev outside Docker
        show=False,          # don't try to open a browser on the headless Jetson
        reconnect_timeout=30.0,   # tolerate GPU/decode loop stalls without dropping the client
    )
