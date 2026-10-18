# Copyright (C) 2026 by xcentaurix

import os
import re
import shutil
import threading
import time
import traceback
from collections import deque
from io import BytesIO

from Screens.MessageBox import MessageBox
from Tools.Directories import fileExists
from enigma import eDVBDB, eEPGCache, eTimer
from twisted.internet import threads, reactor

from .PiconFetcher import PiconFetcher
from .PRSServiceRef import escapeServiceName
from .M3UPlaylist import writeM3UPlaylist
from .Debug import logger


def loadNoDataLocations(path):
    """Read a NODATA_FILE (one cc per line) into a set - shared by
    TVDownloadBase (which also writes it) and each plugin's Cockpit screen
    (which only needs to read it, to know a missing bouquet is a known
    geo-block rather than one a re-update could still fix)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}
    except OSError:
        return set()


_CLAIM_LOCK = threading.Lock()
UPDATE_INTERVAL_MINUTES = 60 * 5  # between the silent updates; a stored bouquet variant older than this is stale
BOUQUET_DIR = "/etc/enigma2/"
PROXY_SUFFIX = ".proxy"
NOPROXY_SUFFIX = ".noproxy"


def readBouquet(path):
    """(name, [service entries]) of a bouquet file, the entries without the leading "#SERVICE " and the line end
    (the form eDVBDB.addOrUpdateBouquet takes). None if there is no such file."""
    name = None
    entries = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\r\n")
                if line.startswith("#NAME "):
                    name = line[len("#NAME "):]
                elif line.startswith("#SERVICE "):
                    entries.append(line[len("#SERVICE "):])
    except OSError:
        return None
    return name, entries


def patchBouquetName(path, name):
    """addOrUpdateBouquet doesn't update #NAME for existing bouquets, so patch the file."""
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if lines and lines[0].startswith("#NAME"):
            lines[0] = f"#NAME {name}\r\n"
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)


def refreshChannelList():
    """Show the changed bouquets in the channel list that is open, without restarting Enigma2.

    eDVBDB.addOrUpdateBouquet() has replaced the bouquet in Enigma2's database, but the list box of the channel
    selection keeps its own copy of the services of the root it shows. resetRoot() reads the root again and keeps
    the selection. Call it on the main thread."""
    try:
        from Screens.InfoBar import InfoBar   # pylint: disable=import-outside-toplevel
        channelSelection = InfoBar.instance and InfoBar.instance.servicelist
        if channelSelection and channelSelection.servicelist.instance:
            root = channelSelection.getRoot()
            # a root that is not a bouquet (e.g. the providers of IPTVServiceCockpit) isn't read from the database
            if root and "FROM BOUQUET" in root.getPath():
                channelSelection.servicelist.resetRoot()
    except Exception as e:   # pylint: disable=broad-except
        logger.error("Failed to refresh the channel list: %s", e)


def removeBouquetVariants(bouquet_file, suffixes=(PROXY_SUFFIX, NOPROXY_SUFFIX)):
    """Delete the stored proxy / non-proxy copies (<bouquet>.proxy, <bouquet>.noproxy) of every bouquet matching
    *bouquet_file*, a BOUQUET_FILE template (only the ones with the given *suffixes*). eDVBDB.removeBouquet()
    doesn't know them: they are not loaded bouquets."""
    variant_re = re.compile(re.escape(bouquet_file) % ".+" + "(" + "|".join(re.escape(x) for x in suffixes) + ")")
    try:
        filenames = os.listdir(BOUQUET_DIR)
    except OSError:
        return
    for filename in filenames:
        if variant_re.fullmatch(filename):
            try:
                os.remove(BOUQUET_DIR + filename)
            except OSError as e:
                logger.error("Failed to remove %s: %s", filename, e)


# Why a location came back without bouquet (see TVDownloadBase.noCategories)
FAIL_GEO = "geo"            # the service refused the request (HTTP 403/451): geo-blocked
FAIL_NETWORK = "network"    # the service could not be reached or answered with an error
FAIL_EMPTY = "empty"        # the service answered, but lists no channels for the location
FAIL_NOSTREAM = "nostream"  # channels are listed, but no stream address could be resolved for any of them


def classifyFailure(error, geoStatuses=(403, 451)):
    """(reason, detail) of a failed request: *error* is the exception. detail is the HTTP status, if there was one."""
    status = getattr(getattr(error, "response", None), "status_code", None)
    if status in geoStatuses:
        return FAIL_GEO, status
    return FAIL_NETWORK, status


