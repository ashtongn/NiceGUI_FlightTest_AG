"""
Sync search and flywheel over a continuous soft bit stream.

The stream is sync(S) + block(A) + sync + block + ... with S and A fixed.
Searching: every offset whose S bits match the sync word within
max_errors is tried; the block behind it goes to try_block (deinterleave,
decode, CRC, ARQ) and the first one that passes locks the deframer.
Locked: the next block is tried at exactly the expected position whatever the
sync looks like (a frame whose CRC passes keeps the lock even with a damaged
sync). A miss advances by one block and also re-runs the search over what is
buffered, so a slip of a few bits re-locks on the next frame instead of
costing miss_limit frames; miss_limit misses in a row drop to searching.

Everything is numpy over the whole buffer, no per bit Python loop. Bits
left unprocessed beyond max_buffer_blocks are shed (oldest first), which
only happens when try_block cannot keep up with the input.
"""
from __future__ import annotations

from typing import Callable

import numpy as np

from dpmg.framing import frame_codec


def sync_pattern(sync_hex: str) -> np.ndarray:
    """+1/-1 pattern of the sync word (llr sign convention: +1 = bit 1)."""
    bits = frame_codec.normalize_sync(sync_hex)
    return np.where(np.frombuffer(bits.encode("ascii"), np.uint8) == ord("1"), 1.0, -1.0)


class Deframer:
    def __init__(self, sync_hex: str, block_bits: int, max_errors: int = 3, miss_limit: int = 4,
                 max_buffer_blocks: int = 64):
        self.sync = sync_pattern(sync_hex)
        self.S = len(self.sync)
        self.A = int(block_bits)
        self.max_errors = int(max_errors)
        self.miss_limit = int(miss_limit)
        self.max_buffer = max(4, int(max_buffer_blocks)) * (self.S + self.A)
        self.buf = np.zeros(0, dtype=np.float32)
        self.locked = False
        self.misses = 0
        self.stats = {"frames_ok": 0, "frames_bad": 0, "locks": 0, "unlocks": 0,
                      "sync_hits": 0, "bits_in": 0, "bits_dropped": 0}

    # helpers
    def _sync_distances(self, hard: np.ndarray, n_offsets: int) -> np.ndarray:
        win = np.lib.stride_tricks.sliding_window_view(hard[:n_offsets + self.S - 1], self.S)
        return np.count_nonzero(win != (self.sync > 0), axis=1)

    def _consume(self, n: int) -> None:
        self.buf = self.buf[n:]

    def _search(self, try_block: Callable[[np.ndarray], bool], start: int = 0) -> bool:
        """Try every sync candidate from `start`; on success consume through the
        block and return True. On failure consume up to the last testable offset."""
        L = self.S + self.A
        n_offsets = len(self.buf) - L + 1
        if n_offsets <= start:
            return False
        hard = self.buf > 0
        dist = self._sync_distances(hard, n_offsets)
        cands = np.flatnonzero(dist[start:] <= self.max_errors) + start
        for off in cands:
            self.stats["sync_hits"] += 1
            if try_block(self.buf[off + self.S: off + L]):
                self.stats["frames_ok"] += 1
                self.stats["bits_dropped"] += int(off)
                self._consume(int(off) + L)
                return True
            self.stats["frames_bad"] += 1
        self.stats["bits_dropped"] += n_offsets
        self._consume(n_offsets)
        return False

    # main entry
    def feed(self, llr: np.ndarray, try_block: Callable[[np.ndarray], bool]) -> int:
        """Append soft bits and process every whole block available. Returns the
        number of frames try_block accepted during this call."""
        x = np.asarray(llr, dtype=np.float32).reshape(-1)
        self.stats["bits_in"] += len(x)
        self.buf = np.concatenate([self.buf, x]) if len(self.buf) else x.copy()
        L = self.S + self.A
        accepted = 0
        while len(self.buf) >= L:
            if self.locked:
                if try_block(self.buf[self.S:L]):
                    self.stats["frames_ok"] += 1
                    self.misses = 0
                    accepted += 1
                    self._consume(L)
                    continue
                self.stats["frames_bad"] += 1
                self.misses += 1
                if self.misses >= self.miss_limit:
                    self.locked = False
                    self.stats["unlocks"] += 1
                # a slip or a bad frame: look for the next sync beyond offset 0
                if self._search(try_block, start=1):
                    accepted += 1
                    if not self.locked:
                        self.locked = True
                        self.stats["locks"] += 1
                    self.misses = 0
                    continue
                break
            if self._search(try_block):
                self.locked = True
                self.misses = 0
                self.stats["locks"] += 1
                accepted += 1
                continue
            break
        if len(self.buf) > self.max_buffer:                 # left over after processing: shed the oldest
            self.stats["bits_dropped"] += len(self.buf) - self.max_buffer
            self.buf = self.buf[-self.max_buffer:]
            self.locked = False
        return accepted
