# Copyright (C) 2026 by xcentaurix
# License: GNU General Public License v3.0

import re
import time
import uuid

from Components.config import config
from Plugins.SystemPlugins.StreamProxyCockpit import LiveProxyUnmuxed, StreamProxy
import requests

from .Debug import logger
from .CockpitTVDownload import FailureTracker
from .ConfigInit import pickForwardIP, TSIDS
from .Variables import STREAM_POOL_SIZE, USER_AGENT


class _PlutoSlot:
    """One virtual device with its own clientID, HTTP session and boot cache."""

    def __init__(self, index, stitcher_fallback):
        self.index = index
        self._stitcher_fallback = stitcher_fallback
        self.session = requests.Session()
        self.client_id = str(uuid.uuid4())
        self.bootCache = {}

    @staticmethod
    def _tokenExpiry(token):
        try:
            from json import loads
            from base64 import urlsafe_b64decode
            payload = token.split(".")[1]
            padding = 4 - len(payload) % 4
            if padding != 4:
                payload += "=" * padding
            return loads(urlsafe_b64decode(payload)).get("exp", 0)
        except Exception:
            return 0

    def boot(self, country=None, allow_stale=False):
        country = country or config.plugins.plutotv.country.value
        now = time.time()

        if country in self.bootCache:
            if now < self.bootCache[country]["exp"] - 60:
                return self.bootCache[country]["response"]
            if allow_stale:
                # Return stale cache immediately to avoid blocking the main thread.
                # The caller should arrange a background refresh.
                return self.bootCache[country]["response"]
        elif allow_stale:
            # Cache is completely absent (cold slot or DNS/network outage during
            # prewarm). Return empty immediately so buildStreamURL never blocks
            # the reactor on a network call. The caller's _scheduleBackgroundRefresh
            # will populate the cache asynchronously once connectivity recovers.
            return {}

        headers = {
            'authority': 'boot.pluto.tv',
            'accept': '*/*',
            'accept-language': 'en-US,en;q=0.9',
            'origin': 'https://pluto.tv',
            'referer': 'https://pluto.tv/',
            'sec-ch-ua': '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Linux"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-site',
            'user-agent': USER_AGENT,
        }

        params = {
            'appName': 'web',
            'appVersion': '8.0.0-111b2b9dc00bd0bea9030b30662159ed9e7c8bc6',
            'deviceVersion': '122.0.0',
            'deviceModel': 'web',
            'deviceMake': 'chrome',
            'deviceType': 'web',
            'clientID': self.client_id,
            'clientModelNumber': '1.0.0',
            'serverSideAds': 'false',
            'deviceDNT': 'false',
            'drmCapabilities': 'widevine:L3',
            'blockingMode': '',
        }

        if ip := pickForwardIP(country):
            headers['X-Forwarded-For'] = ip

        try:
            response = self.session.get(PlutoTVRequest.BOOT_URL, headers=headers, params=params, timeout=3)
            response.raise_for_status()
            resp = response.json()
            self.bootCache[country] = {
                "response": resp,
                "exp": self._tokenExpiry(resp.get("sessionToken", "")),
                "stitcherUrl": resp.get("servers", {}).get("stitcher", self._stitcher_fallback),
                "stitcherParams": resp.get("stitcherParams", ""),
            }
            logger.debug("Slot %s new token for %s, stitcher=%s",
                         self.index, country, self.bootCache[country]['stitcherUrl'])
            return resp
        except Exception as e:
            logger.debug("Slot %s boot error: %s", self.index, e)
            # If we have stale data, return it rather than nothing
            if country in self.bootCache:
                return self.bootCache[country]["response"]
            return {}


