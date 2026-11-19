"""
UhdBackend (B200mini) against a fake uhd module. Covers the
device-args defaults (type by kind + num_recv_frames host buffering for the
B200 RX overflows on the Jetson), their overrides, and
recv()'s overflow handling (count + read again, never an empty frame that the
drain would mistake for an idle card). Also the rate-sized USB
buffer and recv size, the automatic LO offset (kept across a retune) and the
reader thread that keeps the B200 drained while the drain loop stalls.
No hardware needed.
Run: python3 tests/test_uhd_backend.py
"""
import os
import sys
import time
import types

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class _Enum:
    overflow = "overflow"
    none = "none"
    start_cont = "start_cont"
    stop_cont = "stop_cont"


class FakeMD:
    def __init__(self):
        self.error_code = _Enum.none


class FakeRxStream:
    def __init__(self):
        self.script = []          # queued (n, error_code)
        self.cmds = []

    def issue_stream_cmd(self, cmd):
        self.cmds.append(cmd)

    pace_s = 0.0

    k = 0                         # running sample number, mod 2**15 (fits an int16)

    def recv(self, buf, md, timeout):
        n, md.error_code = self.script.pop(0) if self.script else (buf.shape[1], _Enum.none)
        idx = np.arange(self.k, self.k + n) % (1 << 15)
        if buf.dtype == np.uint32:                # cpu format sc16: int16 I then Q
            v = buf.view(np.int16).reshape(buf.shape[0], -1, 2)
            v[0, :n, 0] = idx
            v[0, :n, 1] = 1
        else:
            buf[0, :n] = idx + 1.0j
        self.k += n
        if self.pace_s:
            time.sleep(self.pace_s)
        return n


class FakeUSRP:
    last = None
    start_mcr = 61.44e6

    def __init__(self, args):
        FakeUSRP.last = self
        self.args = args
        self.rx_stream = FakeRxStream()
        self.calls = []
        self.mcr = FakeUSRP.start_mcr
        self.rx_tunes, self.tx_tunes = [], []

    def get_master_clock_rate(self):
        return self.mcr

    def set_master_clock_rate(self, r):
        self.calls.append(("set_master_clock_rate", (r,)))
        self.mcr = r

    def set_rx_freq(self, tr, ch):
        self.rx_tunes.append(tr)
        return types.SimpleNamespace(actual_rf_freq=tr[0] + tr[1], actual_dsp_freq=-tr[1])

    def set_tx_freq(self, tr, ch):
        self.tx_tunes.append(tr)

    def get_rx_stream(self, st):
        self.rx_cpu = st.cpu
        return self.rx_stream

    def get_tx_stream(self, st):
        return object()

    def get_rx_rate(self, ch):
        return 7.68e6

    def get_rx_antennas(self, ch):
        return ["RX2", "TX/RX"]

    def get_tx_antennas(self, ch):
        return ["TX/RX"]

    def get_clock_sources(self, ch):
        return ["internal", "external"]

    def get_time_sources(self, ch):
        return ["internal", "external"]

    def __getattr__(self, name):            # every other setter is a no-op
        if name.startswith("set_"):
            return lambda *a, **k: self.calls.append((name, a))
        raise AttributeError(name)


def install_fake_uhd():
    uhd = types.ModuleType("uhd")
    usrp = types.ModuleType("uhd.usrp")
    typ = types.ModuleType("uhd.types")
    usrp.MultiUSRP = FakeUSRP
    usrp.StreamArgs = lambda a, b: types.SimpleNamespace(channels=[], cpu=a)
    typ.TuneRequest = lambda f, off=0.0: (f, off)
    typ.RXMetadata = FakeMD
    typ.TXMetadata = lambda: types.SimpleNamespace()
    typ.StreamCMD = lambda mode: types.SimpleNamespace(mode=mode, stream_now=False)
    typ.StreamMode = _Enum
    typ.RXMetadataErrorCode = _Enum
    uhd.usrp, uhd.types = usrp, typ
    sys.modules["uhd"] = uhd
    sys.modules["uhd.usrp"] = usrp
    sys.modules["uhd.types"] = typ


