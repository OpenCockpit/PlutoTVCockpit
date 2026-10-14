# Copyright (C) 2026 by xcentaurix

import concurrent.futures
import os
import shutil
from itertools import count as _count

import requests
from twisted.internet import reactor
from Tools.Directories import fileExists, sanitizeFilename
from .Version import PLUGIN
from .Variables import USER_AGENT


# On this image, Python's certifi package ships a CA bundle frozen at build
# time, which can fall behind the box's own system CA bundle - the latter
# stays current via opkg's ca-certificates package, the former has no
# update path on this box at all (no pip, not opkg-tracked). Observed as
# requests.get() failing with SSLError "self-signed certificate in
# certificate chain" against a CDN signed by a root certifi's snapshot
# doesn't carry yet (e.g. Samsung TV Plus's Thawte-issued chain), even
# though the system bundle validates the exact same chain fine. requests
# reads REQUESTS_CA_BUNDLE from the environment dynamically on every
# request (not just at import time - see Session.merge_environment_settings),
# so setting it here once, at import, covers every requests.get() call in
# this module regardless of import order relative to `requests` itself.
_SYSTEM_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
if os.path.isfile(_SYSTEM_CA_BUNDLE) and not os.environ.get("REQUESTS_CA_BUNDLE"):
    os.environ["REQUESTS_CA_BUNDLE"] = _SYSTEM_CA_BUNDLE

_PICON_WIDTH = 220
_PICON_HEIGHT = 132


