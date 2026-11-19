"""
channels.py: Channel configuration model for the DPMG RF channelizer.

Ties each DDC channel (arbitrary center + bandwidth) to a demodulator and the
parameters that demod needs. Supports up to MAX_CHANNELS channels, validates that
each channel fits inside the captured band and is compatible with the common
output rate (Fs_out), and builds the matching demodulator via the existing
get_demodulator() factory in dpmg.demodulation.

This is config + glue only. The DSP lives in ddc.py; the demods live in
dpmg/demodulation/demodulator.py. Run directly to self-validate:  python3 -m dpmg.dsp.channels
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

MAX_CHANNELS = 30


# Demod parameter schema.
# Every demod takes (sample_rate, symbol_rate, samples_per_bit). On top of that,
# FSK-family demods need tone frequencies. Because the DDC shifts each channel to
# 0 Hz (baseband), the FSK tones are specified as a spacing and placed
# symmetrically around DC: not as absolute RF. The schema below lists the extra
# user-facing params each type needs beyond symbol_rate.

DEMOD_SCHEMA: dict[str, list[str]] = {
    "BPSK":  [],                          # symbol_rate only
    "QPSK":  [],
    "FSK":   ["tone_spacing_hz"],         # tones at +/- spacing/2 around baseband
    "4FSK":  ["tone_spacing_hz"],         # 4 tones evenly spaced, centered on DC
    "GFSK":  ["tone_spacing_hz", "gfsk_bw_hz"],
}

# default param values for the UI to pre-fill
DEMOD_DEFAULTS: dict[str, dict] = {
    "BPSK":  {},
    "QPSK":  {},
    "FSK":   {"tone_spacing_hz": 10_000.0},
    "4FSK":  {"tone_spacing_hz": 10_000.0},
    "GFSK":  {"tone_spacing_hz": 10_000.0, "gfsk_bw_hz": 4_000.0},
}

SUPPORTED_DEMODS = list(DEMOD_SCHEMA.keys())


class ChannelConfigError(ValueError):
    pass


@dataclass
class ChannelConfig:
    """One channelizer channel: a DDC spec + a demod + that demod's params."""
    f_center_hz: float
    bw_hz: float
    demod_type: str
    symbol_rate: float
    demod_params: dict = field(default_factory=dict)
    enabled: bool = True
    name: str = ""

    def validate(self, capture_center_hz: float, fs_in: float, fs_out: float) -> "ChannelConfig":
        """Check the channel fits the captured band and the common output rate."""
        if self.demod_type not in DEMOD_SCHEMA:
            raise ChannelConfigError(
                f"unknown demod '{self.demod_type}' (have: {', '.join(SUPPORTED_DEMODS)})")

        if self.bw_hz <= 0:
            raise ChannelConfigError(f"channel '{self.name}': bandwidth must be > 0")
        if self.symbol_rate <= 0:
            raise ChannelConfigError(f"channel '{self.name}': symbol rate must be > 0")

        # channel must fit inside the captured band [Fc - Fs/2, Fc + Fs/2]
        lo = self.f_center_hz - self.bw_hz / 2.0
        hi = self.f_center_hz + self.bw_hz / 2.0
        band_lo = capture_center_hz - fs_in / 2.0
        band_hi = capture_center_hz + fs_in / 2.0
        if lo < band_lo or hi > band_hi:
            raise ChannelConfigError(
                f"channel '{self.name}' [{lo/1e6:.4f}-{hi/1e6:.4f} MHz] is outside the "
                f"captured band [{band_lo/1e6:.4f}-{band_hi/1e6:.4f} MHz]")

        # bandwidth must fit within the common output rate (Nyquist)
        if self.bw_hz > fs_out:
            raise ChannelConfigError(
                f"channel '{self.name}' bw {self.bw_hz/1e3:g} kHz exceeds the common "
                f"output rate Fs_out {fs_out/1e3:g} kHz -- raise Fs_out or narrow the channel")

        # need at least ~2 samples per symbol at Fs_out for any demod to work
        sps = fs_out / self.symbol_rate
        if sps < 2.0:
            raise ChannelConfigError(
                f"channel '{self.name}': symbol rate {self.symbol_rate:g} too high for "
                f"Fs_out {fs_out/1e3:g} kHz (only {sps:.1f} samples/symbol, need >= 2)")

        # FSK tone spacing must fit inside the channel bandwidth
        if "tone_spacing_hz" in DEMOD_SCHEMA[self.demod_type]:
            spacing = float(self.demod_params.get("tone_spacing_hz", 0.0))
            if spacing <= 0:
                raise ChannelConfigError(
                    f"channel '{self.name}': {self.demod_type} needs tone_spacing_hz > 0")
            n_tones = 4 if self.demod_type == "4FSK" else 2
            total_tone_span = spacing * (n_tones - 1)
            if total_tone_span > self.bw_hz:
                raise ChannelConfigError(
                    f"channel '{self.name}': {self.demod_type} tone span "
                    f"{total_tone_span/1e3:g} kHz exceeds channel bw {self.bw_hz/1e3:g} kHz")
        return self

    def samples_per_bit(self, fs_out: float) -> int:
        return max(2, int(round(fs_out / self.symbol_rate)))

    def baseband_tones(self) -> list[float]:
        """FSK/4FSK tone frequencies as baseband offsets around DC (Hz).

        The DDC centers the channel at 0 Hz, so tones are symmetric about DC.
        2 tones:  [-s/2, +s/2]
        4 tones:  [-3s/2, -s/2, +s/2, +3s/2]
        """
        if "tone_spacing_hz" not in DEMOD_SCHEMA.get(self.demod_type, []):
            return []
        s = float(self.demod_params.get("tone_spacing_hz", 0.0))
        if self.demod_type == "4FSK":
            return [-1.5 * s, -0.5 * s, 0.5 * s, 1.5 * s]
        return [-0.5 * s, 0.5 * s]


