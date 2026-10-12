# Copyright (C) 2026 by xcentaurix
# License: GNU General Public License v3.0
#
#   LiveProxyMuxed - the LiveProxy counterpart for Pluto TV streams that are already muxed (video and audio in
#   the same transport stream, no #EXT-X-MEDIA:TYPE=AUDIO rendition).
#
#   LiveProxy only has to touch a stream whose audio is a separate rendition; a muxed stream used to be answered
#   with a redirect to the CDN and played by GStreamer as it is. Such a stream is made of independently encoded
#   pieces, though: at every #EXT-X-DISCONTINUITY (an ad starts or ends) the timestamps jump, the continuity
#   counters restart, PIDs and codec parameters may change, and what the decoder makes of that is up to the
#   decoder (distorted video for a few seconds on the dm9xx).
#
#   This module puts the same treatment between the CDN and Enigma2 that LiveProxy gives a demuxed stream:
#
#   - one poller thread per channel (_MuxedChannel) follows the media playlist, fetches (and decrypts) every
#     segment once and remuxes it with ffmpeg (stream copy), so the PIDs and PAT/PMT are the same wherever a
#     piece came from, an ad with a different layout included;
#   - after a discontinuity the timeline (PCR/PTS/DTS) continues where the previous segment ended instead of
#     jumping (_rewrite_timeline, the same approach as RecordingProxy), and the continuity counters are chained
#     over all segments (_patch_continuity);
#   - the result is a ring of container-continuous segments (_MuxedChannel.ready) that is
#     * served as a plain media playlist without #EXT-X-DISCONTINUITY (/mpl/{channel_id}.m3u8,
#       /mseg/{channel_id}/{seq}.ts) for live playback, and
#     * concatenated into one continuous stream by RecordingProxyMuxed for recordings, from the very same ring,
#       so a recording of the channel being watched costs no second download.
#
#   Segments are numbered by this module (never reused), like LiveProxy's chunk_seq, not by the CDN.
#
#   A stream that can't be processed (fMP4 or byte-range segments, SAMPLE-AES, nothing but errors) is flagged
#   unsupported; /auto then redirects to the CDN exactly as before.

import math
import subprocess
import threading
import time
from collections import OrderedDict, namedtuple
from typing import Optional
from urllib.parse import urljoin

from .AES128 import AES128
from .Debug import logger
from .HLSParser import HLSPlaylist
from .LiveProxy import PROXY_HOST, PROXY_PORT, _FFMPEG, _FFMPEG_TIMEOUT, _fetch, _first_pts, _patch_continuity
from .RecordingProxy import _PTS_MASK, _rewrite_timeline, _segment_is_disc

_START_BACK_SEGMENTS = 3
START_TIMEOUT = 25.0
_PLAYLIST_SEGMENTS = 6
_MAX_READY = 40
_IDLE_TIMEOUT = 60.0
_STALL_LIMIT = 90.0
_RESYNC_AFTER = 15.0
_STALL_LOG_AFTER = 20.0
_STALL_LOG_EVERY = 15.0
_SEGMENT_TRIES = 3
_SEGMENT_TIMEOUT = 15
_PLAYLIST_TIMEOUT = 8
_SEGMENT_WAIT = 8.0
_DEFAULT_SEG_DURATION = 5.0
_RESOLVE_RETRY_FAILURES = 3

Segment = namedtuple('Segment', 'seq uri duration disc key_uri iv')
MediaPlaylist = namedtuple('MediaPlaylist', 'target segments endlist unsupported')


def _variant_urls(text: str, base: str) -> 'list[str]':
    """Variant playlist urls of a master playlist, highest bandwidth first."""
    variants = []
    lines = [ln.strip() for ln in text.splitlines()]
    for i, line in enumerate(lines):
        if not line.startswith('#EXT-X-STREAM-INF:'):
            continue
        try:
            bandwidth = int(HLSPlaylist.attr_value('BANDWIDTH', line) or 0)
        except ValueError:
            bandwidth = 0
        j = i + 1
        while j < len(lines) and (not lines[j] or lines[j].startswith('#')):
            j += 1
        if j < len(lines):
            variants.append((bandwidth, urljoin(base, lines[j])))
    variants.sort(key=lambda v: v[0], reverse=True)
    return [url for _bw, url in variants]


