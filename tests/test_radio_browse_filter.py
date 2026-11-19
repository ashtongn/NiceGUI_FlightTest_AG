"""Radio file browser (link_tab.browse_filter): at the service folder only the
data and scenario folders show, below it every folder except hidden ones and
__pycache__, and files show everywhere except the service folder itself."""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

pytest.importorskip("nicegui")
from dpmg.web import link_tab as lt                 # noqa: E402

ROOT = "/app_svc"
ROOT_DIRS = ["__pycache__", "bench", "data", "deploy", "dpmg", "scenarios", "tests", ".git"]
ROOT_FILES = [["dpmg_service.py", 120], ["rf_frontend.yaml", 4]]


def test_service_folder_shows_only_data_and_scenarios():
    dirs, files = lt.browse_filter(ROOT, ROOT, ROOT_DIRS, ROOT_FILES)
    assert dirs == ["data", "scenarios"]
    assert files == []


def test_trailing_slash_still_counts_as_service_folder():
    dirs, _ = lt.browse_filter(ROOT + "/", ROOT, ROOT_DIRS, ROOT_FILES)
    assert dirs == ["data", "scenarios"]


def test_below_service_folder_shows_everything_but_hidden():
    dirs, files = lt.browse_filter(ROOT + "/data", ROOT,
                                   ["captures", "inbox", "iq", ".cache", "__pycache__"],
                                   [["a.iq.bin", 10]])
    assert dirs == ["captures", "inbox", "iq"]
    assert files == [["a.iq.bin", 10]]


def test_unknown_root_filters_hidden_only():
    dirs, files = lt.browse_filter(ROOT, None, ROOT_DIRS, ROOT_FILES)
    assert "deploy" in dirs and "__pycache__" not in dirs and ".git" not in dirs
    assert files == ROOT_FILES


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
