# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0


from datetime import datetime
from .Debug import logger


class CockpitEvent():

    def __init__(self, _session, service, _recording_start_time, service_center):
        self.service = service
        self.service_center = service_center

    def getEvent(self):
        return self.service_center.info(self.service).getEvent()

    def getInfo(self):
        return self.service_center.info(self.service)

    def getEventInfo(self):
        logger.info("...")
        info = self.service_center.info(self.service).info
        event_start_time = info.getEventStartTime()
        recording_start_time = info.getRecordingStartTime()
        event_length = info.getLength()
        if recording_start_time and event_start_time > recording_start_time:
            event_length += event_start_time - recording_start_time
        before = 0
        if event_start_time < recording_start_time:
            before = recording_start_time - event_start_time
        offset = 0
        logger.debug("before: %s, event_start_time: %s, recording_start_time: %s, length: %s", before, datetime.fromtimestamp(event_start_time), datetime.fromtimestamp(recording_start_time), event_length)
        return before, offset, event_length, event_start_time, recording_start_time