class PlutoTVRequest(FailureTracker):
    BASE_API = "https://api.pluto.tv"
    BOOT_URL = "https://boot.pluto.tv/v4/start"
    CHANNELS_URL = "https://service-channels.clusters.pluto.tv/v2/guide/channels"
    CATEGORIES_URL = "https://service-channels.clusters.pluto.tv/v2/guide/categories"
    TIMELINES_URL = "https://service-channels.clusters.pluto.tv/v2/guide/timelines"
    STITCHER_FALLBACK = "https://cfd-v4-service-channel-stitcher-use1-1.prd.pluto.tv"
    BASE_VOD = BASE_API + "/v3/vod/categories?includeItems=true&deviceType=web"
    SEASON_VOD = BASE_API + "/v3/vod/series/%s/seasons?includeItems=true&deviceType=web"

    # Legacy API endpoints (fallback for countries not on the new service-channels API)
    LEGACY_CHANNELS_URL = BASE_API + "/v2/channels.json"
    LEGACY_GUIDE_URL = BASE_API + "/v2/channels"

    # for URL insertion at runtime
    PLUTO_SCHEMA = "pluto%3a//"

    # JMP2 proxy URL template
    JMP2_URL_TEMPLATE = "https://jmp2.uk/plu-%s.m3u8"

    def __init__(self):
        # Slot 0 is the "primary" slot used for metadata / API calls.
        self._pool = [_PlutoSlot(i, self.STITCHER_FALLBACK) for i in range(STREAM_POOL_SIZE)]
        self._stream_index = 0
        self.requestCache = {}
        self._bootFailedUntil = {}   # (slot index, country) -> time, see buildDirectStreamURL
        self._sid = str(uuid.uuid1().hex)
        self._deviceId = str(uuid.uuid4().hex)

    # Legacy convenience properties so existing code keeps working.
    @property
    def session(self):
        return self._pool[0].session

    @property
    def bootCache(self):
        return self._pool[0].bootCache

    @bootCache.setter
    def bootCache(self, value):
        self._pool[0].bootCache = value

    def _nextStreamSlot(self):
        """Round-robin through pool slots for stream URLs - never slot 0.

        Slot 0 is reserved for metadata/API calls (see __init__'s own
        comment) - used concurrently by things like CockpitTVDownload's
        bouquet-building, which boot()s slot 0 once per configured
        country in the background (e.g. at every sessionstart). A live
        stream sharing that same slot's device identity with unrelated
        metadata traffic is exactly the collision buildStreamURL's own
        docstring says this whole pool exists to avoid ("preventing
        Pluto from killing concurrent streams") - just via a different
        path (the bouquet-builder, not another concurrent stream) that
        this round-robin previously didn't exclude. Confirmed in
        production: the very first zap after boot always landed on slot
        0 (self._stream_index starts at 0), coinciding with
        sessionstart's bouquet-builder also boot()-ing slot 0 for
        multiple countries in quick succession, right as a GStreamer
        "Invalid manifest" error hit that same live stream.
        """
        slot = self._pool[1 + (self._stream_index % (STREAM_POOL_SIZE - 1))]
        self._stream_index += 1
        return slot

    def boot(self, country=None):
        """Boot the primary (metadata) slot."""
        return self._pool[0].boot(country)

    def _authHeaders(self, country=None):
        """Build authorization headers for service-channels API."""
        country = country or config.plugins.plutotv.country.value
        token = self.boot(country).get('sessionToken', '')
        headers = {
            'authority': 'service-channels.clusters.pluto.tv',
            'accept': '*/*',
            'accept-language': 'en-US,en;q=0.9',
            'authorization': f'Bearer {token}',
            'origin': 'https://pluto.tv',
            'referer': 'https://pluto.tv/',
        }
        if ip := pickForwardIP(country):
            headers['X-Forwarded-For'] = ip
        return headers

    def buildStreamURL(self, channel_id, country=None):
        """Build authenticated stitcher stream URL.

        Uses a pool slot so each concurrent stream gets its own clientID
        device identity, preventing Pluto from killing concurrent streams.

        Uses allow_stale=True so this never blocks the main thread waiting
        for an HTTP response.  A background refresh is scheduled if the
        cached token was stale.

        If *channel_id* already has an active LiveProxyUnmuxed session (e.g. the
        live view is already watching it when instant-record resolves its
        own sref for the same channel a few seconds later), reuse that
        session's proxy URL as-is instead of minting a new pool slot.
        register_channel()'s idempotent-reuse path unconditionally
        overwrites the already-running channel's master_url/url_refresher
        with whatever this call passes in - so calling through with a new
        slot here would silently repoint an already-live channel at a
        *different* device identity's session mid-stream, with the
        prefetch threads that were already running under the old slot
        left going. Two Pluto sessions for "the same channel" aren't
        guaranteed to share an absolute PTS timeline or ad-stitching
        state, which is exactly what was corrupting instant recordings of
        an already-live channel (audio DTS glitches / occasional decoder
        frame corruption right around the slot swap, with no genuine
        #EXT-X-DISCONTINUITY tag involved).
        """
        existing_url = LiveProxyUnmuxed.active_channel_url(channel_id)
        if existing_url is not None:
            return existing_url
        country = country or config.plugins.plutotv.country.value
        if config.plugins.plutotv.live_tv_mode.value != "stitcher":
            return self._registerSourceChannel(channel_id)
        slot = self._nextStreamSlot()
        slot.boot(country, allow_stale=True)
        cache = slot.bootCache.get(country, {})
        # If this slot's token is absent or stale, schedule a background
        # refresh so the next zap has a fresh token.  The url_refresher
        # passed to register_channel handles the cold-slot case synchronously
        # from the proxy handler thread when jwt= ends up in master_url.
        now = time.time()
        if not cache or now >= cache.get("exp", 0) - 60:
            self._scheduleBackgroundRefresh(slot, country)
        token = cache.get('response', {}).get('sessionToken', '')
        stitcherUrl = cache.get('stitcherUrl', self.STITCHER_FALLBACK)
        stitcherParams = cache.get('stitcherParams', '')
        url = (
            f"{stitcherUrl}/v2/stitch/hls/channel/{channel_id}/master.m3u8"
            f"?jwt={token}&masterJWTPassthrough=true"
        )
        if stitcherParams:
            url += f"&{stitcherParams}"
        logger.debug("buildStreamURL slot %s for %s", slot.index, channel_id)
        # Route all live channels through the local HLS proxy so that demuxed
        # streams (EXT-X-MEDIA:TYPE=AUDIO with a separate audio-only TS) are
        # transparently merged into a single muxed TS.  The proxy is a no-op
        # for channels that are already muxed.  register_channel() itself is
        # idempotent for a channel_id that's already registered (see its
        # docstring), so any caller - live playback, a recording of the
        # channel currently being watched, or a recording timer re-resolving
        # the same service - can call this freely without disrupting an
        # already-running stream.
        LiveProxyUnmuxed.start()
        # Pass a refresher so the proxy handler thread (safe to block) can
        # obtain a valid JWT on demand when the channel was registered with
        # an empty token (cold-slot miss: cache absent at zap time).

        def _url_refresher(_slot=slot, _country=country, _ch=channel_id):
            _slot.boot(_country)  # blocks; safe – called from handler thread, not reactor
            _c = _slot.bootCache.get(_country, {})
            _token = _c.get('response', {}).get('sessionToken', '')
            _su = _c.get('stitcherUrl', self.STITCHER_FALLBACK)
            _sp = _c.get('stitcherParams', '')
            _u = f"{_su}/v2/stitch/hls/channel/{_ch}/master.m3u8?jwt={_token}&masterJWTPassthrough=true"
            if _sp:
                _u += f"&{_sp}"
            return _u
        return LiveProxyUnmuxed.register_channel(channel_id, url, url_refresher=_url_refresher)

    def _registerSourceChannel(self, channel_id):
        """The address of the stream proxy for a channel of the source JMP2 (setting "Stream source"): the proxy
        fetches the address of the redirect like it does the stitcher's. Nothing is fetched here, this runs on the main
        thread."""
        LiveProxyUnmuxed.start()
        return LiveProxyUnmuxed.register_channel(channel_id, self.JMP2_URL_TEMPLATE % channel_id)

    def buildDirectStreamURL(self, channel_id, country=None):
        """The stitcher address of a channel with a token of its own, for a bouquet that doesn't go through the stream
        proxy (the proxy otherwise looks it up when the channel is played). "" if there is no token.

        Like buildStreamURL, a pool slot is used (never the metadata slot), round-robin: the channels of a bouquet
        share a few device identities. Blocks on the network the first time per slot and country (the token is cached
        for its life): for the download thread, not for the main thread."""
        country = country or config.plugins.plutotv.country.value
        slot = self._nextStreamSlot()
        key = (slot.index, country)
        if time.time() < self._bootFailedUntil.get(key, 0):
            return ""   # it just failed: not once more for every channel of the bouquet (it takes the timeout each)
        slot.boot(country)
        cache = slot.bootCache.get(country, {})
        token = cache.get('response', {}).get('sessionToken', '')
        if not token:
            self._bootFailedUntil[key] = time.time() + 120
            return ""
        url = (
            f"{cache.get('stitcherUrl', self.STITCHER_FALLBACK)}/v2/stitch/hls/channel/{channel_id}/master.m3u8"
            f"?jwt={token}&masterJWTPassthrough=true"
        )
        if params := cache.get('stitcherParams', ''):
            url += f"&{params}"
        return url

    @staticmethod
    def _scheduleBackgroundRefresh(slot, country):
        """Refresh *slot*'s boot token for *country* in a background thread."""
        try:
            from twisted.internet import threads
            threads.deferToThread(slot.boot, country)
        except Exception:
            pass

    def _apiHeaders(self, country=None):
        """Build authorization headers for api.pluto.tv endpoints (VOD)."""
        country = country or config.plugins.plutotv.country.value
        token = self.boot(country).get('sessionToken', '')
        headers = {
            'accept': 'application/json, text/javascript, */*; q=0.01',
            'authorization': f'Bearer {token}',
            'origin': 'https://pluto.tv',
            'referer': 'https://pluto.tv/',
            'user-agent': USER_AGENT,
        }
        if ip := pickForwardIP(country):
            headers['X-Forwarded-For'] = ip
        return headers

    def _legacyHeaders(self, country=None):
        """Build headers for legacy api.pluto.tv endpoints (no Bearer token needed)."""
        headers = {
            'accept': 'application/json, text/javascript, */*; q=0.01',
            'host': 'api.pluto.tv',
            'connection': 'keep-alive',
            'referer': 'http://pluto.tv/',
            'origin': 'http://pluto.tv',
            'user-agent': USER_AGENT,
        }
        if ip := pickForwardIP(country or config.plugins.plutotv.country.value):
            headers['X-Forwarded-For'] = ip
        return headers

    def getURL(self, url, param=None, header=None, life=60 * 15, country=None):
        if header is None:
            header = {"User-agent": USER_AGENT}
        if param is None:
            param = {}
        now = time.time()
        if (country := country or config.plugins.plutotv.country.value) not in self.requestCache:
            self.requestCache[country] = {}
        if url in self.requestCache[country] and self.requestCache[country][url][1] > (now - life):
            return self.requestCache[country][url][0]
        try:
            import threading
            if threading.current_thread() is threading.main_thread():
                import traceback
                logger.debug('WARNING: getURL(%r) called on main thread!', url)
                traceback.print_stack()
            req = requests.get(url, param, headers=header, timeout=10)
            req.raise_for_status()
            response = req.json()
            req.close()
            self.requestCache[country][url] = (response, now)
            return response
        except Exception:
            return {}

    def buildVodStreamURL(self, vod_url, country=None):
        """Rewrite a VOD stitched URL to use the correct stitcher host + JWT auth,
        then register it with LiveProxyUnmuxed and return the local proxy URL.

        Uses a pool slot so each concurrent stream gets its own clientID
        device identity, preventing Pluto from killing concurrent streams.
        """
        country = country or config.plugins.plutotv.country.value
        slot = self._nextStreamSlot()
        slot.boot(country)
        cache = slot.bootCache.get(country, {})
        token = cache.get('response', {}).get('sessionToken', '')
        stitcherUrl = cache.get('stitcherUrl', self.STITCHER_FALLBACK)
        stitcherParams = cache.get('stitcherParams', '')

        # Extract the path from the old URL (strip host and query string)
        # e.g. https://service-stitcher-ipv4.clusters.pluto.tv/stitch/hls/episode/XXX/master.m3u8?...
        path = vod_url.split('?')[0]  # remove query string
        path = re.sub(r'^https?://[^/]+', '', path)  # remove scheme+host, keep /path

        # Ensure /v2 prefix (old URLs use /stitch/..., new stitcher needs /v2/stitch/...)
        if path.startswith('/stitch/'):
            path = '/v2' + path

        url = (
            f"{stitcherUrl}{path}"
            f"?jwt={token}&masterJWTPassthrough=true"
        )
        if stitcherParams:
            url += f"&{stitcherParams}"

        # Route through the local HLS proxy, same as buildStreamURL does for
        # live channels: Pluto's VOD stitcher uses the same demuxed-audio
        # delivery as live, and playing that raw stream directly hangs
        # gstreamer solid at the first ad transition (confirmed in an
        # Enigma2 debug log). LiveProxyUnmuxed's own /auto/ handler auto-detects
        # and no-ops for content that turns out to already be muxed.
        #
        # Unique channel_id per call (not just per VOD asset): a live
        # channel_id is meant to be reused (a recording and a live viewer
        # share one ongoing feed), but nothing ever closes a VOD channel's
        # state between separate playback attempts, and the idle reaper
        # only fires after 60s - so a quick replay within that window would
        # otherwise resume the previous attempt's state instead of starting
        # clean. The episode id is kept as a log-readable prefix; the
        # reaper cleans up each attempt's entry the same way it does for
        # every other channel.
        match = re.search(r'/([^/]+)/master\.m3u8', path)
        prefix = f'vod_{match.group(1)}' if match else 'vod'
        channel_id = f'{prefix}_{uuid.uuid4().hex[:8]}'
        # registered through StreamProxy, so the live slot closes the channel again when it moves on
        return StreamProxy.registerChannel("pluto", channel_id, url)

    def getVOD(self, epid, country=None):
        country = country or config.plugins.plutotv.country.value
        return self.getURL(self.SEASON_VOD % epid, header=self._apiHeaders(country), life=60 * 60, country=country)

    def getOndemand(self, country=None):
        country = country or config.plugins.plutotv.country.value
        return self.getURL(self.BASE_VOD, header=self._apiHeaders(country), life=60 * 60, country=country)

    def getChannels(self, country=None):
        """The channels of *country*; if there are none, failure(country) tells if a request failed."""
        country = country or config.plugins.plutotv.country.value
        self.clearFailure(country)
        channels = self._getChannels(country)
        if channels:
            self.clearFailure(country)   # the legacy API did answer, whatever the new one did
        return channels

    def _getChannels(self, country):
        """Fetch channels via v2/guide/channels + categories, returned in legacy format.

        Falls back to the legacy api.pluto.tv endpoint if the new API returns
        no data (some countries like Finland are not on the new API).
        """
        country = country or config.plugins.plutotv.country.value
        headers = self._authHeaders(country)
        params = {'channelIds': '', 'offset': '0', 'limit': '1000', 'sort': 'number:asc'}

        try:
            response = self.session.get(self.CHANNELS_URL, params=params, headers=headers, timeout=10)
            response.raise_for_status()
            channel_list = response.json().get("data", [])
        except Exception as e:
            logger.debug("getChannels new API error for %s: %s", country, e)
            self.recordFailure(country, e)
            channel_list = []

        if not channel_list:
            logger.debug("getChannels: new API returned no channels for %s, trying legacy API", country)
            return self._getChannelsLegacy(country)

        try:
            response = self.session.get(self.CATEGORIES_URL, params=params, headers=headers, timeout=10)
            response.raise_for_status()
            cat_data = response.json().get("data", [])
        except Exception:
            cat_data = []

        categories = {}
        for elem in cat_data:
            cat_name = elem.get('name', '')
            for ch_id in elem.get('channelIDs', []):
                categories[ch_id] = cat_name

        # The new categories endpoint leaves out some categories (seasonal ones such
        # as House of Horror, whose channels then belong to none). The legacy channel
        # list still carries them, so take the category of every uncategorised channel
        # from there.
        if any(ch.get('id') not in categories for ch in channel_list):
            legacy = self._getLegacyCategories(country)
            for ch in channel_list:
                ch_id = ch.get('id', '')
                if ch_id not in categories and legacy.get(ch_id):
                    categories[ch_id] = legacy[ch_id]

        result = []
        for ch in channel_list:
            ch_id = ch.get('id', '')
            logo_url = next(
                (img["url"] for img in ch.get("images", []) if img.get("type") == "colorLogoPNG"),
                None
            )
            result.append({
                '_id': ch_id,
                'name': ch.get('name', ''),
                'slug': ch.get('slug', ''),
                'number': ch.get('number', 0),
                'category': categories.get(ch_id, ''),
                'colorLogoPNG': {'path': logo_url},
            })

        return result

    def _getLegacyCategories(self, country):
        """Channel id -> category name from the legacy api.pluto.tv channel list."""
        params = {'sid': self._sid, 'deviceId': self._deviceId}
        try:
            response = requests.get(self.LEGACY_CHANNELS_URL, params=params, headers=self._legacyHeaders(country), timeout=10)
            response.raise_for_status()
            channels = response.json()
            return {ch.get('_id', ''): ch.get('category', '') for ch in channels} if isinstance(channels, list) else {}
        except Exception as e:
            logger.debug('getChannels legacy category lookup error for %s: %s', country, e)
            return {}

    def _getChannelsLegacy(self, country):
        """Fetch channels via the legacy api.pluto.tv/v2/channels.json endpoint."""
        params = {'sid': self._sid, 'deviceId': self._deviceId}
        headers = self._legacyHeaders(country)
        try:
            response = requests.get(self.LEGACY_CHANNELS_URL, params=params, headers=headers, timeout=10)
            response.raise_for_status()
            channels = response.json()
            if isinstance(channels, list):
                logger.debug("getChannels legacy API returned %s channels for %s", len(channels), country)
                return channels
            logger.debug("getChannels legacy API unexpected response for %s", country)
            return []
        except Exception as e:
            logger.debug("getChannels legacy API error for %s: %s", country, e)
            self.recordFailure(country, e)
            return []

    def getBaseGuide(self, start, stop, country=None):
        """Fetch guide data via v2/guide/timelines, returned in legacy format.

        Falls back to the legacy api.pluto.tv endpoint if the new API returns
        no data (some countries like Finland are not on the new API).
        """
        country = country or config.plugins.plutotv.country.value
        headers = self._authHeaders(country)

        if not (channels := self.getChannels(country)):
            return []

        channel_ids = [ch['_id'] for ch in channels]
        channel_lookup = {ch['_id']: ch for ch in channels}

        all_entries = []
        group_size = 100
        for i in range(0, len(channel_ids), group_size):
            group = channel_ids[i:i + group_size]
            params = {
                'start': start,
                'channelIds': ','.join(group),
                'duration': '1440',
            }
            try:
                response = self.session.get(self.TIMELINES_URL, params=params, headers=headers, timeout=10)
                response.raise_for_status()
                data = response.json().get("data", [])
                for entry in data:
                    ch_id = entry.get('channelId', '')
                    ch_data = channel_lookup.get(ch_id, {})
                    all_entries.append({
                        '_id': ch_id,
                        'number': ch_data.get('number', 0),
                        'name': ch_data.get('name', ''),
                        'timelines': entry.get('timelines', []),
                    })
            except Exception as e:
                logger.debug("getBaseGuide new API error for %s: %s", country, e)

        if not all_entries:
            logger.debug("getBaseGuide: new API returned no data for %s, trying legacy API", country)
            return self._getBaseGuideLegacy(start, stop, country)

        return all_entries

    def _getBaseGuideLegacy(self, start, stop, country):
        """Fetch guide data via the legacy api.pluto.tv/v2/channels endpoint."""
        params = {'start': start, 'stop': stop, 'sid': self._sid, 'deviceId': self._deviceId}
        headers = self._legacyHeaders(country)
        try:
            response = requests.get(self.LEGACY_GUIDE_URL, params=params, headers=headers, timeout=10)
            response.raise_for_status()
            guide = response.json()
            if isinstance(guide, list):
                logger.debug("getBaseGuide legacy API returned %s entries for %s", len(guide), country)
                return guide
            logger.debug("getBaseGuide legacy API unexpected response for %s", country)
            return []
        except Exception as e:
            logger.debug("getBaseGuide legacy API error for %s: %s", country, e)
            return []


plutoRequest = PlutoTVRequest()


def startProactiveRefresh():
    """Periodically refresh boot tokens before they expire so that
    synchronous callers (service extensions) never have to block."""
    from twisted.internet import reactor, threads
    now = time.time()
    for slot in plutoRequest._pool:
        for country, cache in list(slot.bootCache.items()):
            if now >= cache.get("exp", 0) - 300:  # refresh 5 min before expiry
                threads.deferToThread(slot.boot, country)
    reactor.callLater(120, startProactiveRefresh)


def resolveStream(channel_id, sref):
    """StreamProxy provider: the address of the stream proxy for the Pluto channel *channel_id*.

    *sref* is the service reference of the bouquet entry (or of the instant recording of the playing service); its
    TSID tells the country the channel was listed for. The service extensions that call this are in
    StreamProxyCockpit (StreamProxy), registered in plugin.py."""
    cc = {v: k for k, v in TSIDS.items()}.get(sref.toString().split(":")[4], None)
    return plutoRequest.buildStreamURL(channel_id, cc)
