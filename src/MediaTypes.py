# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0


# The file extensions of the media types. No enigma2 imports: the standalone cache loader of MediaCockpit (CacheCockpit/mdc_load.py)
# uses them, too. ServiceUtils has them by name for the code that imports them from there.

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

# MediaCockpit lists what it can show and play: no disc images and no Blu-ray index (a DVD or Blu-ray service is not part of its
# dependencies, and the slideshow plays with GStreamer), but more picture formats (ePicLoad reads them) and music formats
# (GStreamer plays them).
MDC_EXT_VIDEO = EXT_TS + EXT_M2TS + EXT_VIDEO
MDC_EXT_PICTURE = EXT_PICTURE + [".bmp", ".gif"]
MDC_EXT_MUSIC = EXT_MUSIC + [".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".wav"]
MDC_ALL_MEDIA = MDC_EXT_VIDEO + MDC_EXT_PICTURE + MDC_EXT_MUSIC + EXT_PLAYLIST