class FailureTracker:
    """What the requests of a plugin remember about why fetching the channels of a location failed, to tell
    geo-blocking from an outage from a location without channels. A request class inherits it, records the
    exceptions of its fetches and clears the record when a fetch succeeded."""

    def recordFailure(self, key, error, geoStatuses=(403, 451)):
        reason, detail = classifyFailure(error, geoStatuses)
        failures = self.__dict__.setdefault("_failures", {})
        if key not in failures or reason == FAIL_GEO:   # a refusal says more than an outage
            failures[key] = (reason, detail)

    def clearFailure(self, key):
        self.__dict__.setdefault("_failures", {}).pop(key, None)

    def failure(self, key):
        """(reason, detail) of the failed fetch for *key*, None if it didn't fail."""
        return self.__dict__.setdefault("_failures", {}).get(key)


class TVDownloadBase:
    """Shared bouquet/EPG download workflow for FAST-channel TV Cockpit plugins
    (Pluto TV, Rakuten TV, Samsung TV Plus, ...).

    A concrete plugin subclasses this (redeclaring ``downloadActive`` on its own
    class so the flag is shared between that plugin's interactive and silent
    download instances, but independent from other plugins) and provides:

    Required class attributes:
        downloadActive (bool)   -- must be redeclared, not inherited, per plugin
        LOG_PREFIX (str)        -- tag used in internal (untranslated) log prints
        TIMER_FILE (str)        -- from the plugin's own Variables.py
        NODATA_FILE (str)       -- from the plugin's own Variables.py; persists which
                                    selected locations came back empty (e.g.
                                    geo-blocked) on their last attempt, so the
                                    "needs updating" status doesn't nag forever
                                    about a location a re-update can't fix
        BOUQUET_FILE (str)      -- from the plugin's own Variables.py
        TSIDS (dict)            -- from the plugin's own Config.py
        SILENT_IN_PROGRESS_TEXT, PICONS_LABEL, FETCHING_PICONS_TEXT,
        UPDATE_COMPLETED_TEXT, PROCESSING_TEXT, WAITING_FOR_CHANNEL_TEXT
                                 -- translated (via the plugin's own `_`) strings
        FINALIZE_DELAY (float)  -- optional pause after writing a bouquet, before
                                    the next country/region starts; defaults to 0
        EPGIMPORT_MISSING_TEXT (str) -- shown once at completion (interactive) or
                                    logged (silent) if a plugin's _importGuide sets
                                    self.epgimport_missing = True; defaults to a
                                    plain, untranslated message - override with a
                                    translated (via the plugin's own `_`) string

    Required hook methods:
        _selectedLocations(self) -> list[str]
        _defaultLocation(self) -> str
        _picons_config(self) -> ConfigElement
        _configFolder(self) -> str -- the plugin's configured export folder
                                    (config.plugins.<plugin>.config_folder.value),
                                    used for the default _afterBouquetWritten's
                                    M3U export and by plugins' own _importGuide
                                    for the XMLTV export
        _fetchChannels(self, cc) -> list[dict]
        buildM3U(self, channel)
        _bouquetName(self, cc) -> str
        _buildBouquetEntry(self, key, chitem) -> (ref, stream_url, ch_name, ch_logourl)
        _proxyEnabled(self) -> bool -- the plugin's "Stream proxy" setting. A bouquet is built for it (read
                                    once per bouquet into self.bouquetProxy, which the plugin's
                                    _buildBouquetEntry/_afterBouquetWritten must use instead of the setting)

    Proxy / non-proxy bouquets:
        <bouquet>.tv is always the bouquet Enigma2 uses and reflects the current setting. Every bouquet that is
        written is also stored as <bouquet>.tv.proxy or <bouquet>.tv.noproxy (by the setting it was built for), and
        so is its channellist export. Changing the setting puts the stored variant in place of <bouquet>.tv
        (switchBouquetVariants; addOrUpdateBouquet, no restart), locations without a stored variant are generated.

    Optional hooks:
        _importGuide(self, cc)   -- called once per country/region before the
                                     per-channel loop; no-op by default
        _clearPluginState(self)  -- called whenever channelsList/categories are
                                     reset; no-op by default
        _afterBouquetWritten(self, cc, bouquet_name) -- called once per
                                     country/region right after its bouquet
                                     file is written. Default: if
                                     CHANNELLIST_FILE is set, writes an
                                     Extended-M3U export to
                                     <_configFolder()>/<CHANNELLIST_FILE % cc>
                                     from self.m3uEntries, reusing each
                                     channel's already-resolved bouquet
                                     stream_url (no extra network calls).
                                     Override this entirely (as PlutoTVCockpit
                                     does) when the bouquet's own stream_url
                                     isn't suitable for external consumption
                                     (e.g. a lazy-resolved custom scheme) and
                                     a different, independently-built URL is
                                     needed for the export instead.

    Optional class attribute:
        CHANNELLIST_FILE (str)  -- printf-style filename template (e.g.
                                    "channellist.rakutentvcockpit_%s.m3u8"),
                                    formatted with cc, for the default
                                    _afterBouquetWritten's M3U export. None
                                    (the default) disables the export.
    """

    downloadActive = False
    FINALIZE_DELAY = 0
    EPGIMPORT_MISSING_TEXT = "EPGImport plugin not installed - no EPG data was imported."
    CHANNELLIST_FILE = None

    def __init__(self, silent=False, locations=None):
        self.channelsList = {}
        self.categories = []
        self.m3uEntries = {}  # group -> [(chid, name, logo, stream_url), ...], for _afterBouquetWritten
        self.state = 1  # this is a hack
        self.silent = silent
        self.epgcache = eEPGCache.getInstance()
        self.epgimport_missing = False  # set by importXMLTVGuide() callers; persists for the whole
        #                                 run (not reset per-region) so it can be surfaced once at
        #                                 completion instead of only ever appearing in the debug log
        self._downloadLocations = locations  # restrict cc() to just these (None/empty = every selected location)
        self._updatedLocations = []  # cc's that actually resolved channels and had their bouquet (re)written
        self._bouquetsChanged = False  # a bouquet was written whose channels differ from the ones in the channel list
        # (not _setDownloadActive(False) here: a download that is running would be reported as finished by the
        # instance that is created for the next one)
        logger.info("%s: created for %s", type(self).__name__, locations or "the selected locations")
        logger.debug("created by:\n%s", "".join(traceback.format_stack(limit=8)[:-1]))

    # ------------------------------------------------------------------
    # downloadActive is shared between a plugin's interactive and silent
    # download instances, so it must live on the plugin-level base class
    # (whichever ancestor first declares it), not on `type(self)`.
    # ------------------------------------------------------------------

    def _downloadActiveOwner(self):
        for klass in type(self).__mro__:
            if "downloadActive" in klass.__dict__:
                return klass
        return TVDownloadBase

    def _isDownloadActive(self):
        return self._downloadActiveOwner().downloadActive

    def _setDownloadActive(self, value):
        self._downloadActiveOwner().downloadActive = value

    # ------------------------------------------------------------------
    # Optional hooks
    # ------------------------------------------------------------------

    def _importGuide(self, cc):
        pass

    def _clearPluginState(self):
        pass

    def _afterBouquetWritten(self, cc, bouquet_name):
        if self.CHANNELLIST_FILE:
            path = os.path.join(self._configFolder(), self.CHANNELLIST_FILE % cc)
            writeM3UPlaylist(path, bouquet_name, self.categories, self.m3uEntries)

    def _fetchFailure(self, cc):  # pylint: disable=unused-argument
        """(reason, detail) why the channels of *cc* could not be fetched (FAIL_GEO, FAIL_NETWORK), None if the
        request didn't fail: then there are simply no channels (FAIL_EMPTY). The plugin gets it from its request."""
        return None

    def _streamFailure(self, cc):  # pylint: disable=unused-argument
        """(reason, detail) why no channel of *cc* got a stream address, None if the plugin can't tell (then it is
        FAIL_NOSTREAM). A service that answers a blocked location with an error when the stream is asked for
        (Rakuten TV) says FAIL_GEO here."""
        return None

    def _proxyEnabled(self):
        raise NotImplementedError

    def _bouquetsSwitched(self, locations):
        """Called (on the main thread) with the locations whose bouquet was replaced by the stored one of the other
        stream proxy setting, and not generated again. A plugin whose guide depends on the stream addresses of the
        bouquet imports it again here."""

    # ------------------------------------------------------------------
    # Proxy / non-proxy variants of the bouquets
    # ------------------------------------------------------------------

    @staticmethod
    def _variantSuffix(proxy):
        return PROXY_SUFFIX if proxy else NOPROXY_SUFFIX

    def _variantSources(self, cc):
        """The files of a location that differ between proxy and non-proxy: the bouquet (first, it is the one
        Enigma2 loads) and the channellist export."""
        paths = [BOUQUET_DIR + self.BOUQUET_FILE % cc]
        if self.CHANNELLIST_FILE:
            paths.append(os.path.join(self._configFolder(), self.CHANNELLIST_FILE % cc))
        return paths

    @staticmethod
    def _replaceFile(source, destination):
        """Copy *source* over *destination* in one step, so nobody reads a half written file."""
        temp = destination + ".tmp"
        shutil.copyfile(source, temp)
        os.replace(temp, destination)

    def _storeVariants(self, cc, proxy):
        """Store the files just written for *cc* as the variant for *proxy*."""
        for path in self._variantSources(cc):
            if os.path.isfile(path):
                try:
                    self._replaceFile(path, path + self._variantSuffix(proxy))
                except OSError as e:
                    logger.error("%s: failed to store %s: %s", cc, path, e)

    def switchBouquetVariants(self):
        """Make the bouquets of the selected locations those of the current "Stream proxy" setting.

        The stored variant of the setting replaces the bouquet in Enigma2's database (and the file) the way a
        generated bouquet does, eDVBDB.addOrUpdateBouquet(): the other bouquets are left alone and the open channel
        list shows the change at once, no restart of Enigma2 is needed. Returns the locations that have to be
        generated: those without a stored variant (or without a bouquet at all), and those whose variant is older
        than the update interval. The stale ones are switched to first, so the bouquet is the one of the setting
        even if generating it fails. Call it on the main thread."""
        suffix = self._variantSuffix(self._proxyEnabled())
        missing = []
        switched = []
        for cc in [x for x in self._selectedLocations() if x] or [self._defaultLocation()]:
            sources = self._variantSources(cc)
            bouquet = sources[0]
            current = readBouquet(bouquet)
            variant = readBouquet(bouquet + suffix)
            if current is None or variant is None:
                missing.append(cc)
                continue
            try:
                if [x.lower() for x in variant[1]] != [x.lower() for x in current[1]]:   # Enigma2 writes the hex fields lower case
                    name = variant[0] or self._bouquetName(cc)
                    eDVBDB.getInstance().addOrUpdateBouquet(name, os.path.basename(bouquet), variant[1], False)
                    patchBouquetName(bouquet, name)
                    switched.append(cc)
                for path in sources[1:]:   # the channellist export
                    if os.path.isfile(path + suffix):
                        self._replaceFile(path + suffix, path)
                if time.time() - os.path.getmtime(bouquet + suffix) > UPDATE_INTERVAL_MINUTES * 60:
                    missing.append(cc)
            except (OSError, TypeError) as e:
                logger.error("%s: failed to switch the bouquet: %s", cc, e)
                missing.append(cc)
        if switched:
            refreshChannelList()
            if (kept := [cc for cc in switched if cc not in missing]):   # the others are generated
                self._bouquetsSwitched(kept)
        return missing

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------

    def _existingBouquets(self):
        """Yield (cc, filename) for every userbouquet file on disk matching
        this plugin's BOUQUET_FILE template, by reversing the %s placeholder
        back into a capturing group."""
        cc_re = re.compile(re.escape(self.BOUQUET_FILE) % "(.+)")
        try:
            filenames = os.listdir("/etc/enigma2/")
        except OSError:
            return
        for filename in filenames:
            m = cc_re.fullmatch(filename)
            if m:
                yield m.group(1), filename

    def _removeBouquet(self, cc, filename):
        """Delete an existing bouquet that's no longer part of the location
        config, via eDVBDB.removeBouquet() (which takes a filename regex, so
        the literal filename is escaped) - along with that location's
        supplementary channellist/XMLTV exports, which would otherwise be
        left orphaned on disk with no bouquet left to match them to."""
        eDVBDB.getInstance().removeBouquet(re.escape(filename))
        for path in self._variantSources(cc):
            for suffix in (PROXY_SUFFIX, NOPROXY_SUFFIX):
                self._removeIfExists(path + suffix)
        if self.CHANNELLIST_FILE:
            self._removeIfExists(os.path.join(self._configFolder(), self.CHANNELLIST_FILE % cc))
        self._removeIfExists(os.path.join(self._configFolder(), self.XMLTV_FILE % cc))

    @staticmethod
    def _removeIfExists(path):
        try:
            os.remove(path)
        except OSError:
            pass

    def _loadNoDataLocations(self):
        return loadNoDataLocations(self.NODATA_FILE)

    def _saveNoDataLocations(self, locations):
        os.makedirs(os.path.dirname(self.NODATA_FILE), exist_ok=True)
        with open(self.NODATA_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(locations)))

    def _markLocationNoData(self, cc):
        """Remember that *cc* came back empty (e.g. geo-blocked) this run, so a
        status check elsewhere doesn't keep claiming a re-update would help."""
        locations = self._loadNoDataLocations()
        if cc not in locations:
            locations.add(cc)
            self._saveNoDataLocations(locations)

    def _markLocationUpdated(self, cc):
        """Clear any stale no-data marker for *cc* once it's successfully updated."""
        locations = self._loadNoDataLocations()
        if cc in locations:
            locations.discard(cc)
            self._saveNoDataLocations(locations)

    def cc(self):
        locations = [x for x in self._selectedLocations() if x] or [self._defaultLocation()]
        # Remove bouquets of not selected locations. Don't touch the ones we are updating so they retain their current position.
        for cc, filename in self._existingBouquets():
            if cc not in locations:
                self._removeBouquet(cc, filename)
        # A caller-restricted subset (e.g. "update just this one bouquet") only
        # narrows what gets (re)built below - it must never narrow the pruning
        # above, or picking a single location would delete every other one.
        yield from (self._downloadLocations if self._downloadLocations else locations)

    def _claimDownload(self):
        """Mark a download as running, False if there is one. The flag was only set by the thread that runs the
        download, so two instances that started together (the screens of two updates opened at once) both found
        it clear and downloaded at the same time."""
        with _CLAIM_LOCK:
            if self._isDownloadActive():
                return False
            self._setDownloadActive(True)
            return True

    def download(self):
        if not self._claimDownload():
            if not self.silent:
                self.session.openWithCallback(self.close, MessageBox, self.SILENT_IN_PROGRESS_TEXT, MessageBox.TYPE_INFO, timeout=30)
            logger.info("A download is in progress, %s is not started.", type(self).__name__)
            return
        logger.info("%s: download of %s", type(self).__name__, self._downloadLocations or "the selected locations")
        # Bouquets and picons are deliberately NOT blanket-cleared here, for
        # either a manual or a silent run: cc() below already removes any
        # bouquet whose location is no longer selected, a selected location's
        # bouquet is fully replaced by addOrUpdateBouquet() once its own fetch
        # succeeds, and PiconFetcher.addPicon() only queues a channel whose
        # picon file isn't already on disk. Wiping everything upfront here
        # used to force a full bouquet+picon re-fetch on every manual update
        # and left a location's bouquet permanently empty whenever its fetch
        # failed (e.g. a geo/VPN mismatch for that country), since nothing
        # then rewrote it for the rest of the run. A full reset is still
        # available as an explicit action (e.g. a "Remove picons" button)
        # via PiconFetcher(...).removeall(), just no longer automatic here.
        try:
            self._updatedLocations = []
            self._bouquetsChanged = False
            self.ccGenerator = self.cc()
            self.piconFetcher = PiconFetcher(self._picons_config(), self)
            threads.deferToThread(self._downloadThread)
        except Exception:
            self._setDownloadActive(False)   # the claim, nothing runs
            raise

    def _downloadThread(self):
        """Run the entire download workflow in a background thread."""
        try:
            self._managerThread()
        except Exception as e:
            logger.error("Error in download thread: %s", e)
            self._setDownloadActive(False)

    def _managerThread(self):
        self._setDownloadActive(True)
        if cc := next(self.ccGenerator, None):
            self._downloadBouquetThread(cc)
        else:
            self.channelsList.clear()
            self.categories.clear()
            self._clearPluginState()
            self._setDownloadActive(False)
            self.ccGenerator = None
            if self.piconFetcher.piconList:
                self.total = len(self.piconFetcher.piconList)
                reactor.callFromThread(self.updateProgressBar, 0)
                reactor.callFromThread(self.updateAction, self.PICONS_LABEL)
                reactor.callFromThread(self.updateStatus, self.FETCHING_PICONS_TEXT)
                self.piconFetcher.fetchPicons()
                reactor.callFromThread(self.updateProgressBar, self.total)
            self.piconFetcher = None
            if self.epgimport_missing:
                # Surfaced once here instead of only as a per-region debug-log
                # print: otherwise bouquets/channels update fine and nothing
                # ever indicates *why* the guide stayed empty.
                reactor.callFromThread(self.updateStatus, self.EPGIMPORT_MISSING_TEXT)
                time.sleep(5)
            reactor.callFromThread(self.updateStatus, self.UPDATE_COMPLETED_TEXT)
            time.sleep(3)
            if self._bouquetsChanged:
                self._bouquetsChanged = False
                reactor.callFromThread(refreshChannelList)   # instead of a restart of Enigma2
            reactor.callFromThread(self.exitOk)
            reactor.callFromThread(self.start)

    def manager(self):
        """Legacy entry point - redirects to threaded version."""
        threads.deferToThread(self._managerThread)

    def _downloadBouquetThread(self, cc):
        self.bouquet = []
        self.resolved_count = 0
        self.bouquetCC = cc
        self.bouquetProxy = self._proxyEnabled()   # the setting this bouquet is built for, even if it is changed meanwhile
        self.tsid = self.TSIDS.get(cc, "0")
        self.usedServiceIds = set()
        reactor.callFromThread(self.stop)
        self.channelsList.clear()
        self.categories.clear()
        self.m3uEntries.clear()
        self._clearPluginState()
        reactor.callFromThread(self.updateAction, cc)
        reactor.callFromThread(self.updateProgressBar, 0)
        reactor.callFromThread(self.updateStatus, self.PROCESSING_TEXT)

        try:
            self._buildBouquet(cc)
        except Exception as e:
            # A single location's failure (network error, bad API response, ...)
            # must not abort the remaining selected locations - log it and move
            # on to the next one via the same path a successful location uses.
            logger.error("%s: failed to update, skipping to the next location: %s", cc, e)
            self._managerThread()

    def _buildBouquet(self, cc):
        channels = sorted(self._fetchChannels(cc), key=lambda x: x["number"])
        for channel in channels:
            self.buildM3U(channel)

        # Sort categories alphabetically, and channels within each category by name
        self.categories.sort(key=str.casefold)
        for _group, channels_in_group in self.channelsList.items():
            channels_in_group.sort(key=lambda ch: ch[2].casefold())

        # Not len(channels): a plugin's buildM3U() may silently drop entries
        # (e.g. an ignore list), and updateprogress()'s category/chitem walk
        # must match the real entry count exactly, or it runs past the last
        # category before self.total is reached and the run never reaches 100%.
        self.total = sum(len(v) for v in self.channelsList.values())

        if len(self.categories) == 0:
            reason, detail = self._fetchFailure(cc) or (FAIL_EMPTY, None)
            logger.error("%s: no channels (%s%s)", cc, reason, f", HTTP {detail}" if detail else "")
            if reason in (FAIL_GEO, FAIL_EMPTY):   # only these are settled, an outage may be over the next time
                self._markLocationNoData(cc)
            reactor.callFromThread(self.noCategories, cc, reason, detail)
            self._managerThread()  # no data for this location - skip it, continue with the rest of the queue
        else:
            if self.categories[0] in self.channelsList:
                self.subtotal = len(self.channelsList[self.categories[0]])
            else:
                self.subtotal = 0
            self.key = 0
            self.chitem = 0

            self._importGuide(cc)

            for i in range(self.total + 1):
                self.updateprogress(param=i)

    def updateprogress(self, param):
        if hasattr(self, "state") and self.state == 1:  # hack for exit before end
            reactor.callFromThread(self.updateProgressBar, param)
            if param < self.total:
                key = self.categories[self.key]
                if self.chitem == self.subtotal:
                    self.chitem = 0
                    found = False
                    while not found:
                        self.key += 1
                        key = self.categories[self.key]
                        found = key in self.channelsList
                    self.subtotal = len(self.channelsList[key])

                if self.chitem == 0:
                    self.bouquet.append(f"1:64:{self.key}:0:0:0:0:0:0:0::{escapeServiceName(self.categories[self.key])}")

                chid = self.channelsList[key][self.chitem][1]
                ref, stream_url, ch_name, ch_logourl = self._buildBouquetEntry(key, self.chitem)
                if stream_url:
                    self.resolved_count += 1
                    self.bouquet.append(f"{ref}:{stream_url}:{escapeServiceName(ch_name)}")
                    # _buildBouquetEntry's stream_url has colons pre-escaped as %3a
                    # (required for embedding in the colon-joined bouquet string
                    # above) - the M3U export needs a real, plain URL instead, so
                    # this reverses that one escaping step rather than writing
                    # "https%3a//..." as literal text into the playlist file.
                    self.m3uEntries.setdefault(key, []).append((chid, ch_name, ch_logourl, stream_url.replace("%3a", ":")))
                    self.piconFetcher.addPicon(ref, ch_name, ch_logourl, self.silent)
                else:
                    # No resolvable stream this build (e.g. a transient API
                    # failure for this one channel) - skip it entirely rather
                    # than writing a bouquet line with an empty URL slot.
                    logger.debug("%s: no resolvable stream URL, skipping from bouquet", ch_name)
                self.chitem += 1
                reactor.callFromThread(self.updateStatus, self.WAITING_FOR_CHANNEL_TEXT + ch_name)
            elif self.total > 0 and self.resolved_count == 0:
                # Every channel's stream URL failed to resolve (e.g. a geo/VPN
                # mismatch for this location's streaming endpoint specifically,
                # even though its channel list fetched fine) - writing self.bouquet
                # here would replace a previously good bouquet with one that has
                # only category headers and no channels. Leave it untouched and
                # move on, the same as the no-categories case above.
                logger.error("%s: no channel resolved a stream URL, leaving existing bouquet unchanged", self.bouquetCC)
                reason, detail = self._streamFailure(self.bouquetCC) or (FAIL_NOSTREAM, None)
                if reason in (FAIL_GEO, FAIL_EMPTY):   # settled: the location is blocked, not worth asking again
                    self._markLocationNoData(self.bouquetCC)
                reactor.callFromThread(self.noCategories, self.bouquetCC, reason, detail)
                self._managerThread()
            else:
                self._updatedLocations.append(self.bouquetCC)
                self._markLocationUpdated(self.bouquetCC)
                bouquet_name = self._bouquetName(self.bouquetCC)
                bouquet_file = self.BOUQUET_FILE % self.bouquetCC
                bouquet_path = "/etc/enigma2/" + bouquet_file
                previous = readBouquet(bouquet_path)   # before the file is replaced
                # (lower case: Enigma2 writes the hex fields of the references that way, the bouquet is built upper case)
                if previous is None or [x.lower() for x in previous[1]] != [x.lower() for x in self.bouquet]:
                    self._bouquetsChanged = True   # the channel list that is open shows the old ones
                # Wait for it: it runs on the reactor thread, and the patch below reads and rewrites the very file it
                # writes. Not waiting, the patch could read the old bouquet before it was replaced and write that
                # back over the new one: Enigma2 then runs with the new addresses until the next restart, and loads
                # the old ones from the file then (lazy addresses of the stream proxy replaced by direct ones).
                threads.blockingCallFromThread(reactor, eDVBDB.getInstance().addOrUpdateBouquet, bouquet_name, bouquet_file, self.bouquet, False)
                patchBouquetName(bouquet_path, bouquet_name)
                self._afterBouquetWritten(self.bouquetCC, bouquet_name)
                self._storeVariants(self.bouquetCC, self.bouquetProxy)
                os.makedirs(os.path.dirname(self.TIMER_FILE), exist_ok=True)  # create config folder recursive if not exists
                with open(self.TIMER_FILE, "w", encoding="utf-8") as f:
                    f.write(str(time.time()))
                time.sleep(self.FINALIZE_DELAY)  # let the reactor paint 100% before the next phase resets it to 0%
                self._managerThread()

    # ------------------------------------------------------------------
    # No-op defaults, overridden by the interactive Screen and the silent timer
    # ------------------------------------------------------------------

    def start(self):
        pass

    def stop(self):
        pass

    def exitOk(self, answer=None):
        pass

    def updateProgressBar(self, param):
        pass

    def updateStatus(self, name):
        pass

    def updateAction(self, cc=""):
        pass

    def noCategories(self, cc="", reason=None, detail=None):
        pass


