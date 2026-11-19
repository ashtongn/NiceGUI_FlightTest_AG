"""
Channelizer-side transceive: ChannelizerWorker decodes an RX channel to a
validated frame message; the modem responder computes the reply and the worker
fires tx_callback with the framed reply bits. Verifies the preset PING/PONG
stream flows through _apply_decode_result, dedup, the static-reply fallback,
and that invalid decodes never transmit. No GPU/hardware. Exit 0 = all pass.
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dpmg.framing import frame_codec
from dpmg.dsp.channelizer_worker import ChannelizerWorker, ChannelRuntime
from dpmg.dsp.decode_pool import DecodeResult
from dpmg.modem.transceive import make_channelizer_responder

checks = []


def chk(name, ok, detail=""):
    checks.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


def dr(message="", valid=True, bits="1"):
    return DecodeResult(key=0, ok=True, valid=valid, message=message,
                        bit_string=bits, n_syms=len(bits), snr_db=12.0,
                        acquired=True)


def new_worker(with_responder=True):
    w = ChannelizerWorker()
    sent = []
    w.tx_callback = lambda idx, bits: sent.append((idx, bits))
    if with_responder:
        w.responder = make_channelizer_responder()
    ch = ChannelRuntime(idx=0, f_center_hz=425.0e6, bw_hz=25e3, demod_type="FSK")
    return w, ch, sent


def main():
    print("Channelizer transceive -- decode -> respond\n")

    # 1) valid PING 1: tx_callback gets framed PONG 1
    w, ch, sent = new_worker()
    w._apply_decode_result(ch, dr("PING 1"))
    ok = len(sent) == 1 and sent[0][1] == frame_codec.encode_text("PONG 1")
    chk("valid 'PING 1' -> framed 'PONG 1' TX'd", ok, f"(sent {len(sent)})")

    # 2) full preset stream
    w, ch, sent = new_worker()
    for m in ["PING 2", "TEXT HELLO", "BYE"]:
        w._apply_decode_result(ch, dr(m))
    want = [frame_codec.encode_text(x) for x in ("PONG 2", "ACK HELLO", "BYE")]
    chk("preset stream replies in order", [b for _, b in sent] == want,
        f"({len(sent)}/3)")

    # 3) dedup: same frame decoded twice: one reply
    w, ch, sent = new_worker()
    w._apply_decode_result(ch, dr("PING 9"))
    w._apply_decode_result(ch, dr("PING 9"))
    chk("dedup: repeated decode replies once", len(sent) == 1, f"(sent {len(sent)})")

    # 4) invalid decode: no TX
    w, ch, sent = new_worker()
    w._apply_decode_result(ch, dr("PING 1", valid=False))
    chk("invalid decode never transmits", len(sent) == 0)

    # 5) no responder: static canned reply still works
    w, ch, sent = new_worker(with_responder=False)
    ch.response_bits = "10101100"
    w._apply_decode_result(ch, dr("anything"))
    chk("static response_bits fallback still fires",
        len(sent) == 1 and sent[0][1] == "10101100")

    print(f"\nresult: {sum(checks)}/{len(checks)} checks passed")
    return 0 if checks and all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
