# Copyright (C) 2026 by xcentaurix
# License: GNU General Public License v3.0

"""Plays a VOD asset from a file that is recorded while it plays.

An HLS stream of the proxy is a sliding window: it can be rewound by about two minutes and not skipped ahead in, and
the flush of a seek in it blocks the whole of Enigma2 (the GIL is held by the main thread while the proxy, which
runs in the same process, has to answer the playlist the seek asks for). A file has none of that: Enigma2 plays it
like any recording, with all of its seek functions.

StreamProxyCockpit already records its channels (/rec/<id>.ts, see StreamProxy.recordServiceExtension); for a VOD
asset that is the whole asset from its first segment, written as fast as the proxy produces it. The player starts on
the file as soon as a few seconds of it are on disk.

The file is written to the movie directory under its final name, as a temporary recording: a marker file next to it
says so, and it is deleted when the player is left. The record key makes it a permanent recording: the marker goes,
the files MovieCockpit lists a recording by are written, and the recording goes on to the end of the asset when the
player is left (see Finisher).
"""

import glob
import os
from time import monotonic, time

import NavigationInstance
from Components.config import config
from Components.Task import Job, Task
from enigma import eServiceReference, eTimer
from Tools.Directories import isPluginInstalled
from Plugins.SystemPlugins.JobCockpit.JobSupervisor import JobSupervisor
from Plugins.SystemPlugins.StreamProxyCockpit import LiveProxyUnmuxed, StreamProxy
try:
    from Plugins.SystemPlugins.CacheCockpit.FileManager import FileManager
except Exception:  # pylint: disable=broad-except  # MovieCockpit's cache is optional
    FileManager = None

from . import _
from .Debug import logger
from .FileUtils import deleteFile, deleteFiles, writeFile
from .ParserMetaFile import ParserMetaFile
from .RecordingUtils import calcRecordingFilename
from .Version import ID

SERVICE_NAME = "Pluto TV"      # the "channel" of the file name, as in "<date> - <channel> - <title>"
MARKER = ".vodtmp"             # <recording>.ts.vodtmp: the recording is temporary
HEAD_START_BYTES = 2_000_000   # about 5 s of a stream of 3 Mbit/s: what is on disk before the player starts
RESERVE_BYTES_PER_SECOND = 750_000   # 6 Mbit/s: more than the proxy's streams have, for the free space check
FREE_SPACE_MARGIN = 200_000_000
STALL_SECONDS = 30   # a file that doesn't grow for so long is not recorded any more (the recording failed)
FINISH_POLL_MS = 5000
MOVIE_DATABASE_ID = "MVC"   # MovieCockpit's


def recordingDirectory():
    """The movie directory of Enigma2 (never /tmp, which is RAM), or None if there is none that can be written to."""
    path = config.usage.default_path.value
    return path if path and os.path.isdir(path) and os.access(path, os.W_OK) else None


def removeFiles(base, restarts_only=False):
    """Delete the files of the recording *base* (path without the extension): the recording, its index and meta
    files, and the ones of the new file Enigma2's recorder starts (<base>_001) when the stream ends. With
    *restarts_only* only the latter.

    They are deleted, not emptied first: Enigma2 maps the index of a file that plays into memory, and a file that
    shrinks under a mapping ends the process (SIGBUS) at the next access. A deleted file stays readable for whoever
    has it open or mapped, and its space is free when they let go."""
    patterns = [glob.escape(base) + "_[0-9][0-9][0-9].*"]
    if not restarts_only:
        patterns.insert(0, glob.escape(base) + ".*")
        unloadMovieDatabase(base + ".ts")
    for pattern in patterns:
        deleteFiles(pattern)


def removeLeftovers(directory):
    """Delete the temporary recordings that Enigma2 left behind when it ended while they played."""
    for marker in glob.glob(os.path.join(glob.escape(directory), "*.ts" + glob.escape(MARKER))):
        logger.debug("leftover: %s", marker)
        removeFiles(os.path.splitext(os.path.splitext(marker)[0])[0])


def loadMovieDatabase(path):
    """Load the database entry of the recording *path* from its files, for MovieCockpit (which lists recordings by its
    database). Only if MovieCockpit is installed: without it there is no such database to keep up to date, and asking
    CacheCockpit for one would create it."""
    if FileManager and isPluginInstalled("MovieCockpit"):
        FileManager.getInstance(MOVIE_DATABASE_ID).loadDatabaseFile(path)


def unloadMovieDatabase(path):
    """Take the entry of the recording *path* out of MovieCockpit's database again: its list may have put the file in
    while it was still written (a temporary recording is listed like any file of the directory). Same condition as
    loadMovieDatabase."""
    if FileManager and isPluginInstalled("MovieCockpit"):
        file_manager = FileManager.getInstance(MOVIE_DATABASE_ID)
        file_manager.delete("table1", path)
        file_manager.delete("table2", os.path.basename(path))


