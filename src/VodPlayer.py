# Copyright (C) 2026 by xcentaurix
# License: GNU General Public License v3.0

"""The player of a VOD title that is recorded while it plays (see VodRecording): the regular CockpitPlayer, as
MovieCockpit plays a recording, with

- the record key, which makes the temporary recording a permanent one,
- the end of the file that has caught up with the recording waited out, not taken for the end of the title,
- the progress of the title kept in the list of the plugin as well. Leaving the player writes the resume point to the
  .cuts file next to the recording, like any recording (CockpitPlayer.leavePlayer).

What happens to the recording when the player is left is up to the caller, see VodRecording.stop."""

from Components.ActionMap import HelpableActionMap
from Screens.InfoBarGenerics import InfoBarSeek
from Screens.MessageBox import MessageBox

from . import _
from .CockpitPlayer import CockpitPlayer
from .CutListUtils import ptsToSeconds, secondsToPts
from .Debug import logger


class VodPlayer(CockpitPlayer):

    def __init__(self, session, service, sid, resume_points, service_center, config_plugins_plugin, recording):
        self.vod_sid = sid
        self.vod_resume_points = resume_points
        self.vod_recording = recording
        self.vod_at_end = False
        # stream=True: the icon of the player is the streaming one, the title is a stream that is recorded
        CockpitPlayer.__init__(self, session, service, config_plugins_plugin, None, True, 0, None, service_center, True)
        self.skinName = "CockpitPlayer"
        self["RecordActions"] = HelpableActionMap(self, "CockpitActions", {
            "RECORD": (self.record, _("Record the movie")),
        }, -2)

    def isRecording(self):
        return self.vod_recording.growing()

    def getRecordingPosition(self):
        # The recording progress bar of the infobar (COCRecordPosition). The regular player takes the time since the
        # recording began, which is right for a recording that runs in real time; this one is written several times
        # faster than it plays, so it is the length of what is written (the proxy knows it).
        position = secondsToPts(int(self.vod_recording.recordedSeconds())) if self.service_started else 0
        logger.debug("recording_position: %ss (%s)", ptsToSeconds(position), position)
        return position

    def record(self):
        if self.vod_recording.makePermanent():
            logger.info("recording: %s", self.vod_recording.path)
            self.session.open(MessageBox, _("The movie is being recorded.") + "\n" + _("The recording goes on when the player is left."),
                              MessageBox.TYPE_INFO, timeout=5)
        else:
            self.session.open(MessageBox, _("The movie is already being recorded."), MessageBox.TYPE_INFO, timeout=5)

    def _InfoBarSeek__evEOF(self):  # pylint: disable=invalid-name
        # The name is the one InfoBarSeek registers its end of file handler under (its private __evEOF).
        # The end of a file that is still recorded is not the end of the title: playback has caught up with the
        # recording (at the start, while the first segments are written, or after a skip ahead). The file push thread
        # waits for more data and reports the end of the file every second until it comes; InfoBarSeek would take
        # it for the end of the recording and leave the player, so it is waited out (see TimeshiftCockpit).
        if self.vod_recording.growing():
            if self.seekstate[1] > 1 or self.seekstate[2] > 0:
                logger.info("end of file while fast forwarding or in slow motion, back to play: %s", self.seekstate)
                self.setSeekState(self.SEEK_STATE_PLAY)
                return
            if self.seekstate == self.SEEK_STATE_PLAY:
                logger.debug("end of file in play, waiting for the recording to grow")
                return
        InfoBarSeek._InfoBarSeek__evEOF(self)   # pylint: disable=protected-access

    def leavePlayer(self):
        length = self.getLength()
        if self.vod_at_end and length:   # played to the end: seen
            self.vod_resume_points.setResumePoint(self.session, self.vod_sid, duration=length, position=length)
        else:
            self.vod_resume_points.setResumePoint(self.session, self.vod_sid, duration=length or None)
        CockpitPlayer.leavePlayer(self)   # the resume point of the recording, in its .cuts file

    def doEofInternal(self, playing):
        self.vod_at_end = True
        CockpitPlayer.doEofInternal(self, playing)
