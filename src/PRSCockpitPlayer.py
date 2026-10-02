# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0

"""CockpitPlayer shared by the FAST-channel TV Cockpit plugins (Pluto TV,
Rakuten TV, Samsung TV Plus).

The length comes from the plugin's own listing (see PRSServiceCenter) instead
of the stream, and resume points are kept in the plugin's ResumePoints rather
than in a .cuts file next to the (non-existent) local file.

VOD (PlutoTVCockpit) - the movie infobar, position/length of the stream:

    self.session.openWithCallback(self._playerClosed, PRSCockpitPlayer, reference, sid,
                                  resumePointsInstance, self.service_center, config.plugins.plutotv)

Live channels (Rakuten TV, Samsung TV Plus), live=True - the infobar shows the
current EPG event of the channel (name, progress, elapsed/remaining, end time)
through the event converters on session.Event_Now (see the CockpitLivePlayer
and CockpitLivePlayerSummary skin screens), so the channel must be played with
the service reference its bouquet/EPG import uses (see PRSServiceRef):

    self.session.openWithCallback(self._playerClosed, PRSCockpitPlayer, reference, sid,
                                  resumePointsInstance, self.service_center, config.plugins.rakutentv, live=True)

*config_plugins_plugin* is the plugin's own config subsection; it must define
movie_resume_at_last_pos and movie_start_position (read by CockpitPlayer).
"""
from Components.config import config
from Screens.MessageBox import MessageBox
from Tools import Notifications

from Screens.Screen import ScreenSummary

from . import _
from .Debug import logger
from .CockpitPlayer import CockpitPlayer
from .CutListUtils import secondsToPts, ptsToSeconds


RESUME_MIN_PTS = 900000


class CockpitLivePlayerSummary(ScreenSummary):

    def __init__(self, session, parent):
        ScreenSummary.__init__(self, session, parent=parent)


class PRSCockpitPlayer(CockpitPlayer):

    def __init__(self, session, service, sid, resume_points, service_center, config_plugins_plugin, live=False):
        self.prs_live = live
        self.prs_sid = sid
        self.prs_resume_points = resume_points
        self.prs_resume_point = 0
        CockpitPlayer.__init__(self, session, service, config_plugins_plugin, leave_on_eof=True, service_center=service_center, stream=True)
        self.skinName = "CockpitLivePlayer" if live else "CockpitPlayer"

    def createSummary(self):
        return CockpitLivePlayerSummary if self.prs_live else CockpitPlayer.createSummary(self)

    def getLength(self):
        length = 0
        if self.service_started:
            length = secondsToPts(self.getEventInfo()[2])
        return length or CockpitPlayer.getLength(self)

    def delayedServiceStarted(self):
        logger.info("...")
        if not self.config_plugins_plugin.movie_resume_at_last_pos.value:
            return
        last, _stored_length = self.prs_resume_points.getResumePoint(self.prs_sid)
        if last is None:
            return
        length = self.getLength()
        if last > RESUME_MIN_PTS and (not length or last < length - RESUME_MIN_PTS):
            self.prs_resume_point = last
            seconds = int(ptsToSeconds(last))
            Notifications.AddNotificationWithCallback(
                self.resumeCallback,
                MessageBox,
                _("Do you want to resume this playback?") + "\n" + (_("Resume position at %s") % f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"),
                timeout=10,
                default="yes" in config.usage.on_movie_start.value,
            )

    def resumeCallback(self, answer):
        logger.info("answer: %s", answer)
        if answer and self.prs_resume_point:
            self.doSeek(int(self.prs_resume_point))

    def leavePlayer(self):
        logger.info("...")
        self.is_closing = True
        self.prs_resume_points.setResumePoint(self.session, self.prs_sid)
        self.session.nav.stopService()
        self.close()

    def doEofInternal(self, playing):
        logger.info("playing: %s, self.execing: %s", playing, self.execing)
        if self.execing:
            self.is_closing = True
            self.close()