class VodRecording():

    def __init__(self):
        self.record_service = None
        self.channel_id = None
        self.name = ""
        self.description = ""
        self.duration = 0
        self.begin = 0
        self.permanent = False
        self.done = False   # a permanent recording that has been finished
        self.last_size = 0
        self.last_growth = monotonic()
        self.path = ""

    def start(self, reference, name, duration, description=""):
        """Start recording *reference* (the address of the proxy's channel) as a temporary recording. False if that is
        not possible (no movie directory, no room for *duration* seconds, the recorder won't start): the caller plays
        the stream then."""
        directory = recordingDirectory()
        if directory is None:
            logger.debug("no directory to record to")
            return False
        _scheme, self.channel_id, _lazy = StreamProxy.channelId(reference)
        if not self.channel_id:
            logger.debug("%s is no channel of the proxy", reference.toString())
            return False
        removeLeftovers(directory)
        try:
            free = os.statvfs(directory)
            free_bytes = free.f_bavail * free.f_frsize
        except OSError as exc:
            logger.error("directory: %s, exception: %s", directory, exc)
            return False
        if duration and free_bytes < duration * RESERVE_BYTES_PER_SECOND + FREE_SPACE_MARGIN:
            logger.debug("not enough room in %s: %s bytes free for %s s", directory, free_bytes, duration)
            return False

        self.name = name
        self.description = description
        self.duration = duration
        self.begin = int(time())
        base = calcRecordingFilename(self.begin, SERVICE_NAME, name, directory)
        number = 1
        while glob.glob(glob.escape(base) + ".*") or glob.glob(glob.escape(base) + "_[0-9][0-9][0-9].*"):
            number += 1   # the same title recorded again within the minute: not over the other one
            base = calcRecordingFilename(self.begin, SERVICE_NAME, f"{name} ({number})", directory)
        self.path = base + ".ts"

        record_service = NavigationInstance.instance.recordService(reference, True)
        if not record_service:
            logger.debug("no record_service")
            return False
        self.record_service = record_service
        self.last_growth = monotonic()
        writeFile(self.path + MARKER, "")
        if record_service.prepare(self.path, self.begin, self.begin, -1, "", "", "", True, False):
            logger.debug("prepare failed")
            self.stop(keep_channel=True)
            return False
        if record_service.start():
            logger.debug("start failed")
            self.stop(keep_channel=True)
            return False
        logger.debug("recording to %s", self.path)
        return True

    def size(self):
        """The size of the file; it also notes when the file last grew (see growing)."""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            size = 0
        if size != self.last_size:
            self.last_size = size
            self.last_growth = monotonic()
        return size

    def ready(self):
        """True once enough of the asset is on disk to start playing it."""
        return self.size() >= HEAD_START_BYTES

    def growing(self):
        """True while the file has more to come: the end of the file that a player reaches then is the one it has
        caught up with the recording at, not the end of the asset. Not after the recording has failed, which is when
        the file has stopped growing for STALL_SECONDS and the asset isn't complete."""
        if not self.record_service or StreamProxy.vod_recording_complete(self.channel_id):
            return False
        self.size()
        return monotonic() - self.last_growth < STALL_SECONDS

    def recordedSeconds(self):
        """How many seconds of the title are in the file so far: the progress of the recording."""
        return StreamProxy.vod_recording_seconds(self.channel_id)

    def reference(self):
        """The service reference of the file."""
        ref = eServiceReference(eServiceReference.idDVB, 0, self.path)
        ref.setName(self.name)
        return ref

    def makePermanent(self):
        """Make the temporary recording a permanent one: what MovieCockpit lists a recording by is written, and the
        recording is no longer deleted when the player is left. False if it was permanent already."""
        if self.permanent or not self.record_service:
            return False
        self.permanent = True
        deleteFile(self.path + MARKER)
        meta = ParserMetaFile(self.path)
        meta.updateMeta({
            "service_reference": "1:0:0:0:0:0:0:0:0:0:",
            "name": self.name,
            "description": self.description,
            "rec_time": self.begin,
            "tags": "",
            "length": self.duration,   # the whole asset, not what is written so far
            "size": self.last_size,
        })
        meta.updateXMeta({
            "timer_start_time": self.begin,
            "timer_stop_time": self.begin + self.duration,
            "recording_start_time": self.begin,
            "recording_stop_time": self.begin + self.duration,
            "recording_margin_before": 0,
            "recording_margin_after": 0,
        })
        loadMovieDatabase(self.path)
        logger.debug("permanent: %s", self.path)
        Finisher.add(self)   # finishes it when its asset is written, whether the player is still open or not
        # the job is what MovieCockpit (another plugin) sees: the recording is marked as being recorded, is kept from
        # being deleted or moved, and its progress is shown in the list and in the player
        JobSupervisor.getInstance().getJobManager(ID).AddJob(VodRecordingJob(self))
        return True

    def progress(self):
        """The percent of the title that is written to the recording."""
        if StreamProxy.vod_recording_complete(self.channel_id):
            return 100
        if self.duration <= 0:
            return 0
        return min(99, int(self.recordedSeconds() * 100 / self.duration))

    def cancel(self):
        """Stop a permanent recording now, as it is (the job was aborted): what is written stays."""
        self.finish()

    def stop(self, keep_channel=False):
        """End the use of the recording by the player.

        A temporary recording is stopped and deleted, with what Enigma2 wrote next to it (index, meta), and the
        proxy's channel of the asset is closed, so that it stops downloading and muxing at once: nothing reads it any
        more, and it would otherwise run on until the idle reaper ends it a minute later, slowing the next title down.
        Not with *keep_channel*, for the caller that plays the stream of the channel instead.

        A permanent recording goes on to the end of the asset, Finisher finishes it (it has done since the record key
        was pressed). MovieCockpit keeps the resume point (and the rest it lists a recording by) in its database, not
        only in the .cuts file that the player has just written: the entry is loaded from the files again."""
        if self.permanent:
            loadMovieDatabase(self.path)
            Finisher.add(self)
            return
        self.stopRecorder()
        if self.channel_id and not keep_channel:
            LiveProxyUnmuxed.close_channel(self.channel_id)
        if self.path:
            removeFiles(os.path.splitext(self.path)[0])
            self.path = ""

    def stopRecorder(self):
        record_service, self.record_service = self.record_service, None
        if record_service:
            NavigationInstance.instance.stopRecordService(record_service)

    def finished(self):
        return not self.growing()

    def finish(self):
        """End a permanent recording: its asset is written to the end, or the recording has failed. Stops the recorder
        and loads the database entry of the recording again, with its final size, for MovieCockpit. This is at the end
        of the recording, not of the playback: the player may be open still (it plays what is written, and its end of
        the file is the end of the title from now on)."""
        if self.done:
            return
        self.done = True
        complete = StreamProxy.vod_recording_complete(self.channel_id)
        size = self.size()
        self.stopRecorder()
        # Enigma2's recorder takes the end of the stream for an interruption and starts the next file (<name>_001.ts)
        removeFiles(os.path.splitext(self.path)[0], restarts_only=True)
        LiveProxyUnmuxed.close_channel(self.channel_id)
        ParserMetaFile(self.path).updateMeta({"size": size})
        loadMovieDatabase(self.path)
        if complete:
            logger.debug("recorded: %s, %s bytes", self.path, size)
        else:
            logger.error("the recording of %s stopped before the end of the asset, %s bytes", self.path, size)


