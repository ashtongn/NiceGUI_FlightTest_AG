#!/usr/bin/env python3
"""
test_runtime_paths.py: files the code finds by location stay at the repo root.

When a module moves into dpmg/, its lookups by location must still land on
the root copies, or the radio silently loses its
front-end config (rf_frontend.yaml), the payload guard its profiles, the GUI
its saved radio list and datasheets, and the RX path its native drain.

  (a) dpmg.paths.REPO_ROOT is the repo root
  (b) rf_frontend.yaml and payload_profiles.yaml default to the root
  (c) output/saved_radios.json (main GUI, discovery port 47811) is under the root
  (d) docs/datasheets and scenarios/json are under the root
  (e) deploy_config's Windows output folder is under the root
  (f) requirements-crypto.txt is read from the root
  (g) the GUI icon is the root mothball_fury.svg
  (h) the librxdrain.so search list: env, native/lib, root, beside the module

Runs under pytest or as a script (exit 0 = all pass).
"""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

os.environ.setdefault("DPMG_DISCOVERY_PORT", "47939")
os.environ.setdefault("DPMG_SAVED_RADIOS", "")


def _same(a, b):
    return os.path.realpath(str(a)) == os.path.realpath(str(b))


class _NoEnv:
    """Temporarily remove environment variables."""

    def __init__(self, *names):
        self.names = names
        self.saved = {}

    def __enter__(self):
        for n in self.names:
            if n in os.environ:
                self.saved[n] = os.environ.pop(n)
        return self

    def __exit__(self, *exc):
        os.environ.update(self.saved)


def test_repo_root():
    from dpmg import paths
    assert _same(paths.REPO_ROOT, ROOT), paths.REPO_ROOT
    assert os.path.isfile(os.path.join(str(paths.REPO_ROOT), "dpmg_service.py"))


def test_rf_frontend_yaml_default():
    from dpmg.payload import rf_frontend as rf
    with _NoEnv(rf.CONFIG_ENV):
        p = rf.default_config_path()
    assert _same(p, os.path.join(ROOT, "rf_frontend.yaml")), p


def test_payload_profiles_default():
    from dpmg.payload import payload_budget as pb
    with _NoEnv(pb.PROFILES_ENV):
        p = pb.default_profiles_path()
    assert _same(p, os.path.join(ROOT, "payload_profiles.yaml")), p
    assert os.path.isfile(p)


def test_saved_radios_under_root_output():
    from dpmg.web import link_tab as lt
    p = lt.saved_radios_path({}, None, 47811)
    assert _same(p, os.path.join(ROOT, "output", "saved_radios.json")), p


def test_datasheets_dir():
    from dpmg.web import payload_tab as ptab
    assert _same(ptab._DS_DIR, os.path.join(ROOT, "docs", "datasheets")), ptab._DS_DIR


def test_scenarios_json_dir():
    from dpmg.web import dsss_builder_tab as dbt
    assert _same(dbt._REPO_SCN_JSON, os.path.join(ROOT, "scenarios", "json")), dbt._REPO_SCN_JSON


def test_deploy_config_repo_dir():
    from dpmg import deploy_config
    assert _same(deploy_config._REPO_DIR, ROOT), deploy_config._REPO_DIR


def test_crypto_requirements_from_root():
    from dpmg.secure import crypto_env as ce
    assert _same(ce.REPO_ROOT, ROOT), ce.REPO_ROOT
    saved = ce.REPO_ROOT
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, ce.REQUIREMENTS_FILE), "w") as f:
            f.write("cryptography>=97.3.1,<99\n")
        try:
            ce.REPO_ROOT = d
            assert ce._read_pin() == (97, 3, 1), ce._read_pin()
        finally:
            ce.REPO_ROOT = saved
    assert os.path.isfile(os.path.join(ROOT, ce.REQUIREMENTS_FILE))


def test_gui_icon():
    import dpmg.web.nicegui_app as app
    assert app._MOTHBALL_ICON and _same(app._MOTHBALL_ICON, os.path.join(ROOT, "mothball_fury.svg")), \
        app._MOTHBALL_ICON


def test_rxdrain_search_order():
    from dpmg.radio import skiq_bridge as skb
    with _NoEnv("DPMG_RXDRAIN_LIB"):
        c = skb.rxdrain_candidates()
    want = [os.path.join(ROOT, "native", "lib", "librxdrain.so"),
            os.path.join(ROOT, "librxdrain.so")]
    assert [os.path.realpath(x) for x in c[:2]] == [os.path.realpath(x) for x in want], c
    assert _same(c[-1], os.path.join(os.path.dirname(skb.__file__), "librxdrain.so")), c
    os.environ["DPMG_RXDRAIN_LIB"] = "/nowhere/librxdrain.so"
    try:
        c = skb.rxdrain_candidates()
    finally:
        del os.environ["DPMG_RXDRAIN_LIB"]
    assert c == ["/nowhere/librxdrain.so"], c


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:
                fails += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    sys.exit(1 if fails else 0)
