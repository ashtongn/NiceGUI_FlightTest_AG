"""The GUI's aircraft position feeds, one listener per named aircraft.

One registry for the whole GUI (link_tab.FEEDS), built from the saved feed
settings (feed_settings.py): the operator console's aircraft blocks, the
planner's Live feed rows and location triggers, the engineering Aircraft feed
tab and the map markers all read the same listeners, so what one page starts
the others see. reload() follows a settings change: a listener keeps running
across a save unless its group, port or interface changed, in which case it
is started again on the new ones; an aircraft taken off the list has its
listener stopped. update() is the one way a page changes an aircraft's
fields: only the fields the page's user changed, written on the settings as
saved now. rev counts the changes, so a page refills its fields when another
page (or another program writing the file) changed them. Stdlib only, no page
code.
"""

from __future__ import annotations

import os
import time

from dpmg.link import feed_settings
from dpmg.link.acft_pos_multicast import MulticastPosListener


class AircraftFeeds:
    def __init__(self, settings=None, listener_factory=MulticastPosListener):
        self._make = listener_factory
        self._items = []          # [(name, listener)] in settings order
        self._entries = {}        # name: the settings entry
        self.rev = 0              # counts changes to the entries
        self._file_sig = None     # (mtime, size) of the settings file last read
        self.reload(settings)

    # the list

    def names(self) -> list:
        return [n for n, _l in self._items]

    def entries(self) -> list:
        """[(name, listener)] in settings order."""
        return list(self._items)

    def get(self, name):
        for n, lst in self._items:
            if n == name:
                return lst
        return None

    def entry(self, name) -> dict | None:
        return self._entries.get(name)

    def first_name(self):
        return self._items[0][0] if self._items else None

    @property
    def primary(self):
        """The first aircraft's listener: what an unbound radio follows."""
        return self._items[0][1] if self._items else None

    def resolve(self, name):
        """A known aircraft name, else the first one (a plan may name an
        aircraft that was renamed or removed since)."""
        return name if name in self._entries else self.first_name()

    def listener_for(self, name):
        return self.get(self.resolve(name))

    def __len__(self):
        return len(self._items)

    # settings

    def reload(self, settings=None) -> None:
        st = feed_settings.normalize(settings if settings is not None else feed_settings.load())
        new = []
        for e in st["aircraft"]:
            lst = self.get(e["name"])
            fresh = lst is None
            if fresh:
                lst = self._make()
            sock_before = (lst.group, int(lst.port), lst.iface_ip)
            feed_settings.apply_entry(lst, e)
            if not fresh and lst.running and sock_before != (e["group"], int(e["port"]), e["iface"]):
                lst.start()                 # start() closes the old socket first
            new.append((e["name"], lst))
        keep = {n for n, _l in new}
        for n, lst in self._items:
            if n not in keep:
                try:
                    lst.stop()
                except Exception:
                    pass
        self._items = new
        entries = {e["name"]: e for e in st["aircraft"]}
        if entries != self._entries or list(entries) != list(self._entries):
            self.rev += 1
        self._entries = entries
        self._file_sig = self._sig()

    def _sig(self):
        try:
            s = os.stat(feed_settings.settings_path())
            return (s.st_mtime_ns, s.st_size)
        except OSError:
            return None

    def follow_file(self) -> bool:
        """Reload when the settings file changed since it was last read (a
        save by another program, or by hand). Returns True when it did."""
        sig = self._sig()
        if sig is None or sig == self._file_sig:
            return False
        self.reload()
        return True

    def update(self, name, changes: dict) -> tuple:
        """Change only these fields of one aircraft (the first when the name
        is unknown) on the settings as saved now, save, and follow: a running
        listener whose group, port or interface changed starts again on the
        new ones. Returns (name, entry, changed). A save that fails raises
        OSError after the listeners took the new values."""
        self.follow_file()
        name = self.resolve(name)
        st, changed = feed_settings.update_aircraft(name, changes, write=False)
        err = None
        if changed:
            try:
                feed_settings.save(st)
            except OSError as e:
                err = e                     # the listeners still follow; the caller says it
        self.reload(st)
        if err is not None:
            raise err
        return name, self.entry(name), changed

    def rename(self, old, new) -> bool:
        """Move a listener to a new name (its socket, counters and history stay).
        The caller saves the settings with the new name and calls reload()."""
        if old == new or self.get(old) is None or self.get(new) is not None:
            return False
        self._items = [(new if n == old else n, lst) for n, lst in self._items]
        if old in self._entries:
            e = dict(self._entries.pop(old), name=new)
            self._entries[new] = e
        return True

    # sockets shared by two aircraft

    def shared_sockets(self) -> dict:
        by = {}
        for n, lst in self._items:
            by.setdefault((lst.group, int(lst.port)), []).append(n)
        return {k: v for k, v in by.items() if len(v) > 1}

    def shares_with(self, name) -> list:
        lst = self.get(name)
        if lst is None:
            return []
        return [n for n in self.shared_sockets().get((lst.group, int(lst.port)), []) if n != name]

    def socket_note(self, name) -> str:
        others = self.shares_with(name)
        if not others:
            return ""
        lst = self.get(name)
        return (f"shares {lst.group}:{lst.port} with {', '.join(others)}: each has its own "
                f"socket and both receive every datagram; the format tells them apart")

    # running and fixes

    def start(self, name) -> None:
        lst = self.get(name)
        if lst is not None:
            lst.start()

    def stop(self, name) -> None:
        lst = self.get(name)
        if lst is not None:
            lst.stop()

    def stop_all(self) -> None:
        for _n, lst in self._items:
            lst.stop()

    def running(self, name) -> bool:
        lst = self.get(name)
        return bool(lst is not None and lst.running)

    def any_running(self) -> bool:
        return any(lst.running for _n, lst in self._items)

    def latest(self, name):
        lst = self.listener_for(name)
        return lst.latest if lst is not None else None

    def latest_named(self, name):
        """(resolved name, AircraftPos or None) for a radio bound to name."""
        rn = self.resolve(name)
        lst = self.get(rn)
        return rn, (lst.latest if lst is not None else None)

    def fresh(self, max_age_s: float) -> list:
        """[(name, AircraftPos)] of every aircraft with a fix younger than
        max_age_s, in settings order."""
        out = []
        now = time.monotonic()
        for n, lst in self._items:
            pos = lst.latest
            if pos is not None and now - pos.t_mono < max_age_s:
                out.append((n, pos))
        return out


