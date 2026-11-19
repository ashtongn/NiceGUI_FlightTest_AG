"""Mission planning catalog.

The fixed, reviewed list of techniques an operator can place on a mission
timeline. Nothing here is new transmit capability. Every entry points at a
command the dpmg service already has, reached through the same confirmed
radiate path the jammer, playback, tools, transponder and record panels use.

An operator cannot add to this list from the tab. Adding a technique is a
change to the product, made here in code.

Each entry carries:
  technique    the id written into a mission step (for example jammer.barrage)
  group        the heading it sits under in the tab
  label        what the operator reads
  radiates     true if running it keys TX, so it needs the radiate confirm
  default_dur  seconds, the length a fresh step gets on the timeline
  fields       every option the step editor shows, in order (Field below)
  params       the default parameter block for a fresh step, built from fields
  service      which service command the runner drives, filled in by the
               mapping in mission_runner.py (kept here only as a note)

The fields mirror what the existing panels send to the service (link_tab.py
jammer, tools, playback, transponder and record/replay panels, plus the RF
center and TX attenuation the radio control card sets), so a step built here
lands on the radio the same way the panel would.
"""

from __future__ import annotations

import copy

# Radio capability tags. A radio in a mission file may list the techniques it
# can run; if it lists none, it can run anything in the catalog. A technique
# names the tag it needs so the validator can reject a step placed on a radio
# that cannot run it.
TAG_TX = "tx"            # can key a transmit waveform
TAG_RX = "rx"            # can receive and record
TAG_SECURE = "secure"    # secure messaging enabled on this radio

# TX attenuation the control card offers (dB); the service takes quarter dB
TX_ATTEN_CHOICES = [0, 1, 2, 3, 5, 7, 10, 15, 20, 25, 30, 35, 40, 41.75]

# RX gain as the control card offers it on a G40: the hardware gain index
# (187-255, what set_gain takes), labelled in dB above the minimum. 0.5 dB per
# step. An X40 starts at 195, so 187-194 are G40 only.
RX_GAIN_MIN_G40 = 187
RX_GAIN_MIN_X40 = 195
RX_GAIN_MAX = 255
RX_GAIN_CHOICES = [(i, f"+{(i - RX_GAIN_MIN_G40) * 0.5:.1f} dB (index {i})")
                   for i in range(RX_GAIN_MAX, RX_GAIN_MIN_G40 - 1, -1)]

# Sample rates a radio can be set to, taken from the same source the link tab
# uses (sdr_core), so the planner and the live GUI agree on what is valid. A
# plan radio carries a starting rate and can be re-initialized to another rate
# at a scheduled time (the "Set sample rate" technique below). VALID_RATES is
# keyed by radio type so the validator can check a rate against the right list.
try:
    from dpmg.radio.sdr_core import (G40_VALID_RATES_HZ as _G40_RATES,
                          X40_VALID_RATES_HZ as _X40_RATES,
                          B200_VALID_RATES_HZ as _B200_RATES)
except Exception:
    _G40_RATES = [1_920_000, 7_680_000, 15_360_000, 30_720_000]
    _X40_RATES = [50_000_000]
    _B200_RATES = [1_000_000, 2_000_000, 5_000_000, 10_000_000, 15_360_000,
                   20_000_000, 25_000_000, 30_720_000, 40_000_000, 50_000_000,
                   56_000_000, 61_440_000]
VALID_RATES = {"g40": list(_G40_RATES), "x40": list(_X40_RATES),
               "b200": list(_B200_RATES)}
DEFAULT_RATE_HZ = 30_720_000 if 30_720_000 in _G40_RATES else _G40_RATES[0]


def valid_rates_for(radio_type) -> list:
    """The valid sample rates for a radio type. A radio with its own list (x40,
    b200) uses it; anything unknown is treated as a G40, the common case on this
    ground station."""
    return VALID_RATES.get(radio_type, VALID_RATES["g40"])


def is_valid_rate(hz, radio_type) -> bool:
    try:
        hz = float(hz)
    except (TypeError, ValueError):
        return False
    return any(abs(hz - v) <= max(1.0, v * 1e-3)
               for v in valid_rates_for(radio_type))


