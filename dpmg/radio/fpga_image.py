"""
fpga_image.py: which FPGA image a radio runs, and loading another one.

Three families, each with its own idea of "load":
  x4xx  Ettus X410. The image lives on the radio. uhd_image_loader writes a
        new one (X4_200, CG_400, UC_200 ...) from the UHD images folder; the
        radio drops off the network while it reloads. uhd_find_devices reports
        the image name, uhd_usrp_probe the version.
  b2xx  Ettus B200 family. Nothing is stored on the radio: UHD loads an image
        at every open. Loading here means choosing the file UHD opens with
        (device arg fpga=PATH) and reopening the radio.
  epiq  Epiq G40 / X40 (NV100, Sidekiq). Read only: version, git hash and
        build date per card from libsidekiq while the radio is open. Flashing
        these cards has its own procedure and is not done from here.

The service owns the load (fpga_load) and reports progress through
fpga_status. Everything that runs a tool takes a runner so the tests and the
simulator can stand in for the radio.
"""
from __future__ import annotations

import ctypes
import glob
import os
import re
import subprocess
import time

FAMILY_BY_KIND = {
    "x410": "x4xx", "x4xx": "x4xx",
    "b200": "b2xx", "ettus": "b2xx", "uhd": "b2xx",
    "g40": "epiq", "x40": "epiq",
}

HOW = {
    "x4xx": "flash",        # written to the radio, survives power cycles
    "b2xx": "on_open",      # chosen file, loaded by UHD at each open
    "epiq": "read_only",
}

X4XX_DEFAULT_IMAGE = "X4_200"
LOAD_TIMEOUT_S = 900        # uhd_image_loader on the X410 takes a few minutes
COMEBACK_TIMEOUT_S = 240    # radio back on the network after the load
FIND_TIMEOUT_S = 15
PROBE_TIMEOUT_S = 120


def family(kind) -> str | None:
    return FAMILY_BY_KIND.get((kind or "").lower())


# X410 personality per FPGA image
#
# What UHD 4.11 reports for each image on the bench X410 (measured with
# uhd_usrp_probe and the MultiUSRP API).
# X4_200 and UC_200 read the same through UHD: master clock 245.76 MHz, 4 RX and
# 4 TX channels, rates 245.76 MHz / n (120 kS/s to 245.76 MS/s). CG_400 runs a
# 491.52 MHz clock, 4 RX and 4 TX channels, rates 491.52 MHz / n (240 kS/s to
# 491.52 MS/s). All three: 400 MHz analog bandwidth, UHD tune range 1 to 8000 MHz
# (the static caps keep the ZBX 7.2 GHz spec limit). An image that is not here
# is refused at init, so the GUI never offers rates a guess would get wrong.
X4XX_IMAGES = {
    "X4_200": {"mcr_hz": 245_760_000, "rx_channels": 4, "tx_channels": 4,
               "analog_bw_hz": 400e6, "tune_hz": (1e6, 8e9),
               "measured": "2026-10-30"},
    "UC_200": {"mcr_hz": 245_760_000, "rx_channels": 4, "tx_channels": 4,
               "analog_bw_hz": 400e6, "tune_hz": (1e6, 8e9),
               "measured": "2026-10-30"},
    "CG_400": {"mcr_hz": 491_520_000, "rx_channels": 4, "tx_channels": 4,
               "analog_bw_hz": 400e6, "tune_hz": (1e6, 8e9),
               "measured": "2026-10-30"},
}

# What the link to the radio carries, whatever the image: mothball2 reaches the
# X410 over its 1 GbE management port. benchmark_rate RX was clean at 9.83 MS/s
# and overflowed at 30.72, so one direction is capped near 10 MS/s. With RX and
# TX both streaming the same link carries both, so keep the pair at 7.68 MS/s
# or below (the GUI notes this; the cap below is the one-way limit).
X4XX_LINK_MAX_HZ = 10e6
X4XX_LINK_PAIR_MAX_HZ = 7.68e6
# the rates offered, where the image's clock divides into them exactly (the
# familiar 1.92 MS/s family; 245.76 and 491.52 MHz both make all of them)
_X4XX_RATES = (480_000, 960_000, 1_228_800, 1_920_000, 2_457_600, 3_840_000,
               4_915_200, 6_144_000, 7_680_000, 9_830_400, 12_288_000, 15_360_000,
               19_660_800, 24_576_000, 30_720_000, 61_440_000)


