# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0


from time import time
from .Debug import logger
from .CutListUtils import secondsToPts, ptsToSeconds
from .ServiceUtils import SID_DVB
from .RecordingUtils import isRecording
from .CockpitSmartSeek import CockpitSmartSeek
from .CockpitEvent import CockpitEvent


class CockpitSeek(CockpitSmartSeek, CockpitEvent):

    def __init__(self, session, service, event_start, recording_start_time, timeshift, service_center):
        self.service = service
        self.timeshift = timeshift
        self.path = self.service.getPath()
        CockpitSmartSeek.__init__(self, event_start, True)
        CockpitEvent.__init__(self, session, service, recording_start_time, service_center)

    def isRecording(self):
        is_recording = isRecording(self.path) or self.timeshift
        return is_recording

    def getSeek(self):
        # CockpitPlayer doesn't inherit InfoBarSeek (it has its own action map
        # and event tracking via CockpitPVRState/CockpitCueSheet), so getSeek()/
        # doSeek() are reimplemented here rather than pulling in the whole
        # InfoBarSeek mixin just for these two stateless helpers.
        service = self.session.nav.getCurrentService()
        if service is None:
            return None
        seek = service.seek()
        if seek is None or not seek.isCurrentlySeekable():
            return None
        return seek

    def doSeek(self, pts):
        seek = self.getSeek()
        if seek is not None:
            seek.seekTo(pts)

    def getLength(self):
        length = 0
        if self.service_started:
            if self.service.type == SID_DVB:
                _, _, length, _, _ = self.getEventInfo()
                length = secondsToPts(length)
            if not length:
                length = self.getSeekLength()
        logger.info("length: %ss (%s)", ptsToSeconds(length), length)
        return length

    def getSeekLength(self):
        length = 0
        seek = self.getSeek()
        if seek and self.service_started:
            seek_len = seek.getLength()
            if not seek_len[0]:
                length = seek_len[1]
        logger.info("length: %ss (%s)", ptsToSeconds(length), length)
        return length

    def getPosition(self):
        position = 0
        if self.service_started:
            before, offset, _, _, _ = self.getEventInfo()
            position = self.getSeekPosition() - secondsToPts(offset) + secondsToPts(before)
        logger.debug("position: %ss (%s)", ptsToSeconds(position), position)
        return position

    def getRecordingLength(self):
        _, _, _, _, recording_start_time = self.getEventInfo()
        length = secondsToPts(int(time()) - recording_start_time)
        logger.debug("recording_length: %ss (%s)", ptsToSeconds(length), length)
        return length

    def getRecordingPosition(self):
        position = 0
        if self.service_started:
            if self.isRecording():
                before, offset, _, _, recording_start_time = self.getEventInfo()
                position = secondsToPts(int(time()) - recording_start_time - offset + before)
        logger.debug("recording_position: %ss (%s)", ptsToSeconds(position), position)
        return position

    def getSeekPosition(self):
        position = 0
        seek = self.getSeek()
        if seek and self.service_started:
            pos = seek.getPlayPosition()
            if not pos[0] and pos[1] > 0:
                position = pos[1]
        logger.info("position: %ss (%s)", ptsToSeconds(position), position)
        return position

    def getBeforePosition(self):
        position = 0
        if self.service_started:
            before, _, _, _, _ = self.getEventInfo()
            position = secondsToPts(before)
        logger.debug("before_position: %ss (%s)", ptsToSeconds(position), position)
        return position