class Field:
    """One option in the step editor.

    kind   num     plain number
           hz      frequency, stored in Hz, edited with a Hz/kHz/MHz unit
           select  one of choices [(value, label), ...]
           bool    checkbox
           text    one line of text
           json    a JSON object (the transponder response)
    optional  the field may be left blank, stored as None (the runner then
              leaves that setting alone); blank is the box's placeholder
    show_if   (key, values): only shown while params[key] is one of values,
              or a function of the params block returning True or False
    """
    __slots__ = ("key", "label", "kind", "default", "choices", "optional",
                 "show_if", "help", "min", "max", "step", "blank")

    def __init__(self, key, label, kind, default=None, choices=None,
                 optional=False, show_if=None, help="", min=None, max=None,
                 step=None, blank="keep as set"):
        self.key = key
        self.blank = blank        # what a blank optional field means, shown in the box
        self.label = label
        self.kind = kind
        self.default = default
        self.choices = choices
        self.optional = optional
        self.show_if = show_if
        self.help = help
        self.min = min
        self.max = max
        self.step = step

    def shown(self, params: dict) -> bool:
        if not self.show_if:
            return True
        if callable(self.show_if):
            return bool(self.show_if(params))
        key, values = self.show_if
        return params.get(key) in values


class Entry:
    __slots__ = ("technique", "group", "label", "radiates", "default_dur",
                 "fields", "params", "needs", "note", "hint")

    def __init__(self, technique, group, label, radiates, default_dur,
                 fields, fixed=None, needs=(TAG_TX,), note="", hint=""):
        self.technique = technique
        self.group = group
        self.label = label
        self.radiates = radiates
        self.default_dur = default_dur
        self.fields = list(fields)
        # fixed keys ride along in every step but are not editable (the
        # generator kind of a jammer, for example)
        self.params = dict(fixed or {})
        for f in self.fields:
            self.params[f.key] = copy.deepcopy(f.default)
        self.needs = tuple(needs)
        self.note = note
        self.hint = hint          # one line for the operator, shown in the step editor

    def fresh_params(self) -> dict:
        """A copy of the default params for a new step, so edits to one step
        never reach another."""
        return copy.deepcopy(self.params)

    def field(self, key):
        return next((f for f in self.fields if f.key == key), None)


# Groups in the order the tab shows them.
GROUPS = ["Radio setup", "Comms", "Jammers", "IQ playback", "Tools generators",
          "Transponder", "Record and replay", "Secure"]


# shared field sets

def _rf_fields(tx=True):
    """Per-step tuning: RF center, TX attenuation (steps that key) and RX gain.
    Each is applied live (set_center, set_tx_atten, set_gain) before the step
    starts, without re-initializing the radio. Blank leaves the radio as the
    control card set it, which is what a panel does."""
    out = [
        Field("center_hz", "RF center (RX and TX)", "hz", None, optional=True,
              blank="keep radio's",
              help="retunes the radio (set_center) before the step: RX moves "
                   "here and TX moves with it; blank keeps the radio's current "
                   "center"),
    ]
    if tx:
        out.append(Field(
            "tx_center_hz", "TX frequency", "hz", None, optional=True,
            blank="same as RX",
            help="puts TX on its own frequency (set_tx_center) before keying; RX "
                 "stays on the RF center. Blank: TX on the RX frequency. To see it "
                 "on this radio's own spectrum keep it within half the sample "
                 "rate of the RX center"))
        out.append(Field(
            "tx_atten_db", "TX atten (dB)", "select", None, optional=True,
            choices=[(None, "leave as set")] + [(v, f"{v:g} dB")
                                                for v in TX_ATTEN_CHOICES],
            help="set_tx_atten before keying; leave as set keeps the radio's"))
    out.append(Field(
        "rx_gain_index", "RX gain", "select", None, optional=True,
        choices=[(None, "leave as set")] + RX_GAIN_CHOICES,
        help="set_gain before the step (G40 index, dB above minimum); an X40 "
             "starts at index 195; leave as set keeps the radio's"))
    return out


# jammer panel: TX power is a fraction of DAC full scale, shown as dBFS
_AMPL = [(1.0, "1.0 (0 dBFS)"), (0.71, "0.71 (-3 dBFS)"), (0.5, "0.5 (-6 dBFS)"),
         (0.35, "0.35 (-9 dBFS)"), (0.25, "0.25 (-12 dBFS)"),
         (0.18, "0.18 (-15 dBFS)"), (0.1, "0.1 (-20 dBFS)"),
         (0.05, "0.05 (-26 dBFS)")]
_LOOP = [(v, f"{v:g} s") for v in (0.05, 0.1, 0.25, 0.5, 1.0, 2.0)]
_DUTY = [(v, f"{v:g}") for v in (0.9, 0.75, 0.5, 0.25, 0.1, 0.05, 0.02, 0.01)]
_MODS = [(m, m) for m in ("qpsk", "bpsk", "8psk", "16qam", "64qam")]
_SYM_DUTY = [(v, f"{v:g}") for v in (1.0, 0.5, 0.25, 0.12, 0.06)]


