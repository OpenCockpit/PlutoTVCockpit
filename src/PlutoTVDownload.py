# Copyright (C) 2026 by xcentaurix
# License: GNU General Public License v3.0

import datetime
import os
import threading
import time
import zlib
from collections import deque

from Components.ActionMap import ActionMap
from Components.config import config
from Components.Label import Label
from Components.ProgressBar import ProgressBar
from Screens.MessageBox import MessageBox
from Screens.Screen import Screen
from Plugins.SystemPlugins.StreamProxyCockpit.LiveProxyUnmuxed import PROXY_HOST, PROXY_PORT
from enigma import eEPGCache, eTimer
from twisted.internet import reactor, threads

# for localized messages
from . import _
from .Debug import logger
from .ConfigInit import COUNTRY_NAMES, TSIDS, getselectedcountries
from .PlutoTVRequest import plutoRequest
from .Variables import TIMER_FILE, NODATA_FILE, BOUQUET_FILE, BOUQUET_NAME, CHANNELLIST_FILE, XMLTV_FILE
from .CockpitTVDownload import TVDownloadBase, TVDownloadScreenMixin, TVDownloadSilentMixin, FAIL_GEO, FAIL_NETWORK, FAIL_NOSTREAM
from .M3UPlaylist import writeM3UPlaylist
from .XMLTVWriter import writeXMLTVFile


