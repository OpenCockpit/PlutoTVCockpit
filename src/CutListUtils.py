# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0


import sys
import struct
from bisect import insort


cutsParser = struct.Struct('>QI')  # big-endian, 64-bit PTS and 32-bit type


# cut_list data structure
# cut_list[x][0] = pts  = long long
# cut_list[x][1] = what = long


CUT_TYPE_IN = 0
CUT_TYPE_OUT = 1
CUT_TYPE_MARK = 2
CUT_TYPE_LAST = 3


def removeFirstMarks(cut_list):
    # ignore first 10 seconds
    for pts, what in cut_list:
        if pts < secondsToPts(10):
            cut_list.remove((pts, what))
    return cut_list


def ptsToSeconds(pts):
    return int(pts / 90 / 1000)


def secondsToPts(seconds):
    return int(seconds * 90 * 1000)


def packCutList(cut_list):
    data = b""
    for pts, what in cut_list:
        if pts <= sys.maxsize:
            data += struct.pack('>QI', pts, what)
    return data


def insortCutList(cut_list, in_pts, in_what):
    INSORT_SCOPE = 45000  # 0.5 seconds * 90 * 1000
    for pts, what in cut_list:
        if what == in_what:
            if pts - INSORT_SCOPE < in_pts < pts + INSORT_SCOPE:
                # found a conflicting entry, remove it to avoid doubles and short jumps
                cut_list.remove((pts, what))
    insort(cut_list, (in_pts, in_what))
    return cut_list


def unpackCutList(data):
    cut_list = []
    pos = 0
    while pos + 12 <= len(data):
        pts, what = struct.unpack('>QI', data[pos:pos + 12])
        if pts <= sys.maxsize:
            cut_list = insortCutList(cut_list, pts, what)
        pos += 12
    return cut_list


def getCutListFirst(cut_list, margin):
    first = 0
    margin = secondsToPts(margin)
    for pts, what in cut_list:
        if what == CUT_TYPE_MARK:
            if margin < pts < first or first == 0:
                first = pts
    return first


def getCutListLast(cut_list):
    last = 0
    for pts, what in cut_list:
        if what == CUT_TYPE_LAST:
            last = pts
            break
    return last


def replaceLast(cut_list, last):
    for pts, what in cut_list:
        if what == CUT_TYPE_LAST:
            cut_list.remove((pts, what))
    if last > 0:
        cut_list = insortCutList(cut_list, last, CUT_TYPE_LAST)
    return cut_list


def removeMarks(cut_list):
    for pts, what in cut_list:
        if what == CUT_TYPE_MARK:
            cut_list.remove((pts, what))
    return cut_list
