"""
transceive.py: the RX to decode to TX-response controller. This is the modem's
event loop, factored out of any radio so it can be proven with a simulated
pair and then dropped onto the G40 with real SDR callbacks.

The controller owns an RX Modem and a TX Modem (usually the same modulation on
different RF ports). on_rx() decodes an incoming burst; on a valid, newly-seen
frame it asks the Responder for a reply and, only if armed, transmits it via
the tx_send callback. Disarmed (the default) it decodes and reports the reply
it would send but never keys TX, so the loop can run against live RX with no
radiation until someone with a spectrum analyzer arms it.

Port routing lives in TransceiveConfig: rx_port/tx_port and same_card tell the
service which SDR handles to use and whether dual-channel mode is required
(same card, two ports then NV100 dual-channel; different cards then independent).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from dpmg.framing import frame_codec
from dpmg.modem.modem import Modem, ModemConfig, Responder, ack_responder


@dataclass
class TransceiveConfig:
    rx: ModemConfig
    tx: ModemConfig
    same_card: bool = False          # rx/tx ports on one card: dual-channel mode
    dedup: int = 4                   # suppress re-reply to the last N frames

    @property
    def needs_dual_channel(self) -> bool:
        return self.same_card and self.rx.rx_port != self.tx.tx_port


@dataclass
class TransceiveEvent:
    decoded: str = ""
    reply: Optional[str] = None
    transmitted: bool = False
    duplicate: bool = False
    valid: bool = False


class TransceiveController:
    def __init__(self, cfg: TransceiveConfig, responder: Optional[Responder] = None,
                 tx_send: Optional[Callable[[np.ndarray], None]] = None,
                 on_event: Optional[Callable[[TransceiveEvent], None]] = None):
        self.cfg = cfg
        self.rx_modem = Modem(cfg.rx)
        self.tx_modem = Modem(cfg.tx)
        self.responder = responder or ack_responder()
        self.tx_send = tx_send
        self.on_event = on_event
        self.armed = False
        self._recent: deque = deque(maxlen=max(1, cfg.dedup))
        self.stats = {"decoded": 0, "replied": 0, "transmitted": 0, "duplicates": 0}

    def arm(self, on: bool = True) -> None:
        """Enable actual TX keying. Leave False until a spectrum analyzer is on
        the TX port. Disarmed, the loop still decodes and computes replies."""
        self.armed = bool(on)

    def on_rx(self, iq: np.ndarray) -> Optional[TransceiveEvent]:
        """Feed one received IQ burst. Returns an event if a frame decoded,
        else None. Transmits the reply only when armed and tx_send is wired."""
        res = self.rx_modem.receive(iq)
        if not res.valid:
            return None
        msg = res.message
        self.stats["decoded"] += 1
        if msg in self._recent:                 # same frame seen again: reply once
            self.stats["duplicates"] += 1
            return self._emit(TransceiveEvent(decoded=msg, duplicate=True, valid=True))
        self._recent.append(msg)
        reply = self.responder.respond(msg)
        ev = TransceiveEvent(decoded=msg, reply=reply, valid=True)
        if reply is not None:
            self.stats["replied"] += 1
            if self.armed and self.tx_send is not None:
                self.tx_send(self.tx_modem.transmit(reply))
                ev.transmitted = True
                self.stats["transmitted"] += 1
        return self._emit(ev)

    def _emit(self, ev: TransceiveEvent) -> TransceiveEvent:
        if self.on_event is not None:
            self.on_event(ev)
        return ev


def make_channelizer_responder(responder: Optional[Responder] = None,
                               dedup: int = 4):
    """Adapter for ChannelizerWorker.responder. The worker decodes an RX channel
    to a validated frame message; this maps that message through the modem
    policy and returns the full framed reply bits (sync+len+CRC) to transmit
    back, or None to stay silent. dedup replies once per repeated frame.

    Signature matches worker.responder: (ch, message): reply_bits | None.
    """
    responder = responder or ack_responder()
    recent: deque = deque(maxlen=max(1, dedup))

    def _resp(ch, message):
        if not message or message in recent:
            return None
        recent.append(message)
        reply = responder.respond(message)
        return frame_codec.encode_text(reply) if reply else None

    return _resp
