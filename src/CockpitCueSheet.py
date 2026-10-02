# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0


from Screens.InfoBarGenerics import InfoBarCueSheetSupport
from .Debug import logger
from .CutList import CutList
from .CutListUtils import removeFirstMarks


class CockpitCueSheet(InfoBarCueSheetSupport, CutList):

    def __init__(self, service):
        self.service = service
        InfoBarCueSheetSupport.__init__(self)
        CutList.__init__(self)
        self.cut_list = []

    def getCutList(self):
        logger.info("cut_list: %s", self.cut_list)
        return self.cut_list

    def downloadCuesheet(self):
        path = self.service.getPath() if self.service else None
        self.cut_list = self.readCutList(path)
        self.cut_list = removeFirstMarks(self.cut_list)
        logger.debug("path: %s, cut_list: %s", path, self.cut_list)

    def uploadCuesheet(self):
        path = self.service.getPath() if self.service else None
        logger.debug("path: %s, cut_list: %s", path, self.cut_list)
        self.writeCutList(self.service.getPath(), self.cut_list)
