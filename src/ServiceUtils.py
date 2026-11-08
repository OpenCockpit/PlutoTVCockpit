# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0


import os
from enigma import eServiceReference, loadPNG
from Components.Renderer.Picon import getPiconName
from .Debug import logger


SID_DVB = eServiceReference.idDVB  # eServiceFactoryDVB::id  enum{id = 0x0001}; - standard enigma2
SID_DVD = 0x1111  # eServiceFactoryDVD::id  enum{id = 0x1111}; - not in standard enigma2
SID_M2TS = 0x0003  # eServiceFactoryM2TS::id enum{id = 0x0003}; - not in standard enigma2
SID_GST = 0x1001  # eServiceFactoryGST::id  enum{id = 0x1001}; - not in standard enigma2


EXT_TS = [".ts", ".trp"]
EXT_M2TS = [".m2ts"]
EXT_DVD = [".ifo", ".iso", ".img"]
EXT_VIDEO = [".avi", ".divx", ".f4v", ".flv", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".mts", ".vob", ".wmv", ".asf", ".stream", ".webm"]
EXT_BLU = [".bdmv"]

EXT_PICTURE = [".jpg", ".jpeg", ".png"]
EXT_MUSIC = [".mp3", ".wma"]
EXT_PLAYLIST = [".m3u"]

ALL_VIDEO = EXT_TS + EXT_M2TS + EXT_DVD + EXT_VIDEO + EXT_BLU
ALL_MEDIA = ALL_VIDEO + EXT_PICTURE + EXT_MUSIC + EXT_PLAYLIST


# DEFAULT_VIDEO_PID = 0x44
# DEFAULT_AUDIO_PID = 0x45


def getService(path, name=""):
    service = None
    ext = os.path.splitext(path)[1].lower()
    if ext in EXT_TS:
        service = eServiceReference(SID_DVB, 0, path)
    elif ext in EXT_DVD:
        service = eServiceReference(SID_DVD, 0, path)
    elif ext in EXT_M2TS:
        service = eServiceReference(SID_M2TS, 0, path)
    else:
        service = eServiceReference(SID_GST, 0, path)
        # service.setData(0, DEFAULT_VIDEO_PID)
        # service.setData(1, DEFAULT_AUDIO_PID)
    service.setName(name)
    return service


def getPicon(service_reference):
    picon_path = getPiconName(service_reference)
    logger.debug("picon_path: %s", picon_path)
    return loadPNG(picon_path) if picon_path else None
