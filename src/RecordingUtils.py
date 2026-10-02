# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0


import os
import re
from time import time, strftime, localtime
from RecordTimer import AFTEREVENT
from ServiceReference import ServiceReference
import NavigationInstance
from Screens.InfoBar import InfoBar
from Plugins.SystemPlugins.JobCockpit.JobCockpit import JobCockpit
from .Debug import logger
from .JobUtils import getPendingJobs


def getTimerFilename(timer):
    """Return the recording filename for a timer in a compatible way.

    Different images may expose the filename under different attributes.
    Try several options and fall back to dirname/name if necessary.
    """
    filename = getattr(timer, "Filename", None)
    if filename:
        return filename

    filename = getattr(timer, "filename", None)
    if filename:
        return filename

    get_filename = getattr(timer, "getFilename", None)
    if callable(get_filename):
        try:
            filename = get_filename()
            if filename:
                return filename
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("getFilename() failed for timer %s: %s", timer, e)

    dirname = getattr(timer, "dirname", None)
    name = getattr(timer, "name", None)
    if dirname and name:
        return os.path.join(dirname, name)

    logger.warning("Unable to determine recording filename for timer: %s", timer)
    return None


def getRecordings():
    return getLiveRecordings() + getTimeshiftRecordings()


def getLiveRecordings():
    logger.debug("...")
    recordings = []
    for timer in NavigationInstance.instance.RecordTimer.timer_list:
        if timer.isRunning() and not timer.justplay:
            filename = getTimerFilename(timer)
            if filename:
                recordings.append(filename + ".ts")
    return recordings


def getTimeshiftRecordings():
    logger.debug("...")
    recordings = []
    if hasattr(InfoBar.instance, "getTimeshiftRecordings"):
        recordings = InfoBar.instance.getTimeshiftRecordings()
    return recordings


def isRecording(path=""):
    return isLiveRecording(path) or isTimeshiftRecording(path) or isDownloadRecording(path)


def isLiveRecording(path=""):
    timer = None
    for __timer in NavigationInstance.instance.RecordTimer.timer_list:
        if __timer.isRunning() and not __timer.justplay:
            filename = getTimerFilename(__timer)
            if not filename:
                continue
            if not path or path == filename + ".ts":
                timer = __timer
                break
    logger.debug("path: %s, is_recording: %s", path, timer is not None)
    return timer


def isLiveRecordingOrRecordingSoon(session):
    logger.info("...")
    timer = isLiveRecording()
    next_rec_time = session.nav.RecordTimer.getNextRecordingTime()
    return timer or (next_rec_time > 0 and (next_rec_time - time()) < 360)


def isTimeshifting():
    is_timeshifting = False
    if hasattr(InfoBar.instance, "isTimeshifting"):
        is_timeshifting = InfoBar.instance.isTimeshifting()
    logger.debug("is_timeshifting: %s", is_timeshifting)
    return is_timeshifting


def isTimeshiftRecording(path=""):
    is_timeshift_recording = False
    if hasattr(InfoBar.instance, "isTimeshiftRecording"):
        is_timeshift_recording = InfoBar.instance.isTimeshiftRecording(path)
    logger.debug("is_timeshift_recording: %s", is_timeshift_recording)
    return is_timeshift_recording


def isDownloadRecording(path=""):
    jobs = getPendingJobs("MTC")
    for job in jobs:
        if job.target_path == path:
            logger.info("download recording path: %s", path)
            return True
    return False


def isStreamRecording(path=""):
    jobs = getPendingJobs("TMP")
    for job in jobs:
        if job.target_path == path:
            logger.info("stream recording path: %s", path)
            return True
    return False


def stopRecording(path, force=True):
    logger.info("path: %s, force: %s", path, force)
    logger.debug("is_timeshift_recording: %s", isTimeshiftRecording(path))
    if isTimeshiftRecording(path):
        logger.debug("stopping timeshift recording")
        jobs = getPendingJobs("TSC")
        for job in jobs:
            logger.debug("target_path: %s", job.target_path)
            if job.target_path == path:
                JobCockpit.abortJob(job, "TSC", force)
                logger.info("stopped timeshift path: %s", path)
                break
    else:
        timer = isLiveRecording(path)
        if timer:
            logger.debug("stopping recording")
            if timer.repeated:
                timer.enable()
                timer_afterEvent = timer.afterEvent
                timer.afterEvent = AFTEREVENT.NONE
                timer.processRepeated(findRunningEvent=False)
                NavigationInstance.instance.RecordTimer.doActivate(timer)
                timer.afterEvent = timer_afterEvent
                NavigationInstance.instance.RecordTimer.timeChanged(timer)
            else:
                timer.afterEvent = AFTEREVENT.NONE
                NavigationInstance.instance.RecordTimer.removeEntry(timer)
            logger.info("stopped recording path: %s", path)


def stopTimeshift():
    logger.info("...")
    if hasattr(InfoBar.instance, "removeTimeshift"):
        InfoBar.instance.removeTimeshift()


def startTimeshift():
    logger.info("...")
    if hasattr(InfoBar.instance, "addTimeshift"):
        InfoBar.instance.addTimeshift()


def calcRecordingFilename(begin, service, event_title, dirname):
    if isinstance(service, str):
        service_name = service
    else:
        service_name = ServiceReference(service).getServiceName()
    logger.info("begin: %s, service_name: %s, event_title: %s, dirname: %s", begin, service_name, event_title, dirname)
    begin_date_time = strftime("%Y%m%d %H%M", localtime(begin))
    filename = f"{begin_date_time} - {service_name} - {event_title}"
    filename = filename.replace('\xc2\x86', '').replace('\xc2\x87', '')
    filename = re.sub(r'[<>:"/\\|?*\0]', '_', filename)
    filename = os.path.join(dirname, filename)
    logger.debug("filename: %s", filename)
    return filename