class TVDownloadScreenMixin:
    """Shared UI plumbing for the interactive download-progress Screen.

    The host class (which must also inherit its plugin's TVDownloadBase
    subclass and Screen, in that order) still owns __init__ (skinName, title
    and widget creation need the plugin's own translated strings), and must
    define EXIT_CONFIRM_TEXT plus a _restartSilentTimer() hook.
    """

    def init(self):
        self["progress"].setValue(0)
        threads.deferToThread(self.download)

    def exit(self):
        self.session.openWithCallback(self.cleanup, MessageBox, self.EXIT_CONFIRM_TEXT, MessageBox.TYPE_YESNO, timeout=30)

    def cleanup(self, answer=None):
        if answer:
            self._setDownloadActive(False)
            self.exitOk(answer)

    def exitOk(self, answer=True):
        if answer and getattr(self, "session", None) is not None:   # not if the screen has been closed meanwhile (it has lost its session)
            if self.session.current_dialog is not self:
                # A message is still on top of this screen (e.g. "no data for this region", shown for 10 s while the
                # update went on): Session.close asserts that the screen to close is the one on top, so closing now
                # fails, the setup behind this screen never gets to save its settings, and the screen stays open.
                # Tried again by an eTimer: a reactor.callLater is not run while nothing else wakes the twisted reactor,
                # which is the case here (the update is over), and the screen was never closed.
                self.exitTimer = eTimer()
                self.exitTimer.callback.append(lambda: self.exitOk(answer))
                self.exitTimer.start(500, True)
                return
            self._restartSilentTimer()
            self.close(self._updatedLocations)

    def updateProgressBar(self, param):
        try:
            progress = min(((param + 1) * 100) // self.total, 100)
        except Exception:
            progress = 0
        logger.debug("progress: %s", progress)
        self["progress"].setValue(progress)
        try:
            self["wait"].text = str(progress) + " %"
        except KeyError:   # the screen of the plugin has no such label (a traceback in the log for every update)
            pass

    def updateStatus(self, name):
        self["status"].text = name


class TVDownloadSilentMixin:
    """Shared background/silent-timer plumbing for FAST-channel TV Cockpit plugins.

    The host class still owns __init__ (it must call the plugin's
    TVDownloadBase.__init__(self, silent=True) and create self.timer), and
    must define BOUQUET_MARKER, FRIENDLY_NAME and LOCATION_WORD.
    """

    def init(self, session):  # called on session start
        self.session = session
        with open("/etc/enigma2/bouquets.tv", "r", encoding="utf-8") as f:
            bouquets = f.read()
        if self.BOUQUET_MARKER in bouquets:
            self.start(True)

    def start(self, fromSessionStart=False):
        self.stop()
        minutes = UPDATE_INTERVAL_MINUTES
        if fileExists(self.TIMER_FILE):
            with open(self.TIMER_FILE, "r", encoding="utf-8") as f:
                last = float(f.read().strip())
            minutes -= int((time.time() - last) / 60)
            if minutes < 0:
                minutes = 1  # do we want to do this so close to reboot
        self.timer.startLongTimer(minutes * 60)
        if not fromSessionStart:
            self.afterUpdateCallbacks()

    def stop(self):
        self.timer.stop()

    def afterUpdateCallbacks(self):
        for f in self.afterUpdate:
            if callable(f):
                f()

    def noCategories(self, cc="", reason=None, detail=None):  # pylint: disable=unused-argument
        # Just a per-location notice now - the queue continues with the next
        # location (TVDownloadBase._buildBouquet), and the timer/TIMER_FILE
        # bookkeeping this used to do here happens once, correctly, at the
        # true end of the run (TVDownloadBase._managerThread's finalize branch).
        logger.debug("There is no data for %s (%s), it is possible that %s is not available in your %s.", cc, reason, self.FRIENDLY_NAME, self.LOCATION_WORD)


def importXMLTVGuide(epgcache, log_prefix, path, xmltv_bytes, channels_map):
    """Import EPG events from an XMLTV byte blob into eEPGCache via EPGImport's
    converter, using a pre-built {channel_id: service_ref} mapping.

    *xmltv_bytes* is also written to *path*, a supplementary artifact for
    tools other than Enigma2's own EPG cache to consume, from the same guide
    data already fetched here. Best-effort: a failure to write it doesn't
    affect the return value or abort the actual eEPGCache import below.

    Returns False if the EPGImport plugin itself isn't installed (the import
    was skipped entirely, not just unsuccessful) so callers can surface this
    to the user instead of it only ever showing up as a debug-log print
    nobody reads - this is a hard requirement (there is no fallback path),
    so silently no-op'ing left users with bouquets/channels updating fine
    and no indication at all of why the guide stayed empty. Returns True
    otherwise, regardless of how many events actually matched a channel
    (that part already gets its own "N EPG events imported" print).
    """
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fp:
            fp.write(xmltv_bytes)
    except OSError as e:
        logger.error("%s: failed to write XMLTV guide to %s: %s", log_prefix, path, e)

    try:
        from Plugins.Extensions.EPGImport.xmltvconverter import XMLTVConverter
    except ImportError:
        logger.error("%s: EPGImport not available, skipping EPG import", log_prefix)
        return False

    try:
        xmltv_parser = XMLTVConverter(channels_map, {})
        evt_cnt = 0
        fail_cnt = [0]  # mutable cell - _drain is a nested closure, can't rebind a plain int from inside it
        pending = deque()

        def _logSummary():
            if fail_cnt[0]:
                logger.error("%s: %s of %s EPG events failed to import (see errors above)", log_prefix, fail_cnt[0], evt_cnt)
            else:
                logger.debug("%s: %s EPG events imported", log_prefix, evt_cnt)

        def _drain():
            # Import for a few ms per reactor iteration. One callFromThread per event made twisted run
            # all of them in a single iteration (seconds for a full guide), which blocked the main
            # thread and showed the "busy" spinner.
            deadline = time.monotonic() + 0.015
            while pending:
                sref, event = pending.popleft()
                started = time.monotonic()
                try:
                    epgcache.importEvents(sref, [event])
                except Exception as e:
                    fail_cnt[0] += 1
                    logger.error("%s: importEvents failed for %s: %s", log_prefix, sref, e)
                if time.monotonic() - started > 0.1:   # a main thread stall (busy spinner) of this length
                    logger.debug("%s: importEvents took %.0f ms for %s", log_prefix, (time.monotonic() - started) * 1000, sref)
                if time.monotonic() >= deadline:
                    reactor.callLater(0.01, _drain)
                    return
            # Reported after the last event, so it is the real outcome instead of the enumerate-time
            # event count, which used to log "imported" even when importEvents() itself threw.
            _logSummary()

        for item in xmltv_parser.enumFile(BytesIO(xmltv_bytes)):
            if not item:
                continue
            pending.append(item)
            evt_cnt += 1

        reactor.callFromThread(_drain)
    except Exception as e:
        logger.error("%s: EPG import error: %s", log_prefix, e)
    return True
