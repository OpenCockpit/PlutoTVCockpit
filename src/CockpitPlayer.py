# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0


import os
from enigma import iPlayableService
from Components.ActionMap import HelpableActionMap
from Components.Pixmap import Pixmap
from Components.config import config
from Components.ServiceEventTracker import InfoBarBase, ServiceEventTracker
from Components.Sources.COCCurrentService import COCCurrentService
from Screens.Screen import Screen, ScreenSummary
from Screens.HelpMenu import HelpableScreen
from Screens.MessageBox import MessageBox
from Screens.InfoBarGenerics import InfoBarAudioSelection, InfoBarShowHide, InfoBarNotifications, Notifications, InfoBarSubtitleSupport
from Tools.LoadPixmap import LoadPixmap
from .Debug import logger
from .__init__ import _
from .CutList import CutList
from .CutListUtils import ptsToSeconds, getCutListLast, getCutListFirst
from .CockpitCueSheet import CockpitCueSheet
from .CockpitPVRState import CockpitPVRState
from .CockpitSeek import CockpitSeek
from .DelayTimer import DelayTimer
from .SkinUtils import getSkinPath


class CockpitPlayerSummary(ScreenSummary):

    def __init__(self, session, parent):
        ScreenSummary.__init__(self, session, parent=parent)


class CockpitPlayer(
        Screen, HelpableScreen, InfoBarBase, InfoBarNotifications, InfoBarShowHide, InfoBarAudioSelection, InfoBarSubtitleSupport,
        CockpitCueSheet, CockpitSeek, CockpitPVRState, CutList):

    ENABLE_RESUME_SUPPORT = False
    ALLOW_SUSPEND = True

    def __init__(self, session, service, config_plugins_plugin, showMovieInfoEPGPtr=None, leave_on_eof=False, recording_start_time=0, timeshift=None, service_center=None, stream=False):
        self.service = service
        self.service_ext = os.path.splitext(self.service.getPath())[1]
        self.config_plugins_plugin = config_plugins_plugin
        self.showMovieInfoEPGPtr = showMovieInfoEPGPtr
        self.leave_on_eof = leave_on_eof
        self.stream = stream

        self.allowPiP = False
        self.allowPiPSwap = False

        Screen.__init__(self, session)
        HelpableScreen.__init__(self)
        self["Service"] = COCCurrentService(session.nav, self)

        InfoBarShowHide.__init__(self)
        InfoBarBase.__init__(self)
        InfoBarAudioSelection.__init__(self)
        InfoBarNotifications.__init__(self)
        InfoBarSubtitleSupport.__init__(self)
        CockpitCueSheet.__init__(self, service)
        CutList.__init__(self)

        self["player_icon"] = Pixmap()

        self["actions"] = HelpableActionMap(
            self,
            "CockpitActions",
            {
                "OK": (self.ok, _("Infobar/Play")),
                "PLAY": (self.playpause, _("Play/Pause")),
                "STOP": (self.leavePlayer, _("Stop playback")),
                "EXIT": (self.leavePlayer, _("Stop playback")),
                "POWER": (self.leavePlayer, _("Stop playback")),
                "CHANNELUP": (self.skipForward, _("Skip forward")),
                "CHANNELDOWN": (self.skipBackward, _("Skip backward")),
                "INFOS": (self.showMovieInfoEPG, _("EPG Info")),
            },
            -2
        )

        self._event_tracker = ServiceEventTracker(
            screen=self,
            eventmap={
                iPlayableService.evStart: self.__serviceStarted,
            }
        )

        event_start = config_plugins_plugin.movie_start_position.value == "event_start"
        CockpitSeek.__init__(self, session, service, event_start,
                             recording_start_time, timeshift, service_center)
        CockpitPVRState.__init__(self)

        self.service_started = False
        self.cut_list = []
        self.mtc_resume_point = 0

        self.onShown.append(self.__onShown)

    def createSummary(self):
        return CockpitPlayerSummary

    def __onShown(self):
        logger.info("...")
        if self["player_icon"].instance:
            player_icon = "streamer.svg" if self.stream else "player.svg"
            self["player_icon"].instance.setPixmap(LoadPixmap(getSkinPath(
                "images/" + player_icon), cached=True, width=60, height=60, scaletoFit=1))
        if not self.service_started:
            self.session.nav.playService(self.service)

    def __serviceStarted(self):
        logger.info("self.is_closing: %s", self.is_closing)
        if not self.service_started and not self.is_closing:
            self.service_started = True
            DelayTimer(10, self.delayedServiceStarted)

    def delayedServiceStarted(self):
        logger.info("...")
        if hasattr(self, "config_plugins_plugin"):
            self.downloadCuesheet()
            if self.config_plugins_plugin.movie_resume_at_last_pos.value:
                self.mtc_resume_point = getCutListLast(self.cut_list)
                if self.mtc_resume_point > 0:
                    seconds = ptsToSeconds(self.mtc_resume_point)
                    logger.debug("mtc_resume_point: %s", seconds)
                    Notifications.AddNotificationWithCallback(
                        self.__serviceStartedCallback,
                        MessageBox,
                        _("Do you want to resume playback at position: %d:%02d:%02d?")
                        % (seconds // 3600, seconds % 3600 // 60, seconds % 60),
                        timeout=10,
                        type=MessageBox.TYPE_YESNO,
                        default=False,
                    )
                else:
                    self.__serviceStartedCallback(False)
            else:
                self.__serviceStartedCallback(False)

    def __serviceStartedCallback(self, answer):
        logger.info("answer: %s", answer)
        if answer and self.mtc_resume_point:
            logger.debug("Seeking to mtc_resume_point: %s", self.mtc_resume_point)
            self.doSeek(int(self.mtc_resume_point))
        elif self.config_plugins_plugin.movie_start_position.value == "first_mark":
            first_mark = getCutListFirst(self.cut_list, config.recording.margin_before.value * 60)
            if first_mark:
                logger.debug("Seeking to first_mark: %s", first_mark)
                self.doSeek(int(first_mark))
        elif self.config_plugins_plugin.movie_start_position.value == "event_start":
            self.skipToEventStart()

    def ok(self):
        if self.seekstate == self.SEEK_STATE_PLAY:
            self.toggleShow()
        else:
            self.playpause()

    def playpause(self):
        self.playpauseService()

    def showMovieInfoEPG(self):
        if self.showMovieInfoEPGPtr:
            self.showMovieInfoEPGPtr()

    def showPVRStatePic(self, show):
        self.show_state_pic = show

    def leavePlayer(self):
        logger.info("last: %s", self.getPosition())
        self.updateCutList(self.service.getPath(), last=self.getPosition())
        self.session.nav.stopService()
        self.close()

    def doEofInternal(self, playing):
        logger.info("playing: %s, self.execing: %s", playing, self.execing)
        if self.execing:
            self.is_closing = True
            if self.leave_on_eof:
                self.leavePlayer()

    def showMovies(self):
        logger.info("...")