def main():
    install_fake_uhd()
    from dpmg.radio import sdr_core as sdr
    checks = []

    def check(name, ok, detail=""):
        checks.append(bool(ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")

    U = sdr.UhdBackend

    def make(pump=False, rate=7.68e6, **kw):
        # the older checks drive recv() directly; the reader thread is off for them
        ex = dict(kw.pop("extras", {}) or {})
        ex.setdefault("rx_pump", pump)
        ex.setdefault("rx_pin_cpus", False)    # never pin the test process's threads
        cfg = sdr.SDRConfig(sample_rate=rate, center_freq=915e6, rx_gain=40.0,
                            tx_gain=0.0, rx_antenna="RX2", tx_antenna="TX/RX",
                            extras=ex, **kw)
        b = sdr.make_backend("b200", cfg, simulate=False)
        b.open()
        return b

    nrf = U.recv_frames_for(7.68e6, U._usbfs_mb())
    b = make()
    args = FakeUSRP.last.args
    check("default device args pin the transport and size RX buffering to the rate",
          args == f"type=b200,num_recv_frames={nrf}", repr(args))
    check("default num_recv_frames well above UHD's 16-frame default",
          sdr.UhdBackend.NUM_RECV_FRAMES >= 128, str(sdr.UhdBackend.NUM_RECV_FRAMES))

    make(extras={"num_recv_frames": 64})
    check("extras num_recv_frames overrides the default",
          FakeUSRP.last.args == "type=b200,num_recv_frames=64", repr(FakeUSRP.last.args))

    make(extras={"num_recv_frames": 0})
    check("extras num_recv_frames=0 leaves UHD's own default",
          FakeUSRP.last.args == "type=b200", repr(FakeUSRP.last.args))

    make(device_args="serial=3000001,num_recv_frames=32")
    check("explicit device_args naming num_recv_frames are passed through untouched",
          FakeUSRP.last.args == "serial=3000001,num_recv_frames=32", repr(FakeUSRP.last.args))

    make(device_args="serial=3000001")
    check("explicit device_args without it still get the deeper buffering",
          FakeUSRP.last.args == f"serial=3000001,num_recv_frames={nrf}",
          repr(FakeUSRP.last.args))

    # buffer and recv sizing
    check("30.72 MSps gets 100 ms of USB frames (1500)",
          U.recv_frames_for(30.72e6) == 1500, str(U.recv_frames_for(30.72e6)))
    check("usbfs limit caps the frames (8 MB gives 768)",
          U.recv_frames_for(30.72e6, 8) == 768, str(U.recv_frames_for(30.72e6, 8)))
    check("low rate keeps the 256-frame floor",
          U.recv_frames_for(1.92e6, 16) == 256, str(U.recv_frames_for(1.92e6, 16)))
    sizes = [U.recv_samps_for(r, 4096) for r in (1.92e6, 7.68e6, 30.72e6, 61.44e6, 100e6)]
    check("recv size ~1 ms, power of two, 4096..65536",
          sizes == [4096, 8192, 32768, 65536, 65536], str(sizes))
    check("open() reads with the rate-sized buffer",
          b._rx_buf.shape == (1, 8192), str(b._rx_buf.shape))

    # LO offset
    check("auto LO offset at 7.68 MSps is half the rate plus 10%",
          U.auto_lo_offset(7.68e6, 61.44e6) == 4.608e6, str(U.auto_lo_offset(7.68e6, 61.44e6)))
    check("no room at 30.72 MSps even with a 61.44 MHz master clock",
          U.auto_lo_offset(30.72e6, 61.44e6) == 0.0)
    check("15.36 MSps needs the 61.44 MHz clock",
          U.auto_lo_offset(15.36e6, 30.72e6) == 0.0
          and U.auto_lo_offset(15.36e6, 61.44e6) == 9.216e6)
    b = make()
    u = FakeUSRP.last
    check("GUI default 0 means auto offset on RX and TX",
          u.rx_tunes[-1] == (915e6, 4.608e6) and u.tx_tunes[-1] == (915e6, 4.608e6),
          f"rx={u.rx_tunes} tx={u.tx_tunes}")
    b.set_frequency_live(920e6)
    check("retune keeps the LO offset on RX and TX",
          u.rx_tunes[-1] == (920e6, 4.608e6) and u.tx_tunes[-1] == (920e6, 4.608e6),
          f"rx={u.rx_tunes[-1]} tx={u.tx_tunes[-1]}")
    make(extras={"lo_offset_hz": 2e6})
    check("an explicit offset is used as given",
          FakeUSRP.last.rx_tunes[-1] == (915e6, 2e6), str(FakeUSRP.last.rx_tunes))
    make(extras={"lo_offset_auto": False})
    check("lo_offset_auto False keeps the LO at center",
          FakeUSRP.last.rx_tunes[-1] == (915e6, 0.0), str(FakeUSRP.last.rx_tunes))
    make(rate=30.72e6)
    check("30.72 MSps: no offset, master clock left alone",
          FakeUSRP.last.rx_tunes[-1] == (915e6, 0.0)
          and not any(c[0] == "set_master_clock_rate" for c in FakeUSRP.last.calls),
          str(FakeUSRP.last.rx_tunes))
    FakeUSRP.start_mcr = 30.72e6
    make(rate=15.36e6)
    u = FakeUSRP.last
    check("15.36 MSps on a 30.72 MHz clock raises it to 61.44 and applies the offset",
          ("set_master_clock_rate", (61.44e6,)) in u.calls
          and u.rx_tunes[-1] == (915e6, 9.216e6), f"{u.calls[-6:]} {u.rx_tunes}")
    make(rate=15.36e6, device_args="type=b200,master_clock_rate=30.72e6")
    check("a master_clock_rate in device_args is not overridden",
          not any(c[0] == "set_master_clock_rate" for c in FakeUSRP.last.calls)
          and FakeUSRP.last.rx_tunes[-1] == (915e6, 0.0))
    FakeUSRP.start_mcr = 61.44e6

    # best LO offset (the GUI's Best + / Best - button) and room for any offset
    best = {r: U.best_lo_offset(r) for r in (7.68e6, 15.36e6, 20e6, 25e6, 30.72e6, 40e6, 61.44e6)}
    check("up to 20 MSps the best offset puts the leak outside the band",
          all(best[r][2] == "outside" and best[r][0] == r / 2 + 0.1 * r
              for r in (7.68e6, 15.36e6, 20e6)), str(best))
    check("20 MSps uses a 60 MHz clock, a whole multiple, so the rate stays exact",
          best[20e6][1] == 60e6, str(best[20e6]))
    check("25 and 30.72 MSps: guard strip at 0.4 x rate + 0.5 MHz, clock 2 x rate",
          best[25e6] == (10.5e6, 50e6, "guard")
          and best[30.72e6][2] == "guard" and abs(best[30.72e6][0] - 12.788e6) < 1
          and best[30.72e6][1] == 61.44e6, f"{best[25e6]} {best[30.72e6]}")
    check("40 MSps and up: no room",
          best[40e6] == (0.0, 0.0, "none") and best[61.44e6] == (0.0, 0.0, "none"))
    check("Best - mirrors the offset", U.best_lo_offset(25e6, -1) == (-10.5e6, 50e6, "guard"))
    check("a typed offset without room at the current clock is detected",
          not U.offset_room(30.72e6, 12.788e6, 30.72e6)
          and U.mcr_with_room(30.72e6, 12.788e6) == 61.44e6
          and U.mcr_with_room(30.72e6, 15e6) == 0.0)
    FakeUSRP.start_mcr = 30.72e6
    make(rate=30.72e6, extras={"lo_offset_hz": 12.788e6})
    u = FakeUSRP.last
    check("typed guard offset at 30.72 raises the clock to 61.44 and tunes with it",
          ("set_master_clock_rate", (61.44e6,)) in u.calls
          and u.rx_tunes[-1] == (915e6, 12.788e6), f"{u.calls[-4:]} {u.rx_tunes}")
    make(rate=30.72e6, extras={"lo_offset_hz": 15e6})
    check("typed offset with no room anywhere: LO left at center, band never folded",
          FakeUSRP.last.rx_tunes[-1] == (915e6, 0.0), str(FakeUSRP.last.rx_tunes))
    make(rate=20e6)
    check("auto at 20 MSps on a 30.72 MHz clock moves to 60 MHz (whole multiple)",
          ("set_master_clock_rate", (60e6,)) in FakeUSRP.last.calls
          and FakeUSRP.last.rx_tunes[-1] == (915e6, 12e6),
          f"{FakeUSRP.last.calls[-4:]} {FakeUSRP.last.rx_tunes}")
    FakeUSRP.start_mcr = 61.44e6

    # TX attenuation (set_tx_atten): gain = top of the range minus the attenuation
    b = make()
    u = FakeUSRP.last
    b.set_tx_attenuation_qdb(80)                         # 20 dB
    gains = [c[1][0] for c in u.calls if c[0] == "set_tx_gain"]
    check("TX atten 20 dB sets TX gain 69.75 (top 89.75 when UHD cannot say)",
          gains and gains[-1] == 69.75 and b.config.tx_gain == 69.75, str(gains[-3:]))
    u.get_tx_gain_range = lambda ch: types.SimpleNamespace(stop=lambda: 80.0)
    b.set_tx_attenuation_qdb(0)
    gains = [c[1][0] for c in u.calls if c[0] == "set_tx_gain"]
    check("TX atten 0 dB = the top of the radio's own range", gains[-1] == 80.0,
          str(gains[-1]))
    b.set_tx_attenuation_qdb(4 * 200)
    gains = [c[1][0] for c in u.calls if c[0] == "set_tx_gain"]
    check("more attenuation than the range floors at gain 0", gains[-1] == 0.0)

    # reader thread (pump)
    FakeRxStream.pace_s = 0.001
    b = make(pump=True)
    st = FakeUSRP.last.rx_stream
    time.sleep(0.05)
    f1, f2 = b.recv(), b.recv()
    check("pump delivers frames with contiguous timestamps",
          f1.samples.size == 8192 and f2.rf_timestamp == f1.rf_timestamp + f1.samples.size,
          f"n={f1.samples.size} ts={f1.rf_timestamp},{f2.rf_timestamp}")
    o0 = b.health.overruns
    st.script = [(0, _Enum.overflow)]
    t_end = time.time() + 2.0                  # poll: a busy host may be slow
    while b.health.overruns == o0 and time.time() < t_end:
        time.sleep(0.01)
    check("pump counts a radio overflow", b.health.overruns == o0 + 1,
          f"{o0} to {b.health.overruns}")
    b.close()
    check("close() stops the pump thread", b._pump is None)

    b = make(pump=True, extras={"recv_samps": 65536})
    t_end = time.time() + 5.0  # nobody reading: let the pump lap the ring a few times
    while b._wpos <= b._ring.size * 2 and time.time() < t_end:
        time.sleep(0.05)
    check("a lapped ring while nobody reads is not an overrun",
          b._wpos > b._ring.size * 2 and b._host_drops == 0 and b.health.overruns == 0,
          f"written={b._wpos} ring={b._ring.size} drops={b._host_drops} "
          f"overruns={b.health.overruns}")
    fr = b.recv()
    check("first read after an idle spell starts from fresh samples",
          fr.samples.size == 65536 and b._wpos - fr.rf_timestamp <= 4 * 65536
          and b.health.overruns == 0, f"behind={b._wpos - fr.rf_timestamp}")
    b.close()

    # recv_batch: the scheduler's one-call-per-batch path
    check("no pump, no batch path", not make().native_drain_ok())
    check("without the pump RX streams fc32", FakeUSRP.last.rx_cpu == "fc32")
    b = make(pump=True)
    check("pumped backend offers the batch path as engine 'pump'",
          b.native_drain_ok() and b.drain_engine == "pump")
    check("with the pump RX streams raw sc16", FakeUSRP.last.rx_cpu == "sc16")
    chunk, st = b.recv_batch(2048, 0.03)
    r = np.rint(chunk.real.astype(np.float64) * 32767)
    check("batch is contiguous samples in order, ts0 and last block consistent",
          chunk.dtype == np.complex64 and st.n_blocks >= 2 and st.n_mismatch == 0
          and not st.empty and np.all(np.diff(r) % (1 << 15) == 1)
          and r[0] == st.ts0 % (1 << 15) and np.allclose(chunk.imag, 1 / 32767)
          and st.last_ts + st.last_n == st.ts0 + chunk.size,
          f"blocks={st.n_blocks} n={chunk.size} mismatch={st.n_mismatch}")
    raw = np.array([32767, -32767, 0, 16384], dtype=np.int16).view(np.uint32)
    c = U._sc16_to_c64(raw)
    check("sc16 to complex64 matches UHD's scale (32767 is full scale)",
          np.allclose(c, [1 - 1j, 16384 / 32767 * 1j]), str(c))
    time.sleep(0.4)            # reader busy elsewhere: ring fills, pump drops blocks
    chunk, st = b.recv_batch(2048, 0.03)
    check("a dropped stretch shows up as a reported ts jump",
          b._host_drops > 0 and st.n_mismatch >= 1 and st.first_got > st.first_exp,
          f"drops={b._host_drops} mismatch={st.n_mismatch} exp={st.first_exp} got={st.first_got}")
    b.close()
    FakeRxStream.pace_s = 0.0

    # recv(): an overflow with 0 samples is counted and the stream read again
    b = make()
    st = FakeUSRP.last.rx_stream
    st.script = [(0, _Enum.overflow), (4096, _Enum.none)]
    fr = b.recv()
    check("overflow with 0 samples is counted and read again, frame not empty",
          b.health.overruns == 1 and fr.samples.size == 4096 and fr.rf_timestamp == 0,
          f"overruns={b.health.overruns} n={fr.samples.size}")
    st.script = [(1000, _Enum.overflow)]
    fr2 = b.recv()
    check("overflow with samples is counted, samples delivered, ts contiguous",
          b.health.overruns == 2 and fr2.samples.size == 1000 and fr2.rf_timestamp == 4096,
          f"overruns={b.health.overruns} n={fr2.samples.size} ts={fr2.rf_timestamp}")
    st.script = [(0, _Enum.overflow)] * 4
    fr3 = b.recv()
    check("four empty overflows in a row give up with an empty frame (no hang)",
          fr3.samples.size == 0 and b.health.overruns == 6, f"overruns={b.health.overruns}")

    # thread placement on the Orin Nano (6 cores) and smaller hosts
    check("6 cores: UHD 4, pump 5, drain 3, the rest 0-2",
          U.plan_cores(range(6)) == {"io": 4, "pump": 5, "drain": 3, "rest": [0, 1, 2]},
          str(U.plan_cores(range(6))))
    check("fewer than 4 cores: nothing pinned", U.plan_cores([0, 1, 2]) is None)
    check("container cpuset lists at least one core", len(U._cpuset()) >= 1)
    b2 = make(pump=True)
    check("rx_pin_cpus False leaves threads alone", b2._cores is None)
    b2.close()

    # usable band: the detector grid stays off the noisy edges above 30.72 MSps
    for rate, want in ((30.72e6, 30.72e6), (15.36e6, 15.36e6), (61.44e6, 61.44e6 * 0.66)):
        b._achieved_rate = rate
        check(f"usable band at {rate / 1e6:g} MSps is {want / 1e6:.2f} MHz",
              abs(b.usable_bw_hz() - want) < 1, str(b.usable_bw_hz()))

    from dpmg.radio import dpmg_service as svc_mod

    def det_for(backend):
        svc = svc_mod.RadioService.__new__(svc_mod.RadioService)
        svc.backend, svc._det_params = backend, {}
        svc._make_detector()
        return svc.det

    cfg61 = types.SimpleNamespace(sample_rate=61.44e6, center_freq=915e6)
    d = det_for(types.SimpleNamespace(config=cfg61, usable_bw_hz=lambda: 61.44e6 * 0.66))
    span = (d.slot_centers.min() - 915e6, d.slot_centers.max() - 915e6)
    check("service builds the detector grid inside the usable band",
          d.n_slots == 2027 and -20.3e6 < span[0] and span[1] < 20.3e6,
          f"slots={d.n_slots} span={span[0] / 1e6:.2f}..{span[1] / 1e6:.2f} MHz")
    d = det_for(types.SimpleNamespace(config=cfg61))
    check("a backend without usable_bw_hz keeps the full grid (G40 unchanged)",
          d.n_slots == 3072, f"slots={d.n_slots}")

    # Separate TX frequency: the AD9364 has its own TX synthesizer
    b = make(extras={"tx_center_freq": 917e6})
    u = FakeUSRP.last
    check("open honors tx_center_freq (TX on its own frequency)",
          u.rx_tunes[-1][0] == 915e6 and u.tx_tunes[-1][0] == 917e6 and b._tx_lo == 917e6,
          f"rx={u.rx_tunes[-1]} tx={u.tx_tunes[-1]}")
    b.set_frequency_live(920e6)
    check("retune keeps the RX/TX gap (+2 MHz) and the LO offset",
          u.rx_tunes[-1][0] == 920e6 and u.tx_tunes[-1] == (922e6, u.rx_tunes[-1][1])
          and b._tx_lo == 922e6, f"rx={u.rx_tunes[-1]} tx={u.tx_tunes[-1]}")
    n_rx = len(u.rx_tunes)
    ok = b.set_tx_frequency_live(930e6)
    check("set_tx_frequency_live moves only TX",
          ok and u.tx_tunes[-1][0] == 930e6 and len(u.rx_tunes) == n_rx
          and b._tx_lo == 930e6, f"tx={u.tx_tunes[-1]} rx tunes {n_rx}->{len(u.rx_tunes)}")
    b.set_frequency_live(925e6)
    check("next retune keeps the new gap (+10 MHz)",
          u.tx_tunes[-1][0] == 935e6, f"tx={u.tx_tunes[-1]}")
    b = make()
    check("no tx_center_freq: TX co-tuned with RX",
          FakeUSRP.last.tx_tunes[-1][0] == 915e6 and b._tx_lo == 915e6)

    n_ok = sum(checks)
    print(f"\n{n_ok}/{len(checks)} passed")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
