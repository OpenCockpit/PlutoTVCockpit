# Copyright (C) 2026 by xcentaurix

"""Shared VoD browse-list Source for the FAST-channel TV Cockpit plugins
(Pluto TV, Rakuten TV, Samsung TV Plus, ...).

Instantiate directly - no per-plugin subclass needed:

    self["feedlist"] = PRSList([])                                             # Rakuten/Samsung: menu icon only
    self["feedlist"] = PRSList([], icons=("menu.png", "series.png", "cine.png",
                                          "cine_half.png", "cine_end.png"),
                                          resume_points=resumePointsInstance)  # Pluto: full VoD icon set

*icons* names the plugin's own bundled skin/images/<file> files

*resume_points* (a ResumePoints instance, see PlutoUtils/RakutenTVUtils/
SamsungTVUtils), if given, enables picking between "cine"/"cine_half"/
"cine_end" (unseen / seeing / seen, see watchStatus()) for movie/episode
entries based on playback progress.

The actual per-row layout (icon + text position/size) is skin-defined, in
Common/src/skin/screenpart_PRSList.ymlinc - included as the "feedlist"
widget's own TemplatedMultiContentEx convert (see
screenpart_PRSPluginBody.ymlinc). listentry() below only supplies the raw
values that template's "value: N" field indices read: index 0 is the
identity tuple (name, data, _id, epid) callers already read back via
getCurrent()[0] - unchanged so PlutoTVCockpit.py/RakutenTVCockpit.py/
SamsungTVCockpit.py's existing getSelection()-style code needed no changes -
index 1 is the icon pixmap, index 2 the display text.
"""

import os

from Components.Sources.List import List
from Tools.Directories import fileExists, resolveFilename, SCOPE_CURRENT_SKIN
from Tools.LoadPixmap import LoadPixmap
from .CutListUtils import secondsToPts
from .Version import PLUGIN

SEEN_MARGIN_PTS = secondsToPts(5 * 60)
STATUS_ICONS = {"unseen": "cine", "seeing": "cine_half", "seen": "cine_end"}


def watchStatus(last, length):
    """Watch state: unseen below 5 min watched, seen within 5 min of the end
    (or past it), seeing in between. *last* and *length* are 90 kHz pts;
    length 0 = unknown, which can never be seen. The margin shrinks to a
    third of the length for clips under 15 min so the states never overlap."""
    if not last:
        return "unseen"
    margin = min(SEEN_MARGIN_PTS, length // 3) if length else SEEN_MARGIN_PTS
    if last < margin:
        return "unseen"
    if length and last > length - margin:
        return "seen"
    return "seeing"


class PRSList(List):
    def __init__(self, entries, icons=("menu.png",), resume_points=None):
        icon_dir = f"/usr/lib/enigma2/python/Plugins/Extensions/{PLUGIN}/skin/images"
        self._resume_points = resume_points
        self._durations = {}  # sid -> length in pts, from the listing
        self._pixmaps = {}

        for icon_file in icons:
            icon_name = os.path.splitext(icon_file)[0]
            fallback = os.path.join(icon_dir, icon_file)
            # Namespaced under PLUGIN, not bare "icons/<file>" - the generic
            # path collides with some images' own system icons of the same name
            # (e.g. OpenATV ships its own icons/menu.png at the shared skin root,
            # a legacy top-level SCOPE_GUISKIN fallback it still resolves through -
            # silently replacing this plugin's 120x120 icon with OpenATV's 438x320
            # main-menu graphic, which then renders as a tiny sliver once fit into
            # this list's small icon box).
            resolved = x if fileExists(x := resolveFilename(SCOPE_CURRENT_SKIN, f"icons/{PLUGIN}/{icon_file}")) else fallback
            self._pixmaps[icon_name] = LoadPixmap(resolved) if fileExists(resolved) else None

        List.__init__(self)
        self.setList(entries)

    # Components.Sources.List has no MenuList-style getSelectionIndex()/
    # moveToIndex() of its own (it exposes an "index" property instead) -
    # these keep PlutoTVCockpit.py/RakutenTVCockpit.py/SamsungTVCockpit.py's
    # existing calls working unchanged.
    def getSelectionIndex(self):
        return self.index

    def moveToIndex(self, index):
        self.index = index

    def refresh(self):
        """Rebuild every entry in place, keeping the selection, so icons that
        depend on playback progress (cine/cine_half/cine_end) pick up resume
        points saved since the list was built - e.g. after leaving the player."""
        if self.list:
            self.updateList([self.listentry(*entry[0]) for entry in self.list])

    def listentry(self, name, data, _id, epid=0, duration=0):
        """*duration* (seconds, from the listing) is remembered per item, so
        refresh() - which only has the identity tuple - still knows it."""
        png = None
        if data == "menu":
            png = self._pixmaps.get("menu")
        elif data in {"series", "seasons"}:
            png = self._pixmaps.get("series")
        elif data in {"movie", "episode"}:
            png = self._pixmaps.get("cine")
            if self._resume_points is not None:
                sid = epid if data == "episode" else _id
                if duration:
                    self._durations[sid] = secondsToPts(duration)
                last, stored_length = self._resume_points.getResumePoint(sid)
                # the listing's duration is reliable; the length stored with the resume point is often 0
                status = watchStatus(last, self._durations.get(sid) or stored_length)
                png = self._pixmaps.get(STATUS_ICONS[status]) or png
        else:
            png = self._pixmaps.get("menu")

        return ((name, data, _id, epid), png, name)
