# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0


import os
from enigma import eServiceReference, loadPNG
from Components.Renderer.Picon import getPiconName, piconLocator, invalidateListPicons
from .Debug import logger
from .MediaTypes import EXT_TS, EXT_M2TS, EXT_DVD, EXT_VIDEO, EXT_BLU, EXT_PICTURE, EXT_MUSIC, EXT_PLAYLIST  # noqa: F401  # pylint: disable=unused-import
from .MediaTypes import ALL_VIDEO, ALL_MEDIA, MDC_EXT_VIDEO, MDC_EXT_PICTURE, MDC_EXT_MUSIC, MDC_ALL_MEDIA  # noqa: F401  # pylint: disable=unused-import


SID_DVB = eServiceReference.idDVB  # eServiceFactoryDVB::id  enum{id = 0x0001}; - standard enigma2
SID_DVD = 0x1111  # eServiceFactoryDVD::id  enum{id = 0x1111}; - not in standard enigma2
SID_M2TS = 0x0003  # eServiceFactoryM2TS::id enum{id = 0x0003}; - not in standard enigma2
SID_GST = 0x1001  # eServiceFactoryGST::id  enum{id = 0x1001}; - not in standard enigma2


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


def announcePiconDirectory(path):
    """Picons were put into *path*: let enigma2 know. It only searches the picon folders it has found on a mounted drive (a folder
    needs a picon to be found: a new one is not searched before a re-mount), and the service lists keep what they have shown
    for a service. Must be called in the main thread."""
    try:
        piconLocator.addSearchPath(path)
        invalidateListPicons()
    except Exception as e:
        logger.error("path: %s, exception: %s", path, e)


def getPicon(service_reference):
    picon_path = getPiconName(service_reference)
    logger.debug("picon_path: %s", picon_path)
    return loadPNG(picon_path) if picon_path else None