def _jam_common():
    return [
        Field("ampl", "TX power (FS)", "select", 0.5, choices=_AMPL,
              help="peak as a fraction of DAC full scale"),
        Field("seconds", "loop length", "select", 0.5, choices=_LOOP,
              help="length of the waveform the radio loops; the step's start "
                   "and stop set how long it transmits"),
        Field("prf", "pulse PRF (Hz, 0 = CW)", "num", 0.0, min=0,
              help="0 transmits continuously; above 0 pulses at this rate"),
        Field("duty", "pulse duty", "select", 0.5, choices=_DUTY,
              show_if=lambda p: float(p.get("prf") or 0) > 0,
              help="fraction of each pulse period that is on"),
    ]


def _center_off(default=0.0):
    return Field("center", "offset from center", "hz", default,
                 help="where the jammer sits relative to the RF center")


_JAM_HINTS = {
    "barrage": "Fills the whole band the radio transmits (TX frequency +- half "
               "the sample rate). Its frequency is TX frequency, or the RF "
               "center when that is blank.",
}

_JAMMERS = [
    ("jammer.barrage", "Barrage: wideband noise", "barrage", []),
    ("jammer.noise", "Partial-band noise patch", "noise",
     [Field("bw", "bandwidth", "hz", 1.0e6), _center_off()]),
    ("jammer.tone", "Spot / CW tone", "tone",
     [Field("freq", "offset from center", "hz", 100.0e3)]),
    ("jammer.comb", "Comb: multi-tone", "comb",
     [Field("spacing", "tone spacing", "hz", 200.0e3),
      Field("n_tones", "number of tones", "num", 8, min=1, step=1),
      _center_off()]),
    ("jammer.swept", "Swept noise", "swept",
     [Field("span", "sweep span", "hz", 5.0e6),
      Field("sweep_period_s", "sweep period (s)", "num", 0.01, min=0.0001),
      Field("noise_bw", "sweep noise bw (0 = CW)", "hz", 0.0),
      _center_off()]),
    ("jammer.chirp", "LFM chirp", "chirp",
     [Field("f0", "chirp start", "hz", -5.0e6),
      Field("f1", "chirp stop", "hz", 5.0e6)]),
    ("jammer.modulated", "Modulated interferer", "modulated",
     [Field("mod", "constellation", "select", "qpsk", choices=_MODS),
      Field("sym_rate", "symbol rate", "hz", 100.0e3),
      Field("sym_duty", "symbol duty", "select", 1.0, choices=_SYM_DUTY),
      Field("rolloff", "RRC rolloff", "num", 0.35, min=0.0, max=1.0),
      _center_off()]),
]

# Tools generators share gen_test_waveform with the jammers; they are the
# clean test signals from the tools panel.
_TOOLS = [
    ("tools.tone", "Tone", "tone",
     [Field("freq", "offset from center", "hz", 100.0e3)]),
    ("tools.two_tone", "Two-tone", "two_tone",
     [Field("f1", "tone 1", "hz", -100.0e3), Field("f2", "tone 2", "hz", 100.0e3)]),
    ("tools.chirp", "Chirp", "chirp",
     [Field("f0", "chirp start", "hz", -400.0e3), Field("f1", "chirp stop", "hz", 400.0e3)]),
    ("tools.noise", "Noise", "noise",
     [Field("bw", "bandwidth", "hz", 1.0e6)]),
]


def _tool_common():
    return [
        Field("ampl", "amplitude (FS)", "num", 0.5, min=0.0, max=1.0, step=0.05),
        Field("seconds", "loop length (s)", "num", 0.1, min=0.001, max=5.0,
              help="length of the waveform the radio loops (the radio caps it "
                   "at 5 s); start and stop set how long it transmits"),
    ]


# transponder response: the tools panel's default
TRANSPONDER_RESPONSE = {"message": "ACK", "modulation": "FSK", "symbol_rate": 4800,
                        "tone_spacing_hz": 5000, "offset_hz": 0}