class PanelFields:
    """What one page's feed fields last showed, so the page writes back only
    the fields its user changed and follows a change saved anywhere else.
    fill() gives the values to put in the fields; edits() the ones the user
    changed since; apply() saves a picked format at once; stale() says the
    saved settings moved on since the fill."""

    def __init__(self, feeds):
        self.feeds = feeds
        self.name = None
        self.shown = {}
        self.rev = -1

    def fill(self, name, current=None) -> dict:
        """The saved values of this aircraft for the fields. With current (the
        fields as they are now) and the same aircraft, a field the user has
        changed and not applied keeps the user's value."""
        name = self.feeds.resolve(name)
        e = self.feeds.entry(name) or {}
        vals = {k: e[k] for k in feed_settings.EDIT_KEYS if k in e}
        out = dict(vals)
        if current is not None and name == self.name:
            for k in feed_settings.entry_edits(self.shown, current):
                out[k] = current[k]
        self.name, self.shown, self.rev = name, vals, self.feeds.rev
        return out

    def stale(self) -> bool:
        return self.rev != self.feeds.rev

    def edits(self, current) -> dict:
        if self.name is None:
            return {}
        return feed_settings.entry_edits(self.shown, current)

    def apply(self, current, keys=("format",)):
        """Save these fields at once when the user changed them (a pick from
        a list, the format, needs no Save or Listen), on the settings as
        saved now. A running listener takes a new format on its next
        datagram. A value put in the field from code (a fill) is the one
        shown, so it is no edit and nothing is written. Returns (name,
        entry, changed), or None when none of the keys changed. A save that
        fails raises OSError after the listener took the new value."""
        changes = {k: v for k, v in self.edits(current).items() if k in keys}
        if not changes:
            return None
        try:
            return self.feeds.update(self.name, changes)
        finally:
            # the field now holds the saved value: the next fill keeps no
            # edit of it, and Listen does not send it again
            e = self.feeds.entry(self.feeds.resolve(self.name)) or {}
            for k in changes:
                if k in e:
                    self.shown[k] = e[k]