def _parse_media(text: str, base: str) -> MediaPlaylist:
    """Segments of a media playlist; whatever this module can't remux is reported in .unsupported."""
    target = 0.0
    seq = 0
    endlist = False
    unsupported = ''
    segments = []
    disc = False
    duration = 0.0
    key_uri = None
    iv = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith('#EXT-X-TARGETDURATION:'):
            try:
                target = float(line.split(':', 1)[1])
            except ValueError:
                pass
        elif line.startswith('#EXT-X-MEDIA-SEQUENCE:'):
            try:
                seq = int(line.split(':', 1)[1])
            except ValueError:
                pass
        elif line.startswith('#EXT-X-DISCONTINUITY-SEQUENCE:'):
            pass
        elif line.startswith('#EXT-X-DISCONTINUITY'):
            disc = True
        elif line.startswith('#EXT-X-ENDLIST'):
            endlist = True
        elif line.startswith('#EXT-X-MAP'):
            unsupported = 'fMP4 segments'
        elif line.startswith('#EXT-X-BYTERANGE'):
            unsupported = 'byte-range segments'
        elif line.startswith('#EXT-X-KEY:'):
            method = (HLSPlaylist.attr_value('METHOD', line) or 'NONE').upper()
            if method == 'NONE':
                key_uri = None
                iv = None
            elif method == 'AES-128' and HLSPlaylist.attr_value('URI', line):
                key_uri = urljoin(base, HLSPlaylist.attr_value('URI', line))
                iv_str = HLSPlaylist.attr_value('IV', line)
                iv = bytes.fromhex(iv_str[2:].zfill(32)) if iv_str and iv_str[:2].lower() == '0x' else None
            else:
                unsupported = f'encryption {method}'
        elif line.startswith('#EXTINF:'):
            try:
                duration = float(line.split(':', 1)[1].split(',', 1)[0])
            except ValueError:
                duration = 0.0
        elif not line.startswith('#'):
            segments.append(Segment(seq, urljoin(base, line), duration, disc, key_uri, iv))
            seq += 1
            disc = False
            duration = 0.0
    return MediaPlaylist(target, segments, endlist, unsupported)


_AUD_ALL_TYPES = 0xF0
_AUD_I_ONLY = 0x10
_AUD_SCAN_LIMIT = 16384


def _find_slice_type(buf: bytearray, start: int) -> int:
    """NAL unit type (1 = non-IDR, 5 = IDR) of the first slice NAL at or after *start*, 0 if there is none (yet)."""
    pos = start
    while True:
        pos = buf.find(b'\x00\x00\x01', pos)
        if pos < 0 or pos + 3 >= len(buf):
            return 0
        nal_type = buf[pos + 3] & 0x1F
        if nal_type in {1, 5}:
            return nal_type
        pos += 3


