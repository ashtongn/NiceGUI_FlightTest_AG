"""
dpmg.secure.replay: receive-side anti-replay.

One window per (sender16, generation, key day). The ordinal of a frame is
slot * 1024 + counter (the counter has 10 bits on air). An RFC 6479-style
bitmap of WINDOW ordinals behind the highest accepted ordinal records what has
been seen; older than the window or already seen: REPLAY. The window is
updated only after the AEAD tag verified (the caller guarantees the order).

Freshness: the caller also checks |slot - local_slot| <= freshness_slots
against its own clock before spending a decryption on the frame (STALE).

Generation tracking: a receiver learns a sender's generation from a long
header. A short header whose GEN_LSB does not match the tracked generation (or
generation+1, one reboot) is not decrypted at all (GEN_UNKNOWN). A long header
with a generation below the tracked one is a replay of an earlier boot
(REPLAY_GEN).
"""
from __future__ import annotations

from dataclasses import dataclass, field

WINDOW = 1024                       # ordinals remembered behind the newest
COUNTER_BITS = 10


def ordinal(slot: int, counter: int) -> int:
    return (int(slot) << COUNTER_BITS) | (int(counter) & ((1 << COUNTER_BITS) - 1))


@dataclass
class Window:
    highest: int = -1
    bitmap: int = 0                 # bit i set = ordinal (highest - i) seen
    accepted: int = 0
    rejected: int = 0

    def check(self, ordv: int) -> bool:
        """True if ordv is new (not yet seen and inside the window). Does not
        record it: call accept() after the tag verifies."""
        if self.highest < 0:
            return True
        if ordv > self.highest:
            return True
        delta = self.highest - ordv
        if delta >= WINDOW:
            return False
        return not (self.bitmap >> delta) & 1

    def accept(self, ordv: int) -> None:
        if self.highest < 0 or ordv > self.highest:
            shift = (ordv - self.highest) if self.highest >= 0 else WINDOW
            if shift >= WINDOW:
                self.bitmap = 1
            else:
                self.bitmap = ((self.bitmap << shift) | 1) & ((1 << WINDOW) - 1)
            self.highest = ordv
        else:
            self.bitmap |= 1 << (self.highest - ordv)
        self.accepted += 1


@dataclass
class SenderState:
    generation: int | None = None   # last authenticated generation
    windows: dict = field(default_factory=dict)    # (generation, day_iso): Window
    long_headers: int = 0

    def peek(self, generation: int, day_iso: str) -> "Window | None":
        """The window for (generation, day) if one exists. Never creates one:
        unauthenticated candidates must not grow or evict state."""
        return self.windows.get((int(generation), day_iso))

    def window(self, generation: int, day_iso: str) -> Window:
        """Create-or-get, called only after a tag verified for that generation."""
        key = (int(generation), day_iso)
        w = self.windows.get(key)
        if w is None:
            w = Window()
            self.windows[key] = w
            # keep the table small: only the newest two authenticated generations
            # (and at most 3 days each) survive
            gens = sorted({g for g, _d in self.windows}, reverse=True)[:2]
            for k in list(self.windows):
                if k[0] not in gens:
                    self.windows.pop(k, None)
            per_gen = sorted((k for k in self.windows if k[0] == key[0]), key=lambda k: k[1])
            for k in per_gen[:-3]:
                self.windows.pop(k, None)
        return w


class ReplayTable:
    """All senders seen by this receiver."""

    def __init__(self):
        self.senders: dict = {}      # sender16: SenderState

    def sender(self, sender16: int) -> SenderState:
        s = self.senders.get(int(sender16))
        if s is None:
            s = SenderState()
            self.senders[int(sender16)] = s
        return s

    def status(self) -> dict:
        out = {}
        for sid, s in self.senders.items():
            wins = {f"{g}/{d}": {"highest": w.highest, "accepted": w.accepted, "rejected": w.rejected}
                    for (g, d), w in s.windows.items()}
            out[f"{sid:04x}"] = {"generation": s.generation, "windows": wins}
        return out
