# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0


from Components.ActionMap import HelpableActionMap
from Screens.InfoBarGenerics import InfoBarSeek
from enigma import eTimer
from .__init__ import _
from .Debug import logger
from .CutListUtils import secondsToPts, ptsToSeconds


SKIP_TIMEOUT = 5000  # milliseconds
STOP_BEFORE_EOF = 5  # seconds


class CockpitSmartSeek(InfoBarSeek):

    def __init__(self, event_start, config_skip_first_long):
        logger.info("...")
        InfoBarSeek.__init__(self)

        self["InfoBarSmartSeek"] = HelpableActionMap(
            self,
            "InfoBarSmartSeekActions",
            {
                "CHANNELUP": (self.skipForward, _("Skip forward")),
                "CHANNELDOWN": (self.skipBackward, _("Skip backward")),
            },
            prio=-1
        )

        self.event_start = event_start
        self.skip_first = True
        self.config_skip_first_long = config_skip_first_long
        self.skip_forward = True
        self.skip_index = 0
        self.skip_distance_long = [300, 60, 30, 15]
        self.skip_distance_short = [60, 30, 15]
        self.skip_distance = self.skip_distance_long
        self.reset_skip_timer = eTimer()
        self.reset_skip_timer.callback.append(self.resetSkipTimer)

    def skipToEventStart(self):
        logger.info("...")
        _, _, _, event_start_time, recording_start_time = self.getEventInfo()
        skip_distance = event_start_time - recording_start_time
        if skip_distance > 0:
            self.doSkip(skip_distance)

    def resetSkipTimer(self):
        logger.info("...")
        self.skip_first = True
        self.skip_distance = self.skip_distance_long
        self.skip_index = 0
        self.skip_forward = True
        self.showPVRStatePic(True)

    def setSkipDistance(self):
        if self.skip_first and self.event_start:
            _, _, _, event_start_time, recording_start_time = self.getEventInfo()
            logger.debug("position: %s, event_start_time: %s", ptsToSeconds(self.getPosition()), event_start_time)
            if abs(event_start_time - recording_start_time - ptsToSeconds(self.getSeekPosition())) <= 60:
                self.skip_distance = self.skip_distance_short
            else:
                self.skip_distance = self.skip_distance_long
            logger.debug("skip_distance: %s", self.skip_distance)

    def skipForward(self):
        logger.info("...")
        self.reset_skip_timer.start(SKIP_TIMEOUT, True)
        self.setSkipDistance()
        if not self.skip_first and (not self.skip_forward or (self.config_skip_first_long and self.skip_distance == self.skip_distance_long and self.skip_index == 0)):
            self.skip_index = len(self.skip_distance) - 1 if self.skip_index >= len(self.skip_distance) - 1 else self.skip_index + 1
        self.skip_forward = True
        self.skip_first = False
        distance = self.skip_distance[self.skip_index]
        position = ptsToSeconds(self.getSeekPosition())
        self.showPVRStatePic(False)
        self.doSkip(position + distance)

    def skipBackward(self):
        logger.info("...")
        self.reset_skip_timer.start(SKIP_TIMEOUT, True)
        self.setSkipDistance()
        if not self.skip_first and self.skip_forward:
            self.skip_index = len(self.skip_distance) - 1 if self.skip_index >= len(self.skip_distance) - 1 else self.skip_index + 1
        self.skip_forward = False
        self.skip_first = False
        distance = self.skip_distance[self.skip_index]
        position = ptsToSeconds(self.getSeekPosition())
        distance = min(distance, position)
        self.doSkip(position - distance)

    def doSkip(self, target):
        length = ptsToSeconds(self.getSeekLength())
        target = min(target, length - STOP_BEFORE_EOF)
        target = max(0, target)
        logger.info("target: %s, length: %s", target, length)
        if length:
            self.doSeek(secondsToPts(target))
        self.showAfterSeek()