def _mark_idr_delimiters(ts: bytes) -> bytes:
    """Give the access unit delimiter of every IDR frame the picture type 'I only'.

    ffmpeg's mpegts muxer inserts a delimiter (00 00 00 01 09 F0: any slice type) into every H.264 access unit of
    a stream that has none. Enigma2's recorder stores that byte in the .sc file next to every frame, and its frame
    skipping (eDVBTSTools::findFrame: fast forward/rewind, jumps) only accepts a frame as I-frame if it says "I
    only". With F0 on every frame it never finds one, falls back to byte skipping (an offset estimated from the
    average bitrate) and lands somewhere else in a stream whose bitrate varies, ads included.

    The type is changed in place (the NAL keeps its size). An IDR frame consists of I slices only, so 'I only' is
    true for it; every other frame keeps 'any slice type'. Non-H.264 video is left alone.
    """
    buf = bytearray(ts)
    vpid = None
    aud_pos = None
    au_data = bytearray()
    for off in range(0, len(buf) - 187, 188):
        if buf[off] != 0x47:
            continue
        afc = (buf[off + 3] >> 4) & 3
        if not afc & 1:
            continue
        pid = ((buf[off + 1] & 0x1F) << 8) | buf[off + 2]
        pl = off + 4 + (1 + buf[off + 4] if afc & 2 else 0)
        if pl >= off + 188:
            continue
        if buf[off + 1] & 0x40:
            if buf[pl:pl + 3] != b'\x00\x00\x01':
                continue
            if vpid is None and 0xE0 <= buf[pl + 3] <= 0xEF:
                vpid = pid
            if pid != vpid:
                continue
            aud_pos = None
            es = pl + 9 + buf[pl + 8]
            if buf[es:es + 4] == b'\x00\x00\x00\x01':
                nal = es + 4
            elif buf[es:es + 3] == b'\x00\x00\x01':
                nal = es + 3
            else:
                continue
            if nal + 2 > off + 188 or buf[nal] & 0x1F != 9 or buf[nal + 1] != _AUD_ALL_TYPES:
                continue
            aud_pos = nal + 1
            au_data = bytearray(buf[nal + 2:off + 188])
        elif pid == vpid and aud_pos is not None:
            au_data += buf[pl:off + 188]
        else:
            continue
        if aud_pos is not None:
            nal_type = _find_slice_type(au_data, 0)
            if nal_type == 5:
                buf[aud_pos] = _AUD_I_ONLY
                aud_pos = None
            elif nal_type == 1 or len(au_data) > _AUD_SCAN_LIMIT:
                aud_pos = None
    return bytes(buf)


