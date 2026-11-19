"""
dpmg.bitpipe: a bit level communications container.

Application bytes go in one face, a continuous framed, FEC coded, encrypted
bit stream comes out of the other, and the reverse on receive. The engine is
pure (no sockets, no threads); the container puts it behind two local TCP
faces; loopback runs two containers back to back through a software channel.

    config      BitpipeConfig and the derived frame sizes
    frame       link frame layout, CRC, the inner record that gets sealed
    coding      one frame = one FEC block (dpmg.fec), bit interleaver
    crypto      NullCrypto, SecureContextCrypto (secure_frame), loopback keys
    deframer    sync search + flywheel over a soft bit stream
    arq         selective repeat window (TX) and reorder buffer (RX)
    engine      LinkEngine: mux, ARQ, seal/open, code/decode, frame in/out
    streams     stream ids and the file record format
    transport   endpoints (tcp 127.0.0.1 or unix socket) and the record stream
    container   the socket faces (app, bit, ctl) and their threads
    loopback    two containers back to back, no radio

Doc: docs/BITPIPE.md.
"""
from .config import BitpipeConfig, FrameSizes
from .engine import LinkEngine
from .crypto import (NullCrypto, SecureContextCrypto, loopback_crypto_pair, KeyProvider, FileKeyProvider,
                     StaticKeyProvider, DeviceKeyProvider, crypto_from_config)
from .container import BitpipeContainer
from . import streams

__all__ = ["BitpipeConfig", "FrameSizes", "LinkEngine", "NullCrypto", "SecureContextCrypto",
           "loopback_crypto_pair", "BitpipeContainer", "streams", "KeyProvider", "FileKeyProvider",
           "StaticKeyProvider", "DeviceKeyProvider", "crypto_from_config"]
