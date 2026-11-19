#!/usr/bin/env python3
"""
test_role_fpga_ui.py: the GUI helpers behind the radio role picker and the
FPGA image panel (role_fpga_ui.py). The widgets themselves are checked in the
browser; this locks what the cards and the confirm dialog say.

  1. role_text: blank for an old service, the label and colour otherwise
  2. fpga_text: image and version, a load in progress, nothing for no control
  3. fpga_rows: one row per radio that reports an FPGA brief
  4. confirm_lines (printed after 5): names the radio, the current and the new image, warns that
     an X410 drops off while loading, and that TX refuses the load
  5. caps_stale: caps built for another X410 image than the radio reports, or
     fetched before the service last restarted

    python3 tests/test_role_fpga_ui.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dpmg.web import role_fpga_ui as rfu                              # noqa: E402

FAILS = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def main():
    print("[1] role_text")
    check(rfu.role_text(None)[0] == "" and rfu.role_text({"v": 1})[0] == "",
          "no role shown for a service that does not report one")
    t, c = rfu.role_text({"role": "jammer"})
    check(t == "Role: Jammer" and "text-red-700" in c, f"jammer ({t}, {c})")
    check(rfu.role_text({"role": "monitor"})[0] == "Role: Receiver / monitor", "monitor")
    check(rfu.role_text({"role": None})[0] == "Role: Any (engineering)", "unset reads as any")

    print("[2] fpga_text")
    check(rfu.fpga_text({})[0] == "" and rfu.fpga_text({"fpga": None})[0] == "", "no FPGA control")
    check(rfu.fpga_text({"fpga": {"fam": "x4xx", "img": "X4_200", "ver": "11.0"}})[0]
          == "FPGA X4_200 v11.0", "x410 image and version")
    t, c = rfu.fpga_text({"fpga": {"fam": "x4xx", "img": "X4_200", "job": "running",
                                    "step": "waiting for the radio to come back"}})
    check(t.startswith("FPGA loading") and "amber" in c, f"load in progress ({t})")
    check(rfu.fpga_text({"fpga": {"fam": "epiq"}})[0] == "", "epiq closed: nothing yet")

    print("[3] fpga_rows")

    class H:
        def __init__(self, cfg):
            self.config = cfg
    hosts = {"X410-1": H({"fpga": {"fam": "x4xx", "img": "CG_400", "ver": "11.0", "job": "done"}}),
             "HACKRF": H({"fpga": None}),
             "B200-1": H({"fpga": {"fam": "b2xx", "img": "UHD default"}})}
    rows = rfu.fpga_rows(hosts, lambda h: h.config, lambda s: s.lower())
    check([r["serial"] for r in rows] == ["B200-1", "X410-1"], "radios with a brief, sorted")
    check(rows[1]["image"] == "CG_400" and rows[1]["version"] == "11.0"
          and rows[1]["radio"] == "x410-1" and rows[1]["load"] == "done", "x410 row")

    print("[5] caps_stale")
    check(not rfu.caps_stale({"family": "uhd"}, {"fpga": {"img": "X4_200"}}),
          "caps with no image (B200, older service) never go stale")
    check(not rfu.caps_stale({"fpga_image": "X4_200"}, {"fpga": {"img": "X4_200"}}), "same image")
    check(rfu.caps_stale({"fpga_image": "X4_200"}, {"fpga": {"img": "UC_200"}}),
          "caps built for X4_200 are stale once the radio reports UC_200")
    check(not rfu.caps_stale({"fpga_image": "X4_200"}, {"fpga": {"fam": "x4xx"}}),
          "no image reported yet: keep the caps")
    check(rfu.caps_stale({"family": "uhd", "_uptime_s": 5000}, {}, 30),
          "service restarted since the caps came (uptime went down): stale")
    check(not rfu.caps_stale({"family": "uhd", "_uptime_s": 5000}, {}, 5040),
          "same service run: keep the caps")
    check(not rfu.caps_stale({"family": "uhd"}, {}, 30) and
          not rfu.caps_stale({"family": "uhd", "_uptime_s": 5000}, {}, None),
          "no stored or no current uptime: keep the caps")

    print("[4] confirm_lines")
    st = {"family": "x4xx", "image": "X4_200", "version": "11.0"}
    lines = rfu.confirm_lines("Bench X410", "X410-1234ABC", st, "CG_400")
    text = "\n".join(lines)
    check("Bench X410" in lines[0] and "X410-1234ABC" in lines[0], "names the radio and serial")
    check(lines[1] == "Current image: X4_200 v11.0", "current image and version")
    check(lines[2] == "New image: CG_400", "new image")
    check("drops off the network" in text, "warns the radio drops off while loading")
    check("TX" in lines[-1], "says TX refuses the load")
    b = "\n".join(rfu.confirm_lines("B200", "B200", {"family": "b2xx", "image": "UHD default"},
                                    "usrp_b205mini_fpga.bin"))
    check("reopens" in b and "drops off" not in b, "b200 warning is about the reopen")


if __name__ == "__main__":
    main()
    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}")
        sys.exit(1)
    print("all passed")
