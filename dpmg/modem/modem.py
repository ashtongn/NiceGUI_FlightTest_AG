"""
modem.py: half-duplex packet modem built on the existing waveform factories
and frame_codec framing. Modulation-agnostic: FSK, DSSS, and CSS/LoRa all plug
in through ModemConfig, so the same RX to decode to TX-response loop drives any of
them by swapping config.

Data path:
    TX:  text/bytes then frame_codec.encode then get_modulator().modulate then IQ
    RX:  IQ then get_demodulator().demodulate then frame_codec.find_and_decode then msg

A Responder maps a decoded message to an optional reply, which is the preset
communication stream the transceive demo runs. run_link() drives a full
scripted exchange between two Modem nodes through an optional channel and
reports every hop, so the modem can be proven end to end before any RF keying.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from dpmg.framing import frame_codec
from dpmg.modulation.modulator import get_modulator
from dpmg.demodulation.demodulator import get_demodulator


# Config: one dataclass carries the modulation choice + every waveform param.
# rx_port/tx_port are informational routing hints the service uses to pick the
# SDR handles; they do not affect the software data path.

@dataclass
class ModemConfig:
    modulation: str = "FSK"
    symbol_rate: float = 4800.0
    samples_per_bit: int = 40
    tone0: float = 1200.0
    tone1: float = 2200.0
    tone2: float = 1400.0
    tone3: float = 2400.0
    gfsk_bw_hz: float = 1440.0        # BT 0.3 at the default 4800 baud
    amplitude: float = 0.8
    # spread-spectrum extras (only read for the matching modulation)
    spread_factor: int = 15
    pn_seed: int = 1
    code_spec: Optional[dict] = None   # DSSS spreading code (seed/m/gold/custom/lfsr)
    code_spec_q: Optional[dict] = None # Second code on the Q rail (qpsk/oqpsk)
    dsss_modem: str = "bpsk"           # DSSS carrier: bpsk | qpsk | oqpsk | pi4qpsk
    css_sf: int = 7
    css_bw: float = 125_000.0
    css_os: int = 1
    dibit_mapping: str = "binary"      # 4FSK dibit to tone mapping (TX and RX)
    # SDR routing (service side): e.g. rx_port="TRx2A", tx_port="TRx1A"
    rx_port: str = ""
    tx_port: str = ""
    # dead air padded around each burst so demod has settling room
    lead_pad_bits: int = 8
    tail_pad_bits: int = 4
    # forward error correction over the (len+payload+crc) frame body; sync stays
    # uncoded. None = off. e.g. {"type": "conv", "rate": "1/2"}
    fec: Optional[dict] = None
    # Seal the body with AES-256-GCM-SIV (frame_codec.encode_secure)
    # and open sealed frames on receive. None/{} = legacy frame. Needs the
    # DPMG_SECURE_MSG flag and a key file (secure_frame.get_context()).
    secure: Optional[dict] = None

    def _mod_kwargs(self) -> dict:
        return dict(
            spread_factor=self.spread_factor,
            pn_seed=self.pn_seed,
            code_spec=self.code_spec,
            code_spec_q=self.code_spec_q,
            dsss_modem=self.dsss_modem,
            css_sf=self.css_sf,
            css_bw=self.css_bw,
            css_os=self.css_os,
        )


class Modem:
    """Frames + modulates on TX, demodulates + de-frames on RX. Stateless per
    burst; hold one per SDR direction."""

    def __init__(self, config: ModemConfig):
        self.cfg = config
        self._mod = get_modulator(
            config.modulation, config.symbol_rate, config.samples_per_bit,
            tone0=config.tone0, tone1=config.tone1, tone2=config.tone2,
            tone3=config.tone3, amplitude=config.amplitude,
            gfsk_bw_hz=config.gfsk_bw_hz, dibit_mapping=config.dibit_mapping,
            **config._mod_kwargs())
        self.sample_rate = int(getattr(self._mod, "sample_rate",
                               config.symbol_rate * config.samples_per_bit))
        self._demod = get_demodulator(
            config.modulation, self.sample_rate, config.symbol_rate,
            config.samples_per_bit, tone0=config.tone0, tone1=config.tone1,
            tone2=config.tone2, tone3=config.tone3,
            gfsk_bw_hz=config.gfsk_bw_hz, dibit_mapping=config.dibit_mapping,
            **config._mod_kwargs())

    def transmit(self, payload) -> np.ndarray:
        """payload: str or bytes: complex IQ burst (with sync/len/crc frame)."""
        data = payload.encode("utf-8", errors="replace") if isinstance(payload, str) \
            else bytes(payload)
        if self.cfg.secure:
            frame_bits = frame_codec.encode_secure(data, self.cfg.fec or None)
        elif self.cfg.fec:
            frame_bits = frame_codec.encode_fec(data, self.cfg.fec)
        else:
            frame_bits = frame_codec.encode_message(data)
        iq = np.asarray(self._mod.modulate(frame_bits).iq_samples,
                        dtype=np.complex64)
        return self._pad(iq)

    def receive(self, iq: np.ndarray) -> frame_codec.FrameResult:
        """complex IQ: FrameResult (valid only if sync locks and CRC passes).

        QPSK-DSSS carries a 4-fold phase ambiguity that find_and_decode's
        normal+inverted pair can't fully cover, so all four 90-deg rotations
        of the input are tried and the first that validates is returned."""
        x = np.asarray(iq, dtype=np.complex64)
        _find = frame_codec.find_and_decode
        if self.cfg.secure:                                   # Sealed body
            if self.cfg.fec:
                _find = lambda b, f=self.cfg.fec: frame_codec.open_sealed(
                    frame_codec.fec_find_and_decode(b, f, layout=frame_codec.SECURE))
            else:
                _find = lambda b: frame_codec.open_sealed(frame_codec.secure_find_and_decode(b))
        _carrier = getattr(self._demod, "modem", "") if hasattr(self._demod, "_despread") else ""
        if _carrier and _carrier != "bpsk":
            # 4 x 90 deg for the QPSK family, 8 x 45 deg for
            # pi/4-QPSK (its grid parity is a 45-deg ambiguity)
            from dpmg.modulation.spread_spectrum import dsss_rotations
            for rot in dsss_rotations(_carrier):
                bits = self._demod.demodulate(
                    (x * np.exp(1j * rot)).astype(np.complex64)).bit_string
                fr = _find(bits)
                if fr.valid:
                    return fr
            return fr
        bits = self._demod.demodulate(x).bit_string
        return _find(bits)

    def _stride(self) -> int:
        # CSS/LoRa slices fixed-length chirp symbols from sample 0 with no
        # preamble sync, so any lead pad must be a whole number of symbols or
        # every symbol misaligns. Other waveforms align on the bit stride.
        m = self._mod
        if hasattr(m, "N") and hasattr(m, "os"):
            return int(m.N * m.os)
        return int(self.cfg.samples_per_bit)

    def _pad(self, iq: np.ndarray) -> np.ndarray:
        stride = self._stride()
        lead = np.zeros(max(0, self.cfg.lead_pad_bits) * stride, dtype=np.complex64)
        tail = np.zeros(max(0, self.cfg.tail_pad_bits) * stride, dtype=np.complex64)
        return np.concatenate([lead, iq, tail])


# Responder: decoded message: optional reply. Rules are tried in order; the
# first whose predicate matches produces the reply. This is where the preset
# communication stream lives.

@dataclass
class Rule:
    match: Callable[[str], bool]
    reply: Callable[[str], Optional[str]]


class Responder:
    def __init__(self, rules: Optional[list] = None,
                 default: Optional[Callable[[str], Optional[str]]] = None):
        self.rules = rules or []
        self.default = default

    def respond(self, message: str) -> Optional[str]:
        for r in self.rules:
            if r.match(message):
                return r.reply(message)
        return self.default(message) if self.default else None


def _prefix(tag: str) -> Callable[[str], bool]:
    return lambda m: m.startswith(tag)


def ack_responder() -> Responder:
    """Preset link-check policy: PING/PONG, TEXT/ACK, echo BYE, ACK anything else."""
    return Responder(
        rules=[
            Rule(_prefix("PING "), lambda m: "PONG " + m[5:]),
            Rule(_prefix("TEXT "), lambda m: "ACK " + m[5:]),
            Rule(lambda m: m == "BYE", lambda m: "BYE"),
        ],
        default=lambda m: "ACK " + m,
    )


# Canned initiator scripts (what the master node transmits, in order).
PRESET_SCRIPTS = {
    "linkcheck": ["PING 1", "PING 2", "TEXT HELLO", "TEXT MODEM-UP", "BYE"],
    "short": ["PING 1", "BYE"],
}


@dataclass
class Hop:
    kind: str          # "tx" or "rx"
    node: str          # "A" or "B"
    text: str
    ok: bool = True


def run_link(initiator: Modem, responder_modem: Modem,
             responder: Responder, script: list,
             channel: Optional[Callable[[np.ndarray], np.ndarray]] = None,
             expect: Optional[Callable[[str], Optional[str]]] = None):
    """Drive a full scripted exchange. For each message the initiator (A) sends,
    the responder (B) decodes it, forms a reply, and sends it back; A decodes the
    reply. Returns (hops, ok). channel() perturbs each burst (noise/attenuation);
    expect(sent) returns the reply A should get (defaults to the responder policy).
    """
    ch = channel or (lambda x: x)
    hops: list = []
    all_ok = True
    for sent in script:
        # A: B
        rx_b = responder_modem.receive(ch(initiator.transmit(sent)))
        hops.append(Hop("tx", "A", sent))
        got = rx_b.message if rx_b.valid else ""
        hops.append(Hop("rx", "B", got, rx_b.valid and got == sent))
        if not (rx_b.valid and got == sent):
            all_ok = False
            continue
        reply = responder.respond(got)
        if reply is None:
            continue
        # B: A
        rx_a = initiator.receive(ch(responder_modem.transmit(reply)))
        hops.append(Hop("tx", "B", reply))
        got_a = rx_a.message if rx_a.valid else ""
        want = expect(sent) if expect else reply
        ok_a = rx_a.valid and got_a == want
        hops.append(Hop("rx", "A", got_a, ok_a))
        if not ok_a:
            all_ok = False
    return hops, all_ok