def _build() -> dict:
    cat = {}

    # Radio setup. Set sample rate re-initializes the radio to a new rate at its
    # scheduled time. Changing the rate means an init, which stops RX for a
    # moment, so place it in a quiet window before the step that needs the new
    # rate; by the time that step keys the radio is already at the right rate
    # and there is no delay. It does not key TX and needs no capability tag.
    cat["set_rate"] = Entry(
        "set_rate", "Radio setup", "Set sample rate", False, 2.0,
        [Field("sample_rate", "sample rate", "hz", DEFAULT_RATE_HZ,
               help="re-initializes the radio to this rate (an init, so RX "
                    "stops briefly); the radio's other settings are kept. Place "
                    "it before the step that needs the rate so there is no "
                    "delay when that step runs.")],
        needs=(), note="get_config then init at the new rate, then start_rx")

    # RX dwell: retune (and optionally re-gain) the receiver for a while, the
    # CFE autoSchedule.csv idea (Freq,Gain,Time). No TX; nothing to stop.
    cat["rx_dwell"] = Entry(
        "rx_dwell", "Radio setup", "RX dwell (retune)", False, 10.0,
        [Field("center_hz", "RF center (RX)", "hz", None, optional=True, blank="keep radio's",
               help="retunes the receiver (set_center) at the step start"),
         Field("rx_gain_index", "RX gain", "select", None, optional=True,
               choices=[(None, "leave as set")] + RX_GAIN_CHOICES,
               help="set_gain at the step start; the G40 hardware index")],
        needs=(TAG_RX,), note="set_center then set_gain, nothing at stop")

    for tech, label, kind, extra in _JAMMERS:
        cat[tech] = Entry(tech, "Jammers", label, True, 30.0,
                          _rf_fields() + _jam_common() + extra, fixed={"kind": kind},
                          note="gen_test_waveform then start_tx CONTINUOUS",
                          hint=_JAM_HINTS.get(kind, ""))
    for tech, label, kind, extra in _TOOLS:
        cat[tech] = Entry(tech, "Tools generators", label, True, 30.0,
                          _rf_fields() + _tool_common() + extra, fixed={"kind": kind},
                          note="gen_test_waveform then start_tx CONTINUOUS")

    # Comm waveform from a scenario JSON: the spec the playback panel and
    # gen_scenario_json.py read (a "signals" list, or DSSS "users"), built on
    # the radio by gen_scenario and keyed. The spec comes from this console
    # (uploaded in the step editor and kept inline in the step as "spec", so the
    # plan file carries it) or is a .json already on the radio ("spec_path").
    # The tab asks for it when the step is added; source records which it was.
    cat["scenario_json"] = Entry(
        "scenario_json", "Comms", "Comm waveform from a scenario JSON", True, 30.0,
        [Field("source", "scenario JSON from", "select", "console",
               choices=[("console", "console file (uploaded here)"),
                        ("radio", "file on the radio")]),
         Field("spec_path", "file on the radio", "text", "",
               help="a scenario .json on the radio (scenarios/json); use Choose "
                    "scenario JSON to pick it"),
         Field("match_radio_rate", "build at the radio's rate", "bool", True,
               help="rebuild the scenario at the radio's sample rate when the "
                    "step runs, so offsets and spacings land where the JSON "
                    "says; off plays a spec written for another rate as-is "
                    "(every frequency scales)"),
         Field("peak_fs", "peak (FS)", "num", 0.45, min=0.05, max=1.0, step=0.05,
               help="peak of the built waveform as a fraction of DAC full scale; "
                    "the signals keep their levels relative to each other. Overrides "
                    "a peak_fs in the JSON")]
        + _rf_fields(),
        fixed={"spec": None, "spec_name": ""},
        note="gen_scenario (spec, spec_inbox or spec_path) then start_tx CONTINUOUS",
        hint="A comm signal set from a scenario JSON: upload the file from this "
             "console or pick one on the radio. The radio builds the waveform "
             "when the step runs.")

    # The receive half of a comm waveform on another radio: the same scenario, built on
    # this radio without arming TX so it gets the decode plan, which it then loads. This
    # step never keys.
    cat["scenario_decode"] = Entry(
        "scenario_decode", "Comms", "Decode a comm waveform from a scenario JSON", False, 30.0,
        [Field("source", "scenario JSON from", "select", "console",
               choices=[("console", "console file (uploaded here)"),
                        ("radio", "file on the radio")]),
         Field("spec_path", "file on the radio", "text", "",
               help="a scenario .json on the radio (scenarios/json); use Choose "
                    "scenario JSON to pick it"),
         Field("match_radio_rate", "build at the radio's rate", "bool", True,
               help="build the plan at this radio's sample rate, so the decode "
                    "channels land where the JSON says"),
         Field("rx_atten_db", "RX attenuator (dB)", "num", None, optional=True, min=0, max=120, step=1,
               blank="leave as set",
               help="payload RX attenuator during this step. It goes back to 120 dB when the "
                    "step ends, and the radio refuses a value under its safe floor"),
         Field("center_hz", "RF center (RX)", "hz", None, optional=True, blank="keep radio's",
               help="retunes the receiver (set_center) at the step start"),
         Field("rx_gain_index", "RX gain", "select", None, optional=True,
               choices=[(None, "leave as set")] + RX_GAIN_CHOICES,
               help="set_gain at the step start; the G40 hardware index")],
        fixed={"spec": None, "spec_name": ""}, needs=(TAG_RX,),
        note="gen_scenario without load_tx, set_plan, RX attenuator, start_rx. Nothing keys",
        hint="Use the same scenario JSON as the Comm waveform step on the transmitting "
             "radio. Decodes appear in the Decodes table of this radio.")

    # IQ playback: an IQ file on the radio; path is the file's path there. The
    # tab asks for the file when the step is added. A scenario JSON is the
    # Comm waveform entry above; mission_file moves older plans that used one
    # here over to it when they load.
    cat["iq_playback"] = Entry(
        "iq_playback", "IQ playback", "IQ playback (file on the radio)", True, 30.0,
        [Field("path", "file on the radio", "text", "",
               help="use Choose IQ file to pick from the radio")]
        + _rf_fields(),
        fixed={"source": "file"},
        note="set_tx_waveform then start_tx")

    # Transponder: watch for a signal, then answer it. With ARM off it only
    # emulates the answer (no RF), as the tools panel does by default.
    cat["transponder"] = Entry(
        "transponder", "Transponder", "Transponder (detect and respond)", True, 30.0,
        [Field("trigger_snr_db", "trigger SNR (dB)", "num", 15.0, min=0),
         Field("emulate", "emulate TX", "bool", True,
               help="build the answer and time it without radiating"),
         Field("armed", "ARM (real TX)", "bool", False,
               help="actually transmit the answer"),
         Field("response", "response message (JSON)", "json",
               dict(TRANSPONDER_RESPONSE)),
         Field("dwell", "dwell (s)", "num", 0.05, min=0.001),
         Field("ring", "ring depth", "num", 16, min=1, step=1)]
        + _rf_fields(),
        note="transponder")

    # Record and replay. Record is receive-only so it does not radiate; replay
    # keys the recording back out, so it radiates.
    cat["record"] = Entry(
        "record", "Record and replay", "Record RX", False, 1.0,
        [Field("band", "record", "select", "full",
               choices=[("full", "Full band"), ("range", "A band (low to high)")]),
         Field("f_lo_hz", "band low", "hz", 0.0, show_if=("band", ("range",))),
         Field("f_hi_hz", "band high", "hz", 0.0, show_if=("band", ("range",))),
         Field("label", "label", "text", "", help="up to 16 characters")]
        + _rf_fields(tx=False),
        needs=(TAG_RX,), note="record_iq (length = step duration, radio max ~5 s)")
    cat["replay"] = Entry(
        "replay", "Record and replay", "Replay a recording", True, 10.0,
        [Field("name", "recording", "text", "",
               help="use Choose recording to pick from the radio"),
         Field("gain_db", "gain vs captured (dB)", "num", 0.0, step=0.5),
         Field("mode", "replay", "select", "SINGLE_SHOT",
               choices=[("SINGLE_SHOT", "Once"), ("CONTINUOUS", "Loop")])]
        + _rf_fields(),
        note="replay_recording")

    # Secure messaging, where the radio has it enabled. No panel sends one and
    # the runner does not drive it.
    cat["secure.send"] = Entry(
        "secure.send", "Secure", "Secure message send", True, 2.0,
        [Field("recipient", "recipient", "text", ""),
         Field("body", "message", "text", "")],
        needs=(TAG_TX, TAG_SECURE), note="secure send (the runner does not drive it)")
    return cat


CATALOG = _build()


def groups_for_tab() -> list:
    """The catalog as [(group, [Entry, ...]), ...] in GROUPS order, for the
    left-hand catalog column."""
    out = []
    for g in GROUPS:
        entries = [e for e in CATALOG.values() if e.group == g]
        if entries:
            out.append((g, entries))
    return out


def is_known(technique: str) -> bool:
    return technique in CATALOG


def get(technique: str) -> Entry | None:
    return CATALOG.get(technique)


def receives(technique: str) -> bool:
    """The step listens on its radio's RX port: an RX technique, or the
    transponder (it receives before it answers). Such a step cannot have RX
    set to none."""
    e = CATALOG.get(technique)
    return technique == "transponder" or (e is not None and TAG_RX in e.needs)