def x4xx_known(image) -> bool:
    return image in X4XX_IMAGES


def x4xx_rates(image, link_max_hz=X4XX_LINK_MAX_HZ) -> list:
    """Sample rates for this image: the offered rates its master clock divides
    into exactly, capped by what the link carries. Empty for an unknown image."""
    d = X4XX_IMAGES.get(image)
    if not d:
        return []
    mcr = int(d["mcr_hz"])
    return [r for r in _X4XX_RATES if mcr % r == 0 and r <= link_max_hz + 1]


def x4xx_caps(image, base=None) -> dict:
    """Caps for an X410 with this image loaded, on top of the radio's static
    caps (ports, gains, clock sources). An unknown image keeps the base but
    offers no rates and says why, so the GUI shows it and init refuses it."""
    c = dict(base or {})
    c["fpga_image"] = image
    d = X4XX_IMAGES.get(image)
    label = c.get("label") or "Ettus X410"
    label = label.split(" (")[0]
    if not d:
        c.update(sample_rates_hz=[], image_known=False, label=f"{label} ({image or 'no image'})",
                 note=f"FPGA image {image or 'unknown'} is not in DPMG's X410 table; "
                      f"load one of {', '.join(sorted(X4XX_IMAGES))}")
        return c
    rates = x4xx_rates(image)
    c.update(sample_rates_hz=rates, image_known=True, label=f"{label} ({image})",
             mcr_hz=d["mcr_hz"], rx_channels=d["rx_channels"], tx_channels=d["tx_channels"],
             analog_bw_hz=d["analog_bw_hz"], link_max_hz=X4XX_LINK_MAX_HZ,
             link_pair_max_hz=X4XX_LINK_PAIR_MAX_HZ)
    if c.get("default_rate_hz") not in rates and rates:
        c["default_rate_hz"] = min(rates, key=lambda r: abs(r - 1_920_000))
    return c


def x4xx_init_refusal(image, caps_image, rate_hz):
    """Why an init on an X410 must not go ahead, or None. caps_image is the
    image the GUI's caps were built for (None if it never asked)."""
    if not image:
        return "the X410 did not answer uhd_find_devices; is it on the network?"
    if not x4xx_known(image):
        return (f"FPGA image {image} is not in DPMG's X410 table, so its rates are "
                f"not known; load one of {', '.join(sorted(X4XX_IMAGES))} first")
    if caps_image and caps_image != image:
        return (f"the FPGA image changed from {caps_image} to {image} since the "
                f"controls were built; they have been refreshed, check them and "
                f"initialize again")
    if rate_hz and int(round(float(rate_hz))) not in x4xx_rates(image):
        return (f"{float(rate_hz)/1e6:g} MS/s is not a rate the X410 runs with {image} "
                f"over this link; pick one of the listed rates")
    return None


# tool runner

def run_tool(argv, env=None, timeout=60):
    """(returncode, combined output). A missing tool or a timeout is a
    returncode of 127 / 124 with the reason as the output."""
    e = dict(os.environ)
    e.update(env or {})
    try:
        r = subprocess.run(argv, env=e, timeout=timeout, capture_output=True, text=True)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except FileNotFoundError:
        return 127, f"{argv[0]}: not found on this host"
    except subprocess.TimeoutExpired as ex:
        out = ex.stdout or ""
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        return 124, out + f"\n{argv[0]}: timed out after {timeout} s"


# images folders and choices

def x4xx_images_dir(env=None) -> str:
    env = os.environ if env is None else env
    return (env.get("DPMG_UHD_IMAGES") or env.get("UHD_IMAGES_DIR")
            or os.path.expanduser("~/x410_uhd411"))


