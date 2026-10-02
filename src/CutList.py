# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0


from .Debug import logger
from .CutListUtils import packCutList, unpackCutList, replaceLast, removeMarks
from .FileUtils import readFile, writeFile


class CutList():

    def __init__(self):
        return

    def updateCutList(self, path, last=None):
        logger.debug("last: %s,", last)
        if last is not None:
            cut_list = replaceLast(self.readCutList(path), last)
            self.writeCutList(path, cut_list)

    def removeCutListMarks(self, path):
        cut_list = removeMarks(self.readCutList(path))
        self.writeCutList(path, cut_list)

    def readCutList(self, path):
        cut_list = []
        logger.debug("path: %s", path)
        if not path:
            return cut_list
        data = readFile(path + ".cuts", "rb")
        if data:
            cut_list = unpackCutList(data)
        logger.info("cut_list: %s", cut_list)
        return cut_list

    def writeCutList(self, path, cut_list):
        logger.debug("path: %s, cut_list: %s", path, cut_list)
        if not path:
            return
        data = packCutList(cut_list)
        writeFile(path + ".cuts", data, "wb")