class _MuxedChannel:
    """Follows one muxed channel and keeps a ring of container-continuous segments."""

    def __init__(self, channel_id: str, master_url_getter, inherit: 'Optional[_MuxedChannel]' = None):
        self.channel_id = channel_id
        self._master_url_getter = master_url_getter
        self.cond = threading.Condition()
        self.ready: 'OrderedDict[int, tuple[bytes, float]]' = OrderedDict()
        self._next_seq = 0
        self.status = ''
        self.reason = ''
        self.closed = False
        self.last_access = time.monotonic()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f'MuxedPoller-{channel_id[:8]}')
        self._keys: dict = {}
        self._cc: dict = {}
        self._delta = 0
        self._expected = None
        self._lost = False
        self._warned_ffmpeg = False
        if inherit is not None:
            self._cc = dict(inherit._cc)
            self._delta = inherit._delta
            self._expected = inherit._expected
            self._lost = True

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def touch(self):
        self.last_access = time.monotonic()

    def wait_ready(self, timeout: float) -> str:
        """Block until the first segment is ready or the stream turned out to be unusable; returns the status."""
        end = time.monotonic() + timeout
        with self.cond:
            while not self.status and not self.closed:
                left = end - time.monotonic()
                if left <= 0:
                    break
                self.cond.wait(left)
            return self.status or ('error' if self.closed else '')

    def wait_for(self, seq: int, timeout: float) -> 'tuple[int, bytes, float] | None':
        """(seq, data, duration) of segment *seq*, or of the oldest one if *seq* is gone already. Waits up to
        *timeout* seconds for a segment that isn't there yet; None if it doesn't arrive or the channel closed."""
        end = time.monotonic() + timeout
        with self.cond:
            while True:
                if self.ready:
                    first = next(iter(self.ready))
                    seq = max(seq, first)
                    if seq in self.ready:
                        data, duration = self.ready[seq]
                        return seq, data, duration
                if self.closed:
                    return None
                left = end - time.monotonic()
                if left <= 0:
                    return None
                self.cond.wait(left)

    def newest_seq(self) -> 'int | None':
        with self.cond:
            return next(reversed(self.ready)) if self.ready else None

    def playlist(self) -> 'str | None':
        """The media playlist for live playback: the newest segments, no discontinuity tags."""
        with self.cond:
            items = list(self.ready.items())[-_PLAYLIST_SEGMENTS:]
        if not items:
            return None
        target = max(1, math.ceil(max(duration for _seq, (_data, duration) in items)))
        out = ['#EXTM3U', '#EXT-X-VERSION:3', f'#EXT-X-TARGETDURATION:{target}', f'#EXT-X-MEDIA-SEQUENCE:{items[0][0]}']
        for seq, (_data, duration) in items:
            out.append(f'#EXTINF:{duration:.3f},')
            out.append(f'http://{PROXY_HOST}:{PROXY_PORT}/mseg/{self.channel_id}/{seq}.ts')
        return '\n'.join(out) + '\n'

    def _set_status(self, status: str, reason: str = ''):
        with self.cond:
            if not self.status:
                self.status = status
                self.reason = reason
            self.cond.notify_all()

    def _run(self):
        try:
            self._loop()
        except Exception as exc:  # pylint: disable=broad-except
            logger.debug('%s: muxed poller failed: %s', self.channel_id, exc)
            self._set_status('error', str(exc))
        finally:
            with self.cond:
                self.closed = True
                self.cond.notify_all()
            with _lock:
                if _channels.get(self.channel_id) is self:
                    _channels.pop(self.channel_id, None)
            logger.debug('%s: muxed poller stopped', self.channel_id)

    def _media_url(self) -> 'str | None':
        master_url = self._master_url_getter()
        text = _fetch(master_url, timeout=_PLAYLIST_TIMEOUT, tag='muxed:master') if master_url else None
        if not text or not text.lstrip().startswith('#EXTM3U'):
            return None
        if '#EXT-X-STREAM-INF' not in text:
            return master_url
        variants = _variant_urls(text, master_url)
        return variants[0] if variants else None

    def _loop(self):
        media_url = None
        for _attempt in range(3):
            media_url = self._media_url()
            if media_url:
                break
            self._stop.wait(1.0)
        if not media_url:
            self._set_status('error', 'master playlist not reachable')
            return
        next_seq = None
        last_progress = time.monotonic()
        last_stall_log = 0.0
        failures = 0
        while not self._stop.is_set():
            if time.monotonic() - self.last_access > _IDLE_TIMEOUT:
                logger.debug('%s: muxed channel idle, stopping', self.channel_id)
                return
            text = _fetch(media_url, timeout=_PLAYLIST_TIMEOUT, tag='muxed:playlist')
            if not text:
                failures += 1
                if failures >= _RESOLVE_RETRY_FAILURES:
                    media_url = self._media_url() or media_url
                    failures = 0
                if time.monotonic() - last_progress > _STALL_LIMIT:
                    logger.debug('%s: muxed playlist not reachable for %ss, giving up', self.channel_id, int(_STALL_LIMIT))
                    self._set_status('error', 'playlist not reachable')
                    return
                self._stop.wait(1.0)
                continue
            failures = 0
            media = _parse_media(text, media_url)
            if media.unsupported:
                logger.debug('%s: muxed stream not processable (%s)', self.channel_id, media.unsupported)
                self._set_status('unsupported', media.unsupported)
                return
            segments = media.segments
            if next_seq is None and segments:
                next_seq = segments[0].seq if media.endlist else segments[max(0, len(segments) - _START_BACK_SEGMENTS)].seq
            fresh = []
            if segments:
                if next_seq < segments[0].seq:
                    logger.debug('%s: fell behind the playlist, seq %s -> %s', self.channel_id, next_seq, segments[0].seq)
                    next_seq = segments[0].seq
                    self._lost = True
                fresh = [s for s in segments if s.seq >= next_seq]
            for seg in fresh:
                if self._stop.is_set():
                    return
                self._handle_segment(seg)
                next_seq = seg.seq + 1
                last_progress = time.monotonic()
            if media.endlist and (not segments or next_seq > segments[-1].seq):
                logger.debug('%s: end of muxed playlist', self.channel_id)
                return
            if not fresh:
                now = time.monotonic()
                if segments and now - last_progress > _STALL_LOG_AFTER and now - last_stall_log > _STALL_LOG_EVERY:
                    last_stall_log = now
                    logger.debug('%s: nothing new in the playlist for %ss: seq %s..%s, waiting for %s',
                                 self.channel_id, int(now - last_progress), segments[0].seq, segments[-1].seq, next_seq)
                if segments and segments[-1].seq < next_seq and now - last_progress > _RESYNC_AFTER:
                    next_seq = segments[max(0, len(segments) - _START_BACK_SEGMENTS)].seq
                    self._lost = True
                    last_progress = now
                    logger.debug('%s: media sequence went back to %s..%s, following it from %s',
                                 self.channel_id, segments[0].seq, segments[-1].seq, next_seq)
                    continue
                if now - last_progress > _STALL_LIMIT:
                    logger.debug('%s: no new muxed segment for %ss, giving up', self.channel_id, int(_STALL_LIMIT))
                    return
                self._stop.wait(max(0.5, min((media.target or _DEFAULT_SEG_DURATION) / 2.0, 3.0)))

    def _fetch_segment(self, seg: Segment) -> 'bytes | None':
        for attempt in range(_SEGMENT_TRIES):
            if self._stop.is_set():
                return None
            data = _fetch(seg.uri, binary=True, timeout=_SEGMENT_TIMEOUT, tag='muxed:segment')
            if data and seg.key_uri:
                key = self._keys.get(seg.key_uri) or AES128.fetch_key(seg.key_uri, _fetch)
                if not key:
                    data = None
                else:
                    self._keys[seg.key_uri] = key
                    data = AES128.decrypt(data, key, AES128.iv(seg.iv, seg.seq))
            if data:
                if data[0] == 0x47:
                    return data
                logger.debug('%s: segment %s is not MPEG-TS (%s bytes)', self.channel_id, seg.seq, len(data))
                return None
            logger.debug('%s: segment %s fetch %s failed', self.channel_id, seg.seq, attempt + 1)
            self._stop.wait(0.5)
        return None

    def _normalize(self, data: bytes) -> bytes:
        """Remux (stream copy) so the PIDs/PAT/PMT are the same for every segment, whatever the source layout."""
        cmd = [_FFMPEG, '-loglevel', 'error', '-probesize', '1000000', '-analyzeduration', '2000000', '-i', 'pipe:0',
               '-map', '0:v:0', '-map', '0:a:0?', '-c', 'copy', '-copyts', '-f', 'mpegts', '-pat_period', '2', 'pipe:1']
        try:
            r = subprocess.run(cmd, input=data, capture_output=True, timeout=_FFMPEG_TIMEOUT, check=False)
            if r.returncode == 0 and r.stdout:
                return r.stdout
            logger.debug('%s: ffmpeg rc=%s: %s', self.channel_id, r.returncode, r.stderr.decode(errors='replace')[-200:])
        except (OSError, subprocess.SubprocessError) as exc:
            if not self._warned_ffmpeg:
                self._warned_ffmpeg = True
                logger.debug('%s: ffmpeg failed: %s', self.channel_id, exc)
        return data

    def _continuize(self, ts: bytes, seg: Segment, disc: bool) -> bytes:
        """Keep the timeline and the continuity counters running over a discontinuity."""
        first = _first_pts(ts)
        if first is not None:
            if (disc or _segment_is_disc(ts)) and self._expected is not None:
                delta = (self._expected - first) & _PTS_MASK
                self._delta = delta - (1 << 33) if delta >= 1 << 32 else delta
            ticks = int(seg.duration * 90000) if seg.duration > 0 else int(_DEFAULT_SEG_DURATION * 90000)
            self._expected = (first + self._delta + ticks) & _PTS_MASK
            ts = _rewrite_timeline(ts, self._delta)
        ts, self._cc = _patch_continuity(ts, self._cc)
        return ts

    def _handle_segment(self, seg: Segment):
        data = self._fetch_segment(seg)
        if data is None:
            logger.debug('%s: segment %s lost', self.channel_id, seg.seq)
            self._lost = True
            return
        ts = _mark_idr_delimiters(self._normalize(data))
        disc = seg.disc or self._lost
        self._lost = False
        if disc:
            logger.debug('%s: segment %s starts after a discontinuity', self.channel_id, seg.seq)
        out = self._continuize(ts, seg, disc)
        duration = seg.duration if seg.duration > 0 else _DEFAULT_SEG_DURATION
        with self.cond:
            self.ready[self._next_seq] = (out, duration)
            self._next_seq += 1
            while len(self.ready) > _MAX_READY:
                self.ready.popitem(last=False)
            if not self.status:
                self.status = 'ok'
            self.cond.notify_all()