def uhd_images_dir(env=None) -> str:
    env = os.environ if env is None else env
    return (env.get("DPMG_UHD_IMAGES") or env.get("UHD_IMAGES_DIR")
            or "/usr/share/uhd/images")


def x4xx_choices(folder, product="x410") -> list:
    """Image names (X4_200, CG_400, ...) that have both the bitstream and the
    device tree in the folder, which is what uhd_image_loader needs."""
    out = []
    for p in glob.glob(os.path.join(folder, f"usrp_{product}_fpga_*.bin")):
        name = os.path.basename(p)[len(f"usrp_{product}_fpga_"):-len(".bin")]
        if os.path.exists(p[:-4] + ".dts") and re.fullmatch(r"[A-Za-z0-9_]+", name):
            out.append(name)
    return sorted(out)


def b2xx_choices(folder) -> list:
    """B200 family bitstreams in the folder (file names)."""
    return sorted(os.path.basename(p) for p in glob.glob(os.path.join(folder, "usrp_b2*_fpga*.bin")))


# parsing tool output

def parse_find(text) -> list:
    """Device address blocks from uhd_find_devices, as dicts."""
    devs, cur = [], None
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("Device Address"):
            cur = {}
            devs.append(cur)
            continue
        if cur is not None:
            m = re.match(r"^([A-Za-z_]+)\s*:\s*(.*)$", s)
            if m:
                cur[m.group(1)] = m.group(2).strip()
            elif s.startswith("--") or not s:
                cur = None if s.startswith("--") else cur
    return devs


def find_device(devs, addr=None, serial=None):
    for d in devs:
        if addr and addr in (d.get("addr"), d.get("mgmt_addr")):
            return d
        if serial and d.get("serial") == serial:
            return d
    return devs[0] if (devs and not addr and not serial) else None


def parse_probe(text) -> dict:
    """FPGA version and git hash from uhd_usrp_probe output, as found."""
    out = {}
    for key, pat in (("version", r"FPGA Version\s*:\s*([0-9][0-9.]*)"),
                     ("git_hash", r"FPGA git hash\s*:\s*([0-9A-Fa-fx]+)")):
        m = re.search(pat, text or "", re.IGNORECASE)
        if m:
            out[key] = m.group(1)
    m = re.search(r"FPGA (?:image|Image)\s*:\s*([A-Za-z0-9_]+)", text or "")
    if m:
        out["image"] = m.group(1)
    return out


def args_get(args, key):
    for part in (args or "").split(","):
        k, _, v = part.partition("=")
        if k.strip() == key:
            return v.strip()
    return None


def args_set(args, key, value):
    parts = [p for p in (args or "").split(",") if p and p.split("=", 1)[0].strip() != key]
    if value is not None:
        parts.append(f"{key}={value}")
    return ",".join(parts)


def x4xx_args(device_args) -> str:
    a = device_args or ""
    if not args_get(a, "type"):
        a = args_set(a, "type", "x4xx")
    return a


# commands

def find_cmd(args):
    return ["uhd_find_devices", "--args", args]


def probe_cmd(args):
    return ["uhd_usrp_probe", "--args", args]


def load_cmd(args, image):
    return ["uhd_image_loader", "--args", args_set(args, "fpga", image)]


# live readback from an open backend