def build_demod(cfg: ChannelConfig, fs_out: float):
    """Construct the demodulator for a channel via the existing factory.

    Imports get_demodulator lazily so this module stays import-safe even where the
    demodulation package isn't on the path (e.g. the standalone self-test).
    """
    from dpmg.demodulation import get_demodulator

    spb = cfg.samples_per_bit(fs_out)
    kwargs: dict = {}
    tones = cfg.baseband_tones()
    if tones:
        for i, tval in enumerate(tones):
            kwargs[f"tone{i}"] = tval
    if cfg.demod_type == "GFSK":
        kwargs["gfsk_bw_hz"] = float(cfg.demod_params.get("gfsk_bw_hz", 200.0))
    return get_demodulator(cfg.demod_type, fs_out, cfg.symbol_rate, spb, **kwargs)


@dataclass
class ChannelSet:
    """A validated collection of up to MAX_CHANNELS channels."""
    channels: list[ChannelConfig] = field(default_factory=list)

    def add(self, cfg: ChannelConfig) -> None:
        if len(self.channels) >= MAX_CHANNELS:
            raise ChannelConfigError(f"at most {MAX_CHANNELS} channels allowed")
        self.channels.append(cfg)

    def remove(self, idx: int) -> None:
        if 0 <= idx < len(self.channels):
            self.channels.pop(idx)

    def enabled(self) -> list[tuple[int, ChannelConfig]]:
        return [(i, c) for i, c in enumerate(self.channels) if c.enabled]

    def validate_all(self, capture_center_hz: float, fs_in: float, fs_out: float) -> list[str]:
        """Validate every channel; return a list of error strings (empty = all good)."""
        errs: list[str] = []
        for i, c in enumerate(self.channels):
            try:
                c.validate(capture_center_hz, fs_in, fs_out)
            except ChannelConfigError as e:
                errs.append(f"[ch{i}] {e}")
        return errs

    def required_fs_out(self) -> float:
        """Common output rate: >= widest channel bandwidth and an integer multiple of
        every channel's symbol rate (so samples/symbol is integer: fractional sps
        makes the demods drift off the symbols and never lock).

        Picks the smallest integer-sps rate that clears the widest bandwidth.
        """
        chans = [c for c in self.channels if c.enabled]
        if not chans:
            return 0.0
        widest_bw = max(c.bw_hz for c in chans)
        symrates = [c.symbol_rate for c in chans if c.symbol_rate > 0]
        if not symrates:
            return widest_bw
        # the LCM of the symbol rates: any integer multiple of it gives integer sps
        # for all channels. Find the smallest multiple of that LCM that is >= widest_bw.
        from math import gcd
        def lcm(a, b):
            return a * b // gcd(a, b)
        base = 1
        for sr in symrates:
            base = lcm(base, int(round(sr)))
        # smallest multiple of base that clears the widest bandwidth
        import math
        mult = max(1, math.ceil(widest_bw / base))
        fs_out = base * mult
        return float(fs_out)


# Self-validation

def _selftest() -> int:
    fs_in = 50_000_000.0
    fc = 425_000_000.0
    fs_out = 250_000.0
    ok = True

    cs = ChannelSet()
    cs.add(ChannelConfig(fc - 10e6, 100_000.0, "BPSK", 25_000.0, name="bpsk1"))
    cs.add(ChannelConfig(fc + 5e6, 200_000.0, "FSK", 9_600.0,
                         {"tone_spacing_hz": 10_000.0}, name="fsk1"))
    cs.add(ChannelConfig(fc + 15e6, 60_000.0, "4FSK", 4_800.0,
                         {"tone_spacing_hz": 10_000.0}, name="4fsk1"))

    errs = cs.validate_all(fc, fs_in, fs_out)
    print("valid set errors:", errs if errs else "(none)")
    ok = ok and not errs

    # samples-per-bit + baseband tone placement
    for i, c in cs.enabled():
        spb = c.samples_per_bit(fs_out)
        print(f"ch{i} {c.demod_type:5s} sym={c.symbol_rate:g} -> spb={spb}  "
              f"tones={[f'{t/1e3:+g}k' for t in c.baseband_tones()]}")

    # negative cases: each should raise 
    print("-" * 60)
    bad_cases = [
        ("out of band", ChannelConfig(fc + 30e6, 50e3, "BPSK", 10e3, name="oob")),
        ("bw > Fs_out", ChannelConfig(fc, 400_000.0, "BPSK", 10e3, name="wide")),
        ("sym too high", ChannelConfig(fc, 50e3, "BPSK", 200_000.0, name="fast")),
        ("tone span > bw", ChannelConfig(fc, 20e3, "4FSK", 4_800.0,
                                         {"tone_spacing_hz": 10_000.0}, name="tones")),
        ("unknown demod", ChannelConfig(fc, 50e3, "WIDGET", 10e3, name="bad")),
    ]
    for label, c in bad_cases:
        try:
            c.validate(fc, fs_in, fs_out)
            print(f"  [{label}] did NOT raise -> FAIL")
            ok = False
        except ChannelConfigError as e:
            print(f"  [{label}] correctly rejected: {str(e)[:70]}")

    print("-" * 60)
    print(f"required Fs_out (widest enabled bw) = {cs.required_fs_out()/1e3:g} kHz")
    print("=" * 60)
    print("SELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