_lock = threading.Lock()
_channels: 'dict[str, _MuxedChannel]' = {}


def get_channel(channel_id: str) -> '_MuxedChannel | None':
    with _lock:
        channel = _channels.get(channel_id)
    if channel is not None:
        channel.touch()
    return channel


def ensure_channel(channel_id: str, master_url_getter, inherit: 'Optional[_MuxedChannel]' = None) -> _MuxedChannel:
    """The running pipeline of *channel_id*, started if there is none (idempotent, like register_channel).

    *inherit*: the pipeline this one replaces; a new one continues its timeline (see _MuxedChannel)."""
    with _lock:
        channel = _channels.get(channel_id)
        if channel is None or channel.closed:
            channel = _MuxedChannel(channel_id, master_url_getter, inherit)
            _channels[channel_id] = channel
            channel.start()
    channel.touch()
    return channel


def close_channel(channel_id: str) -> bool:
    """Stop the pipeline of one channel (the live slot moved on); the entry goes away when the poller exits."""
    with _lock:
        channel = _channels.get(channel_id)
    if channel is None:
        return False
    channel.stop()
    return True


def _redirect(handler, location: str):
    handler.send_response(302)
    handler.send_header('Location', location)
    handler.send_header('Content-Length', '0')
    handler.end_headers()