def epiq_readback(backend) -> dict:
    """Per card FPGA version, git hash and build date from libsidekiq. The
    cards must be open (the service holds them)."""
    skiq = getattr(backend, "_skiq", None)
    lib = getattr(skiq, "lib", None)
    if lib is None:
        return {}
    cards = sorted({int(getattr(backend, "_card", 0)),
                    int(getattr(backend, "_tx_card", getattr(backend, "_card", 0)))})
    out = {}
    lock = getattr(backend, "_hw_lock", None)
    for c in cards:
        d = {}
        try:
            if lock:
                lock.acquire()
            ma, mi, pa = ctypes.c_uint8(0), ctypes.c_uint8(0), ctypes.c_uint8(0)
            fn = getattr(lib, "skiq_read_fpga_semantic_version", None)
            if fn is not None and fn(ctypes.c_uint8(c), ctypes.byref(ma), ctypes.byref(mi),
                                     ctypes.byref(pa)) == 0:
                d["version"] = f"{ma.value}.{mi.value}.{pa.value}"
            h = ctypes.c_uint32(0)
            fn = getattr(lib, "skiq_read_fpga_git_hash", None)
            if fn is not None and fn(ctypes.c_uint8(c), ctypes.byref(h)) == 0:
                d["git_hash"] = f"{h.value:08x}"
            bd = ctypes.c_uint32(0)
            fn = getattr(lib, "skiq_read_fpga_build_date", None)
            if fn is not None and fn(ctypes.c_uint8(c), ctypes.byref(bd)) == 0:
                d["build_date"] = f"{bd.value:08x}"      # yymmddhh
        except Exception as e:
            d["err"] = str(e)
        finally:
            if lock:
                lock.release()
        out[str(c)] = d
    try:
        out["lib"] = skiq.lib_version()
    except Exception:
        pass
    return out


def uhd_readback(backend) -> dict:
    """FPGA version from the UHD property tree of an open radio, as far as
    this UHD exposes it."""
    usrp = getattr(backend, "usrp", None)
    if usrp is None:
        return {}
    out = {}
    try:
        tree = usrp.get_tree()
    except Exception:
        return out
    for key, path in (("version", "/mboards/0/fpga_version"),
                      ("git_hash", "/mboards/0/fpga_version_hash")):
        for acc in ("access_str", "access_string"):
            fn = getattr(tree, acc, None)
            if fn is None:
                continue
            try:
                out[key] = str(fn(path).get())
                break
            except Exception:
                pass
    return out


# simulated X410 (service --simulate, tests)

class SimX4xx:
    """Stands in for uhd_find_devices / uhd_usrp_probe / uhd_image_loader on a
    simulated X410. The radio is gone for `down_s` after a load."""
    VERSIONS = {"X4_200": "11.0", "CG_400": "11.0", "UC_200": "11.0"}

    def __init__(self, image=X4XX_DEFAULT_IMAGE, addr="192.168.8.25", serial="SIM4XX",
                 down_s=0.5, load_s=0.2, fail_load=False, land_on=None, probe_rc=0):
        self.image, self.addr, self.serial = image, addr, serial
        self.down_s, self.load_s = down_s, load_s
        self.fail_load, self.land_on, self.probe_rc = fail_load, land_on, probe_rc
        self._down_until = 0.0
        self.calls = []

    def __call__(self, argv, env=None, timeout=60):
        self.calls.append(list(argv))
        tool, args = argv[0], (argv[2] if len(argv) > 2 else "")
        up = time.monotonic() >= self._down_until
        if tool == "uhd_find_devices":
            if not up:
                return 1, "No UHD Devices Found"
            return 0, ("--------------------------------------------------\n"
                       "-- UHD Device 0\n"
                       "--------------------------------------------------\n"
                       "Device Address:\n"
                       f"    serial: {self.serial}\n    addr: {self.addr}\n"
                       f"    claimed: False\n    fpga: {self.image}\n"
                       f"    mgmt_addr: {self.addr}\n    product: x410\n    type: x4xx\n")
        if tool == "uhd_usrp_probe":
            if not up:
                return 1, "RuntimeError: No devices found"
            return self.probe_rc, (f"|   |   FPGA Version: {self.VERSIONS.get(self.image, '0.0')}\n"
                                   "|   |   FPGA git hash: 881f19d\n")
        if tool == "uhd_image_loader":
            img = args_get(args, "fpga")
            time.sleep(self.load_s)
            if self.fail_load:
                return 1, "Error: image load failed"
            self.image = self.land_on or img
            self._down_until = time.monotonic() + self.down_s
            return 0, f"Loading {img}... done"
        return 127, f"{tool}: not simulated"