class VodRecordingTask(Task):
    """The task of a VodRecordingJob: the recording goes on by itself, this follows it until Finisher has finished it."""

    POLL_MS = 2000

    def __init__(self, job, recording):
        Task.__init__(self, job, _("Recording"))
        self.recording = recording
        self.poll_timer = eTimer()
        self.poll_timer.callback.append(self.poll)

    def run(self, callback):
        self.callback = callback
        self.poll()
        if not self.recording.done:
            self.poll_timer.start(self.POLL_MS, False)

    def poll(self):
        self.setProgress(self.recording.progress())
        if self.recording.done:
            self.poll_timer.stop()
            self.setProgress(self.recording.progress())
            self.finish()

    def abort(self):
        self.recording.cancel()
        self.poll()


class VodRecordingJob(Job):
    """A permanent recording of a VOD title, in the job manager of the plugin.

    It stands for the recording in the job supervisor, which the plugins share (see RecordingUtils.getDownloadRecordingJob):
    is_recording and target_path make the recording known to MovieCockpit. The percent is not that of the task: the
    recording is written whether the job has been started or waits behind another one of its job manager."""

    is_recording = True

    def __init__(self, recording):
        Job.__init__(self, f"{_('Recording')} - {recording.name}")
        self.recording = recording
        self.target_path = recording.path
        VodRecordingTask(self, recording)

    def getProgress(self):
        return self.recording.progress()

    progress = property(getProgress)


class _Finisher():
    """Finishes the permanent recordings: they go on until their asset is written to the end (or the recording
    fails), also when the player has been left, and are finished here."""

    def __init__(self):
        self.recordings = []
        self.timer = eTimer()
        self.timer.callback.append(self.check)

    def add(self, recording):
        if recording.done:
            return
        if recording not in self.recordings:
            self.recordings.append(recording)
            logger.debug("%s is finished when its asset is written", recording.path)
        if not self.timer.isActive():
            self.timer.start(FINISH_POLL_MS, False)

    def check(self):
        for recording in list(self.recordings):
            if recording.finished():
                self.recordings.remove(recording)
                recording.finish()
        if not self.recordings:
            self.timer.stop()


Finisher = _Finisher()