class PlutoTVDownloadBase(TVDownloadBase):
    downloadActive = False  # shared between instances

    TIMER_FILE = TIMER_FILE
    NODATA_FILE = NODATA_FILE
    BOUQUET_FILE = BOUQUET_FILE
    CHANNELLIST_FILE = CHANNELLIST_FILE
    XMLTV_FILE = XMLTV_FILE
    TSIDS = TSIDS

    FINALIZE_DELAY = 3  # let the reactor paint 100% before the next country resets it to 0%
    SILENT_IN_PROGRESS_TEXT = _("A silent download is in progress.")
    PICONS_LABEL = _("picons")
    FETCHING_PICONS_TEXT = _("Fetching picons...")
    UPDATE_COMPLETED_TEXT = _("Live-TV update completed")
    PROCESSING_TEXT = _("Processing data...")
    WAITING_FOR_CHANNEL_TEXT = _("Waiting for Channel: ")

    def __init__(self, silent=False, locations=None):
        TVDownloadBase.__init__(self, silent, locations)
        self.guideList = {}

    def _clearPluginState(self):
        self.guideList.clear()

    def _proxyEnabled(self):
        """True if the bouquet gets the lazy addresses StreamProxyCockpit resolves (setting "Stream proxy"), False for
        the addresses of the stream source (live_tv_mode). The source applies either way."""
        return config.plugins.plutotv.stream_proxy.value

    def _selectedLocations(self):
        return getselectedcountries()

    def _defaultLocation(self):
        return config.plugins.plutotv.country.value

    def _picons_config(self):
        return config.plugins.plutotv.picons

    def _configFolder(self):
        return config.plugins.plutotv.config_folder.value

    def _fetchChannels(self, cc):
        return plutoRequest.getChannels(cc)

    def _fetchFailure(self, cc):
        return plutoRequest.failure(cc)

    def _bouquetName(self, cc):
        return BOUQUET_NAME % COUNTRY_NAMES.get(cc, cc)

    def _afterBouquetWritten(self, cc, bouquet_name):
        # Overrides TVDownloadBase's shared default (which reuses each
        # channel's already-resolved bouquet stream_url) because Pluto's own
        # bouquet ref uses a lazy "pluto://" (or the address of the stream source, per
        # config.plugins.plutotv.live_tv_mode with the stream proxy off) scheme resolved at play time
        # by StreamProxyCockpit (StreamProxy.playServiceExtension) - not portable to tools other
        # than Enigma2. The exported playlist instead points at this proxy's
        # own /auto/ endpoint (only reachable from this box itself, since
        # LiveProxyUnmuxed binds PROXY_HOST=127.0.0.1), which resolves the same way
        # regardless of live_tv_mode.
        entries = {
            group: [(chid, name, logo, f"http://{PROXY_HOST}:{PROXY_PORT}/auto/{chid}.m3u8")
                    for _sid, chid, name, logo, _chid2 in self.channelsList.get(group, [])]
            for group in self.categories
        }
        writeM3UPlaylist(os.path.join(self._configFolder(), self.CHANNELLIST_FILE % cc), bouquet_name, self.categories, entries)

        # Persisted alongside the M3U export, from the same self.guideList
        # data _buildBouquetEntry already fed to eEPGCache.importEvents()
        # directly - this is purely a supplementary artifact for tools other
        # than Enigma2's own EPG cache to consume, the direct import above
        # remains the source of the guide Enigma2 itself displays.
        xmltv_channels = [
            (chid, name)
            for group in self.categories
            for _sid, chid, name, _logo, _chid2 in self.channelsList.get(group, [])
        ]
        xmltv_programmes = [
            (chid, start, start + duration, title, desc)
            for chid, _name in xmltv_channels
            for title, desc, start, duration, _genre, _event_id in self.guideList.get(chid, [])
        ]
        writeXMLTVFile(os.path.join(self._configFolder(), self.XMLTV_FILE % cc), xmltv_channels, xmltv_programmes)

    def _importGuide(self, cc):
        guide = self.getGuidedata(cc)
        for event in guide:
            self.buildGuide(event)
        total_events = sum(len(v) for v in self.guideList.values())
        logger.debug("_importGuide: %s: %d guide entries fetched, %d channels with events, %d events total", cc, len(guide), len(self.guideList), total_events)

    def _bouquetsSwitched(self, locations):
        # The guide is imported under the references of the bouquet that was built last, which include the stream
        # address: after a switch to the stored bouquet of the other stream proxy setting it has to be imported
        # again for the references of that bouquet.
        if locations:
            threads.deferToThread(self.refreshGuide, locations)

    def refreshGuide(self, locations):
        """Import the guide for the bouquets of *locations* (the stored ones just switched to). Runs in a thread.

        The channels are fetched (cached) and numbered again, which gives the same service references as the
        bouquet that was written, the way the Samsung TV Plus plugin refreshes its guide on its own."""
        if not self._claimDownload():   # a running download imports the guide itself
            return
        try:
            for cc in locations:
                try:
                    self.bouquetCC = cc
                    self.bouquetProxy = self._proxyEnabled()
                    self.tsid = self.TSIDS.get(cc, "0")
                    self.usedServiceIds = set()
                    self.channelsList.clear()
                    self.categories.clear()
                    self._clearPluginState()
                    for channel in sorted(self._fetchChannels(cc), key=lambda x: x["number"]):
                        self.buildM3U(channel)
                    self.categories.sort(key=str.casefold)
                    for channels_in_group in self.channelsList.values():
                        channels_in_group.sort(key=lambda ch: ch[2].casefold())
                    self._importGuide(cc)
                    for key in self.categories:
                        for chitem in range(len(self.channelsList[key])):
                            self._buildBouquetEntry(key, chitem)   # queues the guide of the channel
                except Exception as e:   # pylint: disable=broad-except
                    logger.error("%s: failed to refresh the guide: %s", cc, e)
        finally:
            self.channelsList.clear()
            self.categories.clear()
            self._clearPluginState()
            self._setDownloadActive(False)

    def _buildBouquetEntry(self, key, chitem):
        ch_sid, ch_hash, ch_name, ch_logourl, _id = self.channelsList[key][chitem]

        if self.bouquetProxy:
            stream_url = plutoRequest.PLUTO_SCHEMA + _id   # resolved from the stream source when it is played
        else:
            # A channel the source can't give an address for is left out of the bouquet (the download skips an entry
            # without an address), not silently given a proxy address: the setting says no proxy.
            if config.plugins.plutotv.live_tv_mode.value == "jmp2":
                stream_url = plutoRequest.JMP2_URL_TEMPLATE % _id
            else:   # stitcher
                stream_url = plutoRequest.buildDirectStreamURL(_id, self.bouquetCC)
            stream_url = stream_url.replace(":", "%3a")

        ref = f"4097:0:1:{ch_sid}:{self.tsid}:FF:CCCC0000:0:0:0"
        if not stream_url:
            return ref, "", ch_name, ch_logourl

        chevents = []
        if ch_hash in self.guideList:
            for evt in self.guideList[ch_hash]:
                title = evt[0]
                summary = evt[1]
                begin = int(round(evt[2]))
                duration = evt[3]
                genre = evt[4]
                event_id = evt[5]

                chevents.append((begin, duration, title, "", summary, genre, event_id))
        if len(chevents) > 0:
            iterator = iter(chevents)
            events_tuple = tuple(iterator)
            # Must match the bouquet's own service reference (ref:stream_url) exactly -
            # eEPGCache keys these non-DVB extension-type (4097) services by the full
            # reference including the path, not just SID:TSID, since there's no real
            # transponder identity to hash on. A placeholder path here never matches
            # the live channel list entry, so the import silently never displays.
            # Only for the bouquet that is built: switching to the stored bouquet of the other stream proxy setting
            # imports the guide again (refreshGuide), the references of the two differ.
            self._queueEpgImport(f"{ref}:{stream_url}", events_tuple)

        return ref, stream_url, ch_name, ch_logourl

    # One callFromThread per channel made twisted run all of them (hundreds of channels, thousands of events) in
    # a single reactor iteration, which blocks the main thread and shows the "busy" spinner. The channels are
    # queued here and imported on the reactor thread in short batches instead.
    _epgPending = deque()
    _epgLock = threading.Lock()
    _epgDrainScheduled = False

    def _queueEpgImport(self, sref, events):
        cls = PlutoTVDownloadBase
        with cls._epgLock:
            cls._epgPending.append((sref, events))
            if cls._epgDrainScheduled:
                return
            cls._epgDrainScheduled = True
        reactor.callFromThread(self._drainEpgImport)

    def _drainEpgImport(self):
        cls = PlutoTVDownloadBase
        deadline = time.monotonic() + 0.015
        while True:
            with cls._epgLock:
                if not cls._epgPending:
                    cls._epgDrainScheduled = False
                    return
                sref, events = cls._epgPending.popleft()
            started = time.monotonic()
            try:
                eEPGCache.getInstance().importEvents(sref, events)
            except Exception as e:
                logger.error("importEvents failed for %s: %s", sref, e)
            if time.monotonic() - started > 0.1:   # a main thread stall (busy spinner) of this length
                logger.debug("importEvents took %.0f ms for %s (%d events)", (time.monotonic() - started) * 1000, sref, len(events))
            if time.monotonic() >= deadline:
                reactor.callLater(0.01, self._drainEpgImport)
                return

    def buildGuide(self, event):
        # (title, summary, start, duration, genre, event_id)
        _id = event.get("_id", "")
        if len(_id) == 0:
            return
        self.guideList[_id] = []
        timelines = event.get("timelines", [])
        chplot = (event.get("description", "") or event.get("summary", ""))

        for item in timelines:
            episode = (item.get("episode", {}) or item)
            series = (episode.get("series", {}) or item)
            epdur = int(episode.get("duration", "0") or "0") // 1000  # in seconds
            epgenre = episode.get("genre", "")
            etype = series.get("type", "film")

            genre = self.convertgenre(epgenre)

            offset = datetime.datetime.now() - datetime.datetime.utcnow()
            try:
                starttime = self.strpTime(item["start"]) + offset
            except Exception:
                continue
            start = time.mktime(starttime.timetuple())
            title = (item.get("title", ""))
            tvplot = (series.get("description", "") or series.get("summary", "") or chplot)
            epnumber = episode.get("number", 0)
            epseason = episode.get("season", 0)
            epname = episode.get("name", "")
            epmpaa = episode.get("rating", "")
            epplot = (episode.get("description", "") or tvplot or epname)

            if len(epmpaa) > 0 and "Not Rated" not in epmpaa:
                epplot = f"({epmpaa}). {epplot}"

            noserie = ("live", "film")
            if epseason > 0 and epnumber > 0 and etype not in noserie:
                title = f"{title} (T{epseason})"
                epplot = f"T{epseason} Ep.{epnumber} {epplot}"

            if epdur > 0:
                # Pluto's own timeline entry id uniquely identifies this broadcast
                # instance and is stable across guide refreshes. Enigma2's EPG
                # cache falls back to (start & 0xFFFF) when no event id is given,
                # which wraps every ~18h11m and collides between events on the
                # same channel that are that far apart - hash the stable id
                # instead so timers keep tracking the event they were created for.
                item_id = str(item.get("_id") or episode.get("_id") or "")
                event_id = (zlib.crc32(item_id.encode("utf-8")) & 0xFFFF) or 1 if item_id else 0
                self.guideList[_id].append((title, epplot, start, epdur, genre, event_id))

    def buildM3U(self, channel):
        # (number, _id, name, logo, url)
        logo = (channel.get("colorLogoPNG", {}).get("path", None) or None)
        # a channel Pluto lists in no category must not get an empty group name: it
        # becomes a bouquet marker with no name, which enigma2 shows as garbage ("p:CSAT")
        group = channel.get("category", "") or _("Other")
        _id = channel["_id"]

        if group not in self.channelsList:
            self.channelsList[group] = []
            self.categories.append(group)

        if int(channel["number"]) == 0:
            sid = int(_id[-4:], 16) if len(_id) >= 4 else 0
        else:
            sid = int(channel["number"])

        if sid <= 0:
            sid = (zlib.crc32(_id.encode("utf-8")) & 0xFFFF) or 1

        if sid in self.usedServiceIds:
            sid = (zlib.crc32(_id.encode("utf-8")) & 0xFFFF) or 1
            while sid in self.usedServiceIds:
                sid = (sid + 1) & 0xFFFF
                if sid == 0:
                    sid = 1

        self.usedServiceIds.add(sid)
        number = f"{sid:X}"

        self.channelsList[group].append((str(number), _id, channel["name"], logo, _id))
        return True

    @staticmethod
    def convertgenre(genre):
        genre_id = 0
        if genre in {"Classics", "Romance", "Thrillers", "Horror"} or "Sci-Fi" in genre or "Action" in genre:
            genre_id = 0x10
        elif "News" in genre or "Educational" in genre:
            genre_id = 0x20
        elif genre == "Comedy":
            genre_id = 0x30
        elif "Children" in genre:
            genre_id = 0x50
        elif genre == "Music":
            genre_id = 0x60
        elif genre == "Documentaries":
            genre_id = 0xA0
        return genre_id

    @staticmethod
    def getGuidedata(cc):
        start = (datetime.datetime.fromtimestamp(PlutoTVDownloadBase.getLocalTime()).strftime("%Y-%m-%dT%H:00:00Z"))
        stop = (datetime.datetime.fromtimestamp(PlutoTVDownloadBase.getLocalTime()) + datetime.timedelta(hours=int(config.plugins.plutotv.epg_hours.value))).strftime("%Y-%m-%dT%H:00:00Z")
        return sorted(plutoRequest.getBaseGuide(start, stop, cc), key=lambda x: x["number"])

    @staticmethod
    def getLocalTime():
        offset = datetime.datetime.utcnow() - datetime.datetime.now()
        return time.time() + offset.total_seconds()

    @staticmethod
    def strpTime(datestring, fmt="%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.datetime.strptime(datestring, fmt)
        except TypeError:
            return datetime.datetime.fromtimestamp(time.mktime(time.strptime(datestring, fmt)))


class PlutoTVDownload(TVDownloadScreenMixin, PlutoTVDownloadBase, Screen):

    EXIT_CONFIRM_TEXT = _("The download is in progress. Exit now?")

    def __init__(self, session, locations=None):
        self.session = session
        Screen.__init__(self, session)
        self.skinName = "PRSDownloadProgress"
        self.title = _("PlutoTV updating")
        PlutoTVDownloadBase.__init__(self, locations=locations)
        self.total = 0
        self["progress"] = ProgressBar()
        self["action"] = Label()
        self.updateAction()
        self["status"] = Label(_("Please wait..."))
        self["actions"] = ActionMap(["OkCancelActions"], {"cancel": self.exit}, -1)
        self.onFirstExecBegin.append(self.init)

    def updateAction(self, cc=""):
        self["action"].text = _("Updating: Pluto TV %s") % cc.upper()

    def noCategories(self, cc="", reason=None, detail=None):
        # Informational only - the download continues with the next selected
        # location, so this must not close the screen (openWithCallback would).
        name = COUNTRY_NAMES.get(cc, cc)
        if reason == FAIL_GEO:
            text = _("Pluto TV refused the request for %s (HTTP %s). It may be geo-blocked.") % (name, detail)
        elif reason == FAIL_NETWORK:
            text = _("Pluto TV could not be reached for %s%s. Try again later.") % (name, f" (HTTP {detail})" if detail else "")
        elif reason == FAIL_NOSTREAM:
            text = _("Pluto TV lists channels for %s, but the selected source has no stream address for them. Try another source.") % name
        else:
            text = _("Pluto TV has no channels for %s. It may not be available in your country.") % name
        self.session.open(MessageBox, text, type=MessageBox.TYPE_ERROR, timeout=10)

    def _restartSilentTimer(self):
        Silent.stop()
        Silent.start()


class DownloadSilent(TVDownloadSilentMixin, PlutoTVDownloadBase):

    BOUQUET_MARKER = "plutotvcockpit"
    FRIENDLY_NAME = "Pluto TV"
    LOCATION_WORD = "country"

    def __init__(self):
        self.afterUpdate = []  # for callbacks
        PlutoTVDownloadBase.__init__(self, silent=True)
        self.timer = eTimer()
        self.timer.timeout.get().append(self.download)


Silent = DownloadSilent()