def handle_auto(handler, channel_id: str, state):
    """/auto for a channel without a separate audio rendition: start the pipeline and point the player at it, or
    at the CDN as before if the stream can't be processed."""
    channel = ensure_channel(channel_id, lambda: state.master_url)
    status = channel.wait_ready(START_TIMEOUT)
    if status == 'ok':
        logger.debug('%s: muxed stream - served through LiveProxyMuxed', channel_id)
        _redirect(handler, f'http://{PROXY_HOST}:{PROXY_PORT}/mpl/{channel_id}.m3u8')
    elif status == 'unsupported':
        logger.debug('%s: muxed stream - redirecting to CDN (%s)', channel_id, channel.reason)
        _redirect(handler, state.master_url)
    else:
        handler._send(502, b'Bad gateway', 'text/plain')


def handle_playlist(handler, channel_id: str):
    channel = get_channel(channel_id)
    text = channel.playlist() if channel is not None else None
    if text is None:
        handler._send(404, b'Unknown channel', 'text/plain')
        return
    handler._send(200, text.encode(), 'application/x-mpegURL')


def handle_segment(handler, channel_id: str, seq: int):
    channel = get_channel(channel_id)
    item = channel.wait_for(seq, _SEGMENT_WAIT) if channel is not None else None
    if item is None:
        handler._send(404, b'Segment not available', 'text/plain')
        return
    handler._send(200, item[1], 'video/MP2T')
