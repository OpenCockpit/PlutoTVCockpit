# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0


from Components.config import config
from .FileUtils import readFile, writeFile
from .CutListUtils import ptsToSeconds, secondsToPts

# .meta File Contents
# Overview: A UTF-8 text file (up to 10 lines) with ordered fields describing the recording.
# Service reference: DVB/Enigma2 service reference string (used to find PMT/streams).
# Name: Event/program title.
# Description: Short/extended event description.
# Recording time: Start time as a UNIX integer (epoch seconds).
# Tags: Space-delimited tags for the recording (optional).
# Length: Recording length in PTS units (1/90000 s) (optional).
# Filesize: Filesize at time length was calculated (optional).
# Service data: Private service-data cache string (audio/video PIDs, etc.) (optional).
# Packet size: Transport stream packet size (e.g., 188) (optional).
# Scrambled: Flag indicating whether the service was scrambled (optional)


class ParserMetaFile():

    meta_keys = [
        "service_reference", "name", "description", "rec_time", "tags", "length", "size", "service_data", "packet_size", "scrambled"
    ]

    xmeta_keys = [
        "timer_start_time", "timer_stop_time", "recording_start_time", "recording_stop_time", "recording_margin_before",
        "recording_margin_after"
    ]

    def __init__(self, path):
        self.path = path
        self.meta_path = path + ".meta"
        self.xmeta_path = path + ".xmeta"
        self.meta = {}
        self.xmeta = {}

        self.meta_list = self.readMeta(self.meta_path)
        if self.meta_list:
            self.meta = self.list2dict(self.meta_list, self.meta_keys)
            if self.meta and self.meta["length"]:
                self.meta["length"] = ptsToSeconds(self.meta["length"])
            self.xmeta_list = self.readMeta(self.xmeta_path)
            self.xmeta = self.list2dict(self.xmeta_list, self.xmeta_keys)
            if self.meta and not self.xmeta:
                self.xmeta["recording_start_time"] = self.meta["rec_time"]
                self.xmeta["recording_stop_time"] = 0
                self.xmeta["recording_margin_before"] = config.recording.margin_before.value * 60
                self.xmeta["recording_margin_after"] = config.recording.margin_after.value * 60

    def list2dict(self, alist, keys):
        adict = {}
        for i, key in enumerate(keys):
            if i < len(alist):
                try:
                    adict[key] = int(alist[i])
                except ValueError:
                    if alist[i]:
                        adict[key] = alist[i]
                    elif key in {"rec_time", "length", "size"}:
                        adict[key] = 0
                    else:
                        adict[key] = ""
        return adict

    def dict2list(self, adict, keys):
        alist = []
        for key in keys:
            if key in adict:
                alist.append(adict[key])
            else:
                alist.append("")
        return alist

    def readMeta(self, path):
        meta_list = readFile(path).splitlines()
        meta_list = [list_item.strip() for list_item in meta_list]
        return meta_list

    def getMeta(self):
        self.meta.update(self.xmeta)
        return self.meta

    def updateMeta(self, meta):
        if "length" in meta:
            meta["length"] = secondsToPts(meta["length"])
        self.meta.update(meta)
        self.saveMeta()

    def saveMeta(self):
        alist = self.dict2list(self.meta, self.meta_keys)
        data = "\n".join([str(line) for line in alist])
        writeFile(self.meta_path, data)

    def updateXMeta(self, xmeta):
        self.xmeta.update(xmeta)
        self.saveXMeta()

    def saveXMeta(self):
        alist = self.dict2list(self.xmeta, self.xmeta_keys)
        data = "\n".join([str(line) for line in alist])
        writeFile(self.xmeta_path, data)
