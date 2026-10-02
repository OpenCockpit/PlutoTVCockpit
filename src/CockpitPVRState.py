# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0

# pylint: disable=no-member


from Screens.Screen import Screen
from Components.Label import Label
from Components.Pixmap import Pixmap
from Components.config import config
from Tools.LoadPixmap import LoadPixmap
from .__init__ import _
from .Debug import logger
from .SkinUtils import getSkinPath


class ScreenPVRState(Screen):

    def __init__(self, session):
        Screen.__init__(self, session)
        self.skinName = "ScreenPVRState"
        self["state"] = Label()
        self["state_pic"] = Pixmap()


class CockpitPVRState():

    def __init__(self):
        self.on_play_state_changed = self.onPlayStateChanged[:]
        self.onPlayStateChanged = [self.playStateChanged]
        self.pvr_state_dialog = self.session.instantiateDialog(ScreenPVRState)
        self.show_state_pic = True

        self.onShow.append(self.mayShow)
        self.onHide.append(self.pvr_state_dialog.hide)
        self.onClose.append(self.delPvrState)

    def playStateChanged(self, state):
        logger.info("state: %s", state)
        play_state = state[3]
        logger.debug("play_state: %s", play_state)
        state_pic = "dvr_stop.svg"
        factor = ""
        if play_state == ">":
            state_pic = "dvr_play.svg"
        elif play_state == "||":
            state_pic = "dvr_pause.svg"
        elif play_state == "END":
            state_pic = "dvr_stop.svg"
            factor = _("End")
        elif play_state.startswith(">>"):
            factor = play_state.split(" ")[1]
            state_pic = "dvr_forward.svg"
        elif play_state.startswith("<<"):
            state_pic = "dvr_backward.svg"
            factor = play_state.split(" ")[1]
        elif play_state.startswith("/"):
            state_pic = "dvr_play.svg"
            factor = "1" + play_state + "x"

        pic_path = getSkinPath("images/dvr_controls/" + state_pic)
        ptr = LoadPixmap(pic_path, cached=False, width=100, height=100, scaletoFit=1)
        if ptr and self.pvr_state_dialog["state_pic"].instance:
            self.pvr_state_dialog["state_pic"].instance.setPixmap(ptr)
            self.pvr_state_dialog["state_pic"].show()
            logger.debug("Pixmap set and shown: %s (100x100, scaletoFit)", state_pic)
        else:
            logger.warning("Failed to load pixmap: %s or instance not ready", pic_path)
        self.pvr_state_dialog["state"].setText(factor)

        logger.debug("seekstate: %s, show_state_pic: %s", self.seekstate, self.show_state_pic)
        if not self.show_state_pic or (not config.usage.show_infobar_on_skip.value and self.seekstate in (self.SEEK_STATE_PLAY, self.SEEK_STATE_EOF)):
            logger.debug("Hiding PVR state dialog")
            self.pvr_state_dialog.hide()
        elif self.show_state_pic:
            self.mayShow()

    def delPvrState(self):
        logger.info("...")
        self.onPlayStateChanged = self.on_play_state_changed[:]
        self.session.deleteDialog(self.pvr_state_dialog)
        self.pvr_state_dialog = None

    def mayShow(self):
        logger.info("execing: %s, seekstate: %s", self.execing, self.seekstate)
        if self.execing and self.seekstate != self.SEEK_STATE_PLAY:
            logger.debug("Showing PVR state dialog")
            self.pvr_state_dialog.show()
