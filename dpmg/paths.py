"""The repository root, for files the code finds by location.

The modules live under dpmg/, but the files they look up stay at the top of the
tree, where the radio and the GUI container keep them: rf_frontend.yaml,
payload_profiles.yaml, output/, docs/datasheets, scenarios/json,
requirements-crypto.txt, mothball_fury.svg and the native libraries.
"""
import logging
import os
import stat
from pathlib import Path

# abspath, not resolve: keep the path as the process sees it (/app_svc in the
# radio container)
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = Path(REPO_DIR)

log = logging.getLogger(__name__)
_owner_warned = set()                  # folders already warned about


def match_folder_owner(path, folder):
    """Give a file to the owner of its folder. The GUI container runs as root over
    a bind mount, and snap LibreOffice refuses a home file the user does not own,
    even a readable one. A failure warns once per folder and never stops the caller.
    A symlink is left alone: output/ is writable by the user, so root following a
    link in it could hand over any file on the system."""
    if not hasattr(os, "chown"):       # Windows console
        return
    try:
        want, have = os.stat(folder), os.lstat(path)
        if not stat.S_ISLNK(have.st_mode) \
                and (have.st_uid, have.st_gid) != (want.st_uid, want.st_gid):
            os.chown(path, want.st_uid, want.st_gid, follow_symlinks=False)
    except OSError as e:
        if folder not in _owner_warned:
            _owner_warned.add(folder)
            log.warning("cannot give %s to the owner of %s: %s", path, folder, e)


def match_owners_under(folder):
    """match_folder_owner for everything below a folder."""
    for root, dirs, files in os.walk(folder):
        for name in dirs + files:
            match_folder_owner(os.path.join(root, name), folder)


def native_lib_candidates(name, module_file=None):
    """Where to look for a ctypes library, in order: native/lib/, the repo root
    (where it was built before native/ existed), then beside module_file."""
    out = [os.path.join(REPO_DIR, "native", "lib", name), os.path.join(REPO_DIR, name)]
    if module_file:
        out.append(os.path.join(os.path.dirname(os.path.abspath(module_file)), name))
    seen = []
    for p in out:
        if p not in seen:
            seen.append(p)
    return seen
