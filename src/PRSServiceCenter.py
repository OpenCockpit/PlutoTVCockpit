# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0

"""Shared stand-in for eServiceCenter for the FAST-channel TV Cockpit plugins
(Rakuten TV, Samsung TV Plus, ...). Same behaviour as PlutoTVCockpit's own
ServiceCenter."""

from enigma import iServiceInformation
from .Debug import logger


class ServiceCenter():
    """Stand-in for eServiceCenter that serves the movie/channel info of the
    item being played instead of reading it from a local file.

    Call setMovie() with the data from the plugin's listing before opening the
    player; the length comes from the listing's own "duration" field because a
    stream does not reliably report one itself.
    """

    def __init__(self):
        self.name = ""
        self.description = ""
        self.length = 0
        logger.debug("...")

    def setMovie(self, name, description="", length=0):
        logger.debug("name: %s, length: %s", name, length)
        self.name = name
        self.description = description
        self.length = length

    def info(self, service):
        logger.debug("...")
        return ServiceInfo(service, self.name, self.description, self.length)


class ServiceInfo():

    def __init__(self, service, name, description, length):
        logger.debug("service.getPath(): %s", service.getPath() if service else None)
        self.info = Info(service, name, description, length)

    def getLength(self, _service=None):
        logger.debug("...")
        return self.info.getLength()

    def getInfoString(self, _service=None, info_type=None):
        logger.debug("info_type: %s", info_type)
        if info_type == iServiceInformation.sServiceref:
            return self.info.getServiceReference()
        if info_type == iServiceInformation.sDescription:
            return self.info.getShortDescription()
        if info_type == iServiceInformation.sTags:
            return self.info.getTags()
        return None

    def getInfo(self, _service=None, info_type=None):
        logger.debug("info_type: %s", info_type)
        if info_type == iServiceInformation.sTimeCreate:
            return self.info.getEventStartTime()
        return None

    def getInfoObject(self, _service=None, info_type=None):
        logger.debug("info_type: %s", info_type)
        if info_type == iServiceInformation.sFileSize:
            return self.info.getSize()
        return None

    def getName(self, _service=None):
        logger.debug("...")
        return self.info.getName()

    def getEvent(self, _service=None):
        logger.debug("...")
        return self.info

    def getEventStartTime(self, _service=None):
        logger.debug("...")
        return self.info.getEventStartTime()

    def getRecordingStartTime(self, _service=None):
        logger.debug("...")
        return self.info.getRecordingStartTime()


class Info():

    def __init__(self, service, name, description, length):
        self.path = service.getPath() if service else ""
        self.name = name
        self.description = description
        self.length = length
        logger.debug("path: %s, length: %s", self.path, self.length)

    def getName(self):
        logger.debug("name: %s", self.name)
        return self.name

    def getServiceReference(self):
        logger.debug("...")
        return ""

    def getTags(self):
        logger.debug("...")
        return ""

    def getEventId(self):
        logger.debug("...")
        return 0

    def getEventName(self):
        logger.debug("...")
        return self.name

    def getShortDescription(self):
        logger.debug("...")
        return self.description

    def getExtendedDescription(self):
        logger.debug("...")
        return ""

    def getBeginTimeString(self):
        logger.debug("...")
        return ""

    def getEventStartTime(self):
        logger.debug("...")
        return 0

    def getRecordingStartTime(self):
        logger.debug("...")
        return 0

    def getDuration(self):
        logger.debug("...")
        return self.length

    def getLength(self):
        logger.debug("path: %s, length: %s", self.path, self.length)
        return self.length

    def getSize(self):
        logger.debug("...")
        return 0
