# Copyright (C) 2026 by xcentaurix
# License: GNU General Public License v3.0
#
#   RecordingProxyMuxed - serves a single, container-continuous MPEG-TS stream for recording a Pluto TV channel
#   that is already muxed (video and audio in the same transport stream), the RecordingProxy counterpart for a
#   channel LiveProxy doesn't have to mux itself.
#
#   Enigma2's recorder (eServiceMP3Record: uridecodebin straight into a filesink) writes whatever the HLS source
#   delivers to the file, so the recording of a stream with ad breaks (#EXT-X-DISCONTINUITY) ends or becomes
#   unplayable at the first break: the timestamps jump, the continuity counters restart, PIDs and codec
#   parameters may change.
#
#   LiveProxyMuxed already turns the CDN's segments into a ring of container-continuous ones (same PIDs and
#   PAT/PMT, the timeline continued over discontinuities, continuity counters chained) for live playback. This
#   module concatenates them, in order, from the live edge on, into the response of /rec/{channel_id}.ts - like
#   RecordingProxy does with LiveProxy's muxed segments. A channel being watched and recorded at the same time
#   shares one pipeline.
#
#   A stream LiveProxyMuxed can't process (fMP4 or byte-range segments, SAMPLE-AES, ...) is answered with a
#   redirect to the master playlist, so the recorder does exactly what it did before.

import time

from .Debug import logger
from . import LiveProxyMuxed

_READ_WAIT = 1.0


def _segments(channel, state):
    """Yield the channel's segments in order, starting with the newest one (the live edge)."""
    seq = None
    while True:
        state.last_access = time.monotonic()
        channel.touch()
        if seq is None:
            newest = channel.newest_seq()
            if newest is None:
                if channel.closed:
                    return
                time.sleep(0.1)
                continue
            seq = newest
        item = channel.wait_for(seq, _READ_WAIT)
        if item is None:
            if channel.closed:
                return
            continue
        seq, data, _duration = item
        yield data
        seq += 1


def handle_recording(handler, channel_id, state):
    """Serve /rec/{channel_id}.ts for a muxed channel (LiveProxy.RecordingProxy hands those over)."""
    channel = LiveProxyMuxed.ensure_channel(channel_id, lambda: state.master_url)
    status = channel.wait_ready(LiveProxyMuxed.START_TIMEOUT)
    if status == 'unsupported':
        logger.debug('%s: muxed recording - redirecting to CDN (%s)', channel_id, channel.reason)
        handler.send_response(302)
        handler.send_header('Location', state.master_url)
        handler.send_header('Content-Length', '0')
        handler.end_headers()
        return
    if status != 'ok':
        logger.debug('%s: muxed recording - stream not available (%s)', channel_id, channel.reason or status)
        handler.send_response(502)
        handler.end_headers()
        return
    logger.debug('%s: muxed stream - recording through RecordingProxyMuxed', channel_id)
    handler.send_response(200)
    handler.send_header('Content-Type', 'video/MP2T')
    handler.end_headers()
    for chunk in _segments(channel, state):
        handler.wfile.write(chunk)