class PiconFetcher:
    def __init__(self, picons_config, parent=None):
        """
        picons_config  : config element for the picons setting, e.g. config.plugins.rakutentv.picons
        parent         : optional parent widget that receives updateProgressBar(counter) callbacks
        """
        self.plugin_name = PLUGIN
        self._picons_config = picons_config
        self._user_agent = USER_AGENT
        self.parent = parent
        self.piconDir = self.getPiconPath()
        self.pluginPiconDir = os.path.join(self.piconDir, self.plugin_name)
        self.resolutionStr = f"?h={_PICON_HEIGHT}&w={_PICON_WIDTH}"
        self.piconList = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def addPicon(self, ref, name, url, silent):
        """Queue a picon for download.  Call before fetchPicons()."""
        if not self._picons_config.value or not url or silent:
            return
        if self._picons_config.value == "snp" and (ch_name := sanitizeFilename(name.lower())):
            piconname = os.path.join(self.piconDir, ch_name + ".png")
        else:
            piconname = os.path.join(self.piconDir, ref.replace(":", "_") + ".png")
        if not fileExists(piconname):
            self.piconList.append((url, piconname))

    def fetchPicons(self):
        """Download all queued picons in a thread pool."""
        _PICON_WORKERS = 10
        _PICON_BATCH_TIMEOUT = 60
        self._counter = _count()
        self.createFolders()
        if self.piconList:
            with concurrent.futures.ThreadPoolExecutor(max_workers=_PICON_WORKERS) as executor:
                futures = {executor.submit(self.downloadURL, url, fn): (url, fn)
                           for url, fn in self.piconList}
                done, _ = concurrent.futures.wait(futures, timeout=_PICON_BATCH_TIMEOUT)
            print(f"[{self.plugin_name} PiconFetcher] fetched {len(done)}/{len(futures)} picons")

    def removeall(self):
        """Remove all symlinks and downloaded picons created by this plugin."""
        if os.path.exists(self.piconDir):
            for f in os.listdir(self.piconDir):
                item = os.path.join(self.piconDir, f)
                if os.path.islink(item) and self.pluginPiconDir in os.readlink(item):
                    os.remove(item)
        if os.path.exists(self.pluginPiconDir):
            shutil.rmtree(self.pluginPiconDir)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def createFolders(self):
        os.makedirs(self.piconDir, exist_ok=True)
        os.makedirs(self.pluginPiconDir, exist_ok=True)

    # Some source CDNs (e.g. Samsung TV Plus's tvpnlogopus.samsungcloud.tv)
    # ignore the ?h=&w= downscale hint below entirely and always serve the
    # full source image regardless of query string - verified by comparing
    # byte-identical responses with and without it, and against every other
    # resize convention (width=, size=, imwidth=, im=Resize,... etc: all
    # identical). Those sources can run past 1000x1000px / several hundred
    # KB per logo, so _DOWNLOAD_TIMEOUT has to budget for a full-size fetch,
    # not just a thumbnail - too tight a timeout here silently drops most of
    # a large channel list under _PICON_WORKERS-way concurrency (each
    # failed fetch is swallowed with no log line), which is what "only a
    # few picons downloaded" for Samsung TV Plus turned out to be.
    _DOWNLOAD_TIMEOUT = 8.0

    def downloadURL(self, url, piconname):
        filepath = os.path.join(self.pluginPiconDir, piconname.removeprefix(self.piconDir).removeprefix(os.sep))
        try:
            # Best-effort CDN-side downscale hint only - harmless if skipped
            # (source URL already carries a query string, e.g. a signed/
            # versioned CDN URL) or ignored by the CDN. _convertToPng below
            # always downscales locally regardless, so this is purely a
            # bandwidth/latency optimization, never a correctness requirement.
            dl_url = url if "?" in url else f"{url}{self.resolutionStr}"
            response = requests.get(dl_url, timeout=self._DOWNLOAD_TIMEOUT, headers={"User-Agent": self._user_agent})
            response.raise_for_status()
            content_type = response.headers.get('content-type', '')
            if 'image' in content_type.lower() and len(rc := response.content):
                self._convertToPng(rc, filepath, self.plugin_name)
            else:
                print(f"[{self.plugin_name} PiconFetcher] skipped {dl_url}:"
                      f" content-type={content_type!r} len={len(response.content)}")
        except requests.exceptions.RequestException as e:
            print(f"[{self.plugin_name} PiconFetcher] fetch failed {dl_url}: {e}")
        if fileExists(filepath):
            self.makesoftlink(filepath, piconname)
        # else: leave the shared "snp" filename unclaimed on a failed fetch,
        # rather than symlinking this plugin's generic default icon into it.
        # A claim here - even a fallback one - is indistinguishable from a
        # real picon to makesoftlink's cross-plugin ownership check and to
        # addPicon's dedup check, so it permanently blocks any other plugin's
        # (or this same plugin's later retry's) channel of the same
        # sanitized name from ever claiming a real picon there - this is
        # what was actually behind "PlutoTV channel X shows a default
        # SamsungTV picon" recurring across different channels: Samsung's
        # own fetch failed once, and its fallback icon then squatted on the
        # shared name forever. The Picon renderer already falls back to its
        # own default picture when no picon file is found
        # (Components/Renderer/Picon.py), so nothing is lost by leaving this
        # unclaimed - only the next fetch attempt gets a real chance to fill
        # it in properly instead of being permanently blocked.
        if self.parent:
            # Assigned here, at completion, not at task pickup: with
            # _PICON_WORKERS concurrent downloads of varying latency (network
            # fetch + decode/resize), a task picked up early can easily finish
            # late. Counting at pickup time reports values in submission
            # order while callbacks fire in completion order, so the two
            # drift apart under concurrency - visible as the progress bar
            # jumping backward whenever a slow early-picked task finally
            # finishes after later, faster ones already reported higher
            # counts. Counting here instead ties the value directly to
            # completion order, which is what the progress bar should track.
            reactor.callFromThread(self.parent.updateProgressBar, next(self._counter))

    @staticmethod
    def _convertToPng(data, filepath, tag="PiconFetcher"):
        """Decode, downscale to picon size, and save as PNG.

        Some sources (e.g. RakutenTV's channel "artwork"/"snapshot"/"poster"
        fields) are full promotional key-art rather than small icons - several
        MB at native resolution, observed as large as 2-3MB each. Enigma2's
        list rendering decodes the picon file fresh on every row repaint
        (Components/Renderer/Picon.py), so serving these at native resolution
        turns ordinary list scrolling into a multi-megabyte-per-frame
        allocation storm. Always downscale locally rather than trust the
        source to already be small or the CDN to have honored the ?h=&w=
        hint above - thumbnail() only shrinks (never enlarges), so an
        already-small image passes through unchanged.
        """
        try:
            from PIL import Image
            import io
            # Image.LANCZOS moved under Image.Resampling in newer Pillow and was
            # dropped as a top-level alias in Pillow 10+; support either.
            resample = getattr(Image, "Resampling", Image).LANCZOS
            with Image.open(io.BytesIO(data)) as img:
                img.thumbnail((_PICON_WIDTH, _PICON_HEIGHT), resample)
                img.save(filepath, "PNG")
        except Exception as e:
            print(f"[{tag} PiconFetcher] image conversion failed: {e}")

    def makesoftlink(self, filepath, softlinkpath):
        svgpath = softlinkpath.removesuffix(".png") + ".svg"
        # An SVG picon already exists - leave it alone regardless of what
        # softlinkpath itself is.
        if os.path.isfile(svgpath):
            return
        islink = os.path.islink(softlinkpath)
        if islink:
            target = os.readlink(softlinkpath)
            if target == filepath:
                return
            if self.pluginPiconDir not in target and os.path.exists(target):
                # Another plugin's channel of the same sanitized name claimed
                # this filename first - e.g. PlutoTV and SamsungTV both carry
                # the real-world "CBS News 24/7" channel and both derive the
                # same "snp" filename for it. Leave it alone rather than
                # stealing it: unconditionally overwriting here previously
                # let whichever plugin's fetch/refresh happened to run last
                # silently take over the shared filename for every other
                # plugin's same-named channel - including replacing it with
                # this plugin's own *default* icon on a failed fetch.
                #
                # But only while that target still exists: a dangling symlink
                # (its target file deleted directly, e.g. clearing another
                # plugin's picon cache without removing the shared link)
                # claims nothing anymore, so it's safe - and necessary - to
                # reclaim regardless of which plugin originally created it.
                # Otherwise it stays broken forever, since nothing else will
                # ever clean up a dangling link that isn't this plugin's own.
                return
            os.remove(softlinkpath)
        elif os.path.isfile(softlinkpath):
            # Don't touch a real file that isn't ours.
            return
        os.symlink(filepath, softlinkpath)

    @staticmethod
    def getPiconPath():
        _SYSTEM = "/usr/share/enigma2/picon"
        try:
            from Components.Renderer.Picon import lastPiconPath, searchPaths
        except ImportError:
            try:
                from Components.Renderer.Picon import piconLocator
                lastPiconPath = piconLocator.activePiconPath
                searchPaths = piconLocator.searchPaths
            except ImportError:
                lastPiconPath = None
                searchPaths = None
        for p in (searchPaths or []):
            if p != _SYSTEM and os.path.isdir(p) and os.access(p, os.W_OK):
                return p
        return lastPiconPath or _SYSTEM
