# Copyright (C) 2026 by xcentaurix
# License: GNU General Public License v3.0

import os
import re
from time import strftime, localtime, monotonic
from urllib.parse import quote
from twisted.internet import threads

from Components.ActionMap import HelpableActionMap
from Components.config import config, configfile
from Components.Button import Button
from Components.Label import Label
from Components.Sources.StaticText import StaticText
from Components.Pixmap import Pixmap
from Screens.ChoiceBox import ChoiceBox
from Screens.HelpMenu import HelpableScreen
from Screens.MessageBox import MessageBox
from Screens.Screen import Screen
from Tools.Directories import fileExists, isPluginInstalled
from Tools.LoadPixmap import LoadPixmap
from enigma import eServiceReference, eTimer

from . import _, __
from .Debug import logger
from .ConfigInit import COUNTRY_NAMES, getselectedcountries
from .PlutoTVRequest import plutoRequest
from .PlutoTVDownload import PlutoTVDownload, Silent
from .PRSUtils import PRSUtils
from .CockpitTVDownload import loadNoDataLocations
from .CockpitTVUtils import pickBestImage
from .Variables import TIMER_FILE, NODATA_FILE, BOUQUET_FILE
from .PRSList import PRSList
from .PlutoSetup import PlutoSetup
from .PRSCockpitPlayer import PRSCockpitPlayer
from .PRSServiceCenter import ServiceCenter
from .VodPlayer import VodPlayer
from .VodRecording import VodRecording

VOD_RECORDING_START_TIMEOUT = 40   # s the recording gets to have a few seconds of the asset on disk before the stream is played instead

_utils = PRSUtils(config.plugins.plutotvcockpit)
downloadPoster = _utils.downloadPoster
resumePointsInstance = _utils.resumePoints


class PlutoTVCockpit(Screen, HelpableScreen):

    def __init__(self, session):
        self.session = session
        Screen.__init__(self, session)
        self.skinName = "PlutoTVCockpit"
        HelpableScreen.__init__(self)
        self.service_center = ServiceCenter()
        self.last_service = None

        self["feedlist"] = PRSList([], icons=(
            ("menu.png", "menu_selected.png"),
            ("series.png", "series_selected.png"),
            ("cine.png", "cine_selected.png"),
            ("cine_half.png", "cine_half_selected.png"),
            ("cine_end.png", "cine_end_selected.png"),
        ), resume_points=resumePointsInstance)
        self["loading"] = Label(_("Loading data... Please wait"))
        self["vtitle"] = StaticText()
        self["key_red"] = Button(_("Exit"))
        self["key_yellow"] = Button()
        self.mdb = isPluginInstalled("tmdb") and "tmdb" or isPluginInstalled("IMDb") and "imdb"
        self.yellowLabel = _("TMDb Search") if self.mdb == "tmdb" else (_("IMDb Search") if self.mdb else "")
        self["key_green"] = Button()
        self["updated"] = StaticText()
        # the buttons bar shows an action key only if its source has a true boolean: StaticText with text, not Button
        self["key_menu"] = StaticText(_("MENU"))
        self["key_blue"] = Button(_("Change country"))
        self["key_text"] = StaticText()  # names the view the key switches to, see updateViewButton
        self["poster"] = Pixmap()
        self["posterBG"] = Label()
        self["info"] = StaticText()  # combined info for fluid layout, scrolled vertically by COCRunningText

        self["feedlist"].onSelectionChanged.append(self.update_data)

        self.picname = ""

        self["actions"] = HelpableActionMap(
            self, ["SetupActions", "InfobarChannelSelection", "MenuActions"],
            {
                "ok": (self.action, _("Go forward one level including starting playback")),
                "cancel": (self.exit, _("Go back one level including exiting")),
                "save": (self.green, _("Create or update PlutoTV live bouquets")),
                "historyBack": (self.back, _("Go back one level")),
                "menu": (self.loadSetup, _("Open the plugin configuration screen")),
            }, -1
        )

        self["MDBActions"] = HelpableActionMap(
            self, ["ColorActions"],
            {
                "yellow": (self.MDB, _("Search for information in %s") % (_("The Movie Database") if self.mdb == "tmdb" else _("the Internet Movie Database"))),
            }, -1
        )
        self["MDBActions"].setEnabled(False)

        self["CountryActions"] = HelpableActionMap(
            self,
            ["ColorActions"],
            {
                "blue": (self.switchCountry, _("Load the VoD list of another country")),
            },
            -1
        )

        self["ViewActions"] = HelpableActionMap(
            self,
            ["InfobarTeletextActions"],
            {
                "startTeletext": (self.toggleView, _("Switch between the category view and the list of all movies")),
            },
            -1
        )

        self.movieView = config.plugins.plutotvcockpit.movie_view.value  # False: categories, True: one list of all titles, sorted alphabetically; as it was left
        self.updateViewButton()
        self.updatebutton()

        if self.updatebutton not in Silent.afterUpdate:
            Silent.afterUpdate.append(self.updatebutton)

        self.updateDataTimer = eTimer()
        self.updateDataTimer.callback.append(self.update_data_delayed)
        self.country = config.plugins.plutotvcockpit.country.value
        self.vodRecording = None   # the recording of the VOD asset that plays (or is about to), see VodRecording
        self.vodRecordingTimer = eTimer()
        self.vodRecordingTimer.callback.append(self.vodRecordingCheck)
        self.onClose.append(self.stopVodRecording)
        self.onClose.append(self.saveSelection)
        self.initialise()
        self.onLayoutFinish.append(self.getCategories)

    def initialise(self):
        self.titlemenu = _("VOD Menu") + (" - " + COUNTRY_NAMES[self.country] if self.country in COUNTRY_NAMES else "")
        self.films = []
        self.allfilms = []
        self.menu = []
        self.history = []
        self.categoryName = ""   # the category that is open, in the category view
        self.chapters = {}
        self.numSeasons = 0
        self.vinfo = ""
        self.description = ""
        self.eptitle = ""
        self.epinfo = ""
        self["feedlist"].setList([])
        self["poster"].hide()
        self["posterBG"].hide()
        self["info"].setText("")
        self["vtitle"].setText("")
        self["loading"].show()
        self.title = _("PlutoTV") + " - " + self.titlemenu

    def update_data(self):
        self.updateDataTimer.stop()
        if not (selection := self.getSelection()):
            return
        _index, _name, __type, _id = selection
        self["MDBActions"].setEnabled(False)
        self["key_yellow"].text = ""
        if __type == "menu":
            self["poster"].hide()
            self["posterBG"].hide()
            self.updateInfo()
        else:
            self.updateDataTimer.start(500, 1)

    def update_data_delayed(self):
        if not (selection := self.getSelection()):
            return
        index, _name, __type, _id = selection
        if __type in {"movie", "series"}:
            film = self.films[index]
            self.description = film[2].decode("utf-8")
            self["vtitle"].text = film[1].decode("utf-8")
            info = "FSK: " + film[4].decode("utf-8") + ", "
            self["MDBActions"].setEnabled(True)
            self["key_yellow"].text = self.yellowLabel

            if __type == "movie":
                info += f"{int(film[5]) // 60} " + _("min") + "       "
            else:
                info += __("%s Season available", "%s Seasons available", film[10]) % film[10]
                self.numSeasons = film[10]
            self.vinfo = info
            self.updateInfo()
            picname = film[0] + ".jpg"
            self.picname = picname
            pic = film[6]
            if len(picname) > 5:
                self["poster"].hide()
                self["posterBG"].hide()
                threads.deferToThread(downloadPoster, pic, picname, self.downloadPosterCallback)

        elif __type == "seasons":
            self.eptitle = ""
            self.epinfo = ""
            if self.numSeasons == 1:  # if numSeasons == 1 skip displaying the seasons level and go directly to the next level.
                # Fix a timing issue. Calling self.lastAction directly results in the title for the previous level being displayed.
                self.lastActionTimer = eTimer()
                self.lastActionTimer.callback.append(self.lastAction)
                self.lastActionTimer.start(10, 1)

        elif __type == "episode":
            film = self.chapters[_id][index]
            self.eptitle = film[1].decode("utf-8") + "  " + f"{int(film[5]) // 60} " + _("min")
            self.epinfo = film[3].decode("utf-8")
            self.updateInfo()

    def updateInfo(self):
        # combine info for fluid layout
        vinfo = self.vinfo + "\n" if self.vinfo and self.description else self.vinfo
        spacer = "\n" if (self.vinfo or self.description) and (self.eptitle or self.epinfo) else ""
        text = "\n".join([x for x in (vinfo, self.description, spacer, self.eptitle, self.epinfo) if x])
        # COCRunningText splits its text on "|" (two texts, small area), so keep it out of the content
        self["info"].setText(text.replace("|", "/"))

    def downloadPosterCallback(self, filename, name):
        if name == self.picname:  # check if this is the current image we are waiting for
            self.updateInfo()
            self.showPoster(filename, name)

    def showPoster(self, filename, name):
        try:
            if name == self.picname and filename and os.path.isfile(filename):
                self["poster"].instance.setPixmap(LoadPixmap(filename))
                self["poster"].show()
                self["posterBG"].show()
        except Exception as ex:
            logger.debug("[PlutoScreen] showPoster, ERROR: %s", ex)

    def getCategories(self):
        self.lvod = {}
        threads.deferToThread(plutoRequest.getOndemand, self.country).addCallback(self.getCategoriesCallback)

    def getCategoriesCallback(self, ondemand):
        if not (categories := ondemand.get("categories", [])):
            self.session.open(MessageBox, _("There is no data for %s. It may be caused by geo-blocking, or Pluto TV may not be available in your country.") % COUNTRY_NAMES.get(self.country, self.country), type=MessageBox.TYPE_ERROR, timeout=10)
        else:
            for category in categories:
                self.buildlist(category)
            # Sort categories and items within each category alphabetically
            self.menu.sort(key=lambda x: re.sub(r"^[\W_]+", "", x.decode("utf-8").casefold()))
            for _key, items in self.lvod.items():
                items.sort(key=self.filmSortKey)
            # a title can be in several categories, the list of all titles has it once
            films = {}
            for items in self.lvod.values():
                for film in items:
                    films.setdefault(film[0], film)
            self.allfilms = sorted(films.values(), key=self.filmSortKey)
            self.showView()
        self["loading"].hide()

    @staticmethod
    def filmSortKey(film):
        return re.sub(r"^[\W_]+", "", film[1].decode("utf-8")).casefold()

    def filmEntries(self, films):
        return [self["feedlist"].listentry(film[1].decode("utf-8"), film[8], film[0], duration=film[5]) for film in films]

    def updateViewButton(self):
        self["key_text"].text = _("Category view") if self.movieView else _("Movie view")

    def toggleView(self):
        if not self.menu or self["loading"].visible:  # nothing loaded yet, or a request is running
            return
        self.storeSelection()   # of the view that is left; the other one shows what was selected in it
        self.movieView = not self.movieView
        config.plugins.plutotvcockpit.movie_view.value = self.movieView
        self.saveViewSettings()
        self.updateViewButton()
        self.history = []
        self.vinfo = ""
        self.description = ""
        self.eptitle = ""
        self.epinfo = ""
        self["info"].setText("")
        self["vtitle"].setText("")
        self["poster"].hide()
        self["posterBG"].hide()
        self.showView()

    def showView(self):
        """Top level of the current view: the categories, or all titles in one list."""
        country = " - " + COUNTRY_NAMES[self.country] if self.country in COUNTRY_NAMES else ""
        if self.movieView:
            self.films = self.allfilms
            self.titlemenu = _("All movies") + country
            entries = self.filmEntries(self.films)
        else:
            self.titlemenu = _("VOD Menu") + country
            entries = [self["feedlist"].listentry(key.decode("utf-8"), "menu", "") for key in self.menu]
        self["feedlist"].setList(entries)
        self["feedlist"].moveToIndex(0)
        self.selectStored()
        self.title = _("PlutoTV") + " - " + self.titlemenu

    def selectStored(self):
        """Select the entry of the list of the current view that was selected when it was left."""
        if self.movieView:
            wanted = config.plugins.plutotvcockpit.last_movie.value
            ids = [film[0] for film in self.films]
        else:
            wanted = config.plugins.plutotvcockpit.last_category.value
            ids = [key.decode("utf-8") for key in self.menu]
        if wanted and wanted in ids:
            self["feedlist"].moveToIndex(ids.index(wanted))

    def storeSelection(self):
        """Remember the entry of the list of the current view that the selection is in: the category, or the title in
        the list of all titles. Inside a category or a series that is the one it was opened from."""
        try:
            if not (selection := self.getSelection()):
                return
            _index, name, __type, _id = selection
            if self.movieView:
                if __type in {"movie", "series"}:
                    item = _id
                elif __type in {"seasons", "episode"} and 0 <= getattr(self, "_series_index", -1) < len(self.films):
                    item = self.films[self._series_index][0]
                else:
                    return
                config.plugins.plutotvcockpit.last_movie.value = item
            else:
                item = name if __type == "menu" else self.categoryName
                if item:
                    config.plugins.plutotvcockpit.last_category.value = item
        except Exception as exc:  # pylint: disable=broad-except  # remembering the selection must not stop the screen from closing
            logger.debug("selection not stored: %s", exc)

    def saveSelection(self):
        """Store the selection and write the settings that remember the view and the selection."""
        self.storeSelection()
        self.saveViewSettings()

    @staticmethod
    def saveViewSettings():
        for element in (config.plugins.plutotvcockpit.movie_view, config.plugins.plutotvcockpit.last_movie, config.plugins.plutotvcockpit.last_category):
            element.save()
        configfile.save()

    def buildlist(self, category):
        name = category["name"].encode("utf-8")
        if name not in self.lvod:  # categories can repeat; list each name (and its movies) only once
            self.lvod[name] = []
            self.menu.append(name)
        known_ids = {film[0] for film in self.lvod[name]}
        items = category.get("items", [])
        for item in items:
            # film = (_id, name, summary, genre, rating, duration, poster, image, type)
            itemid = item.get("_id", "")
            if not itemid or itemid in known_ids:
                continue
            known_ids.add(itemid)
            itemname = item.get("name", "").encode("utf-8")
            itemsummary = item.get("summary", "").encode("utf-8")
            itemgenre = item.get("genre", "").encode("utf-8")
            itemrating = item.get("rating", "").encode("utf-8")
            itemduration = int(item.get("duration", "0") or "0") // 1000  # in seconds
            itemtype = item.get("type", "")
            seasons = len(item.get("seasonsNumbers", []))
            urls = item.get("stitched", {}).get("urls", [])
            url = urls[0].get("url", "") if urls else ""

            itemposter, itemimage = pickBestImage(item.get("covers", []))
            self.lvod[name].append((itemid, itemname, itemsummary, itemgenre, itemrating, itemduration, itemposter, itemimage, itemtype, url, seasons))

    def buildchapters(self, chapters):
        self.chapters.clear()
        items = chapters.get("seasons", [])
        for item in items:
            chs = item.get("episodes", [])
            for ch in chs:
                if (season := str(ch.get("season", 0))) != "0":
                    if season not in self.chapters:
                        self.chapters[season] = []
                    _id = ch.get("_id", "")
                    name = ch.get("name", "").encode("utf-8")
                    number = str(ch.get("number", 0))
                    summary = ch.get("description", "").encode("utf-8")
                    rating = ch.get("rating", "")
                    duration = ch.get("duration", 0) // 1000
                    genre = ch.get("genre", "").encode("utf-8")
                    imgs = ch.get("covers", [])
                    urls = ch.get("stitched", {}).get("urls", [])
                    url = urls[0].get("url", "") if urls else ""

                    itemposter, itemimage = pickBestImage(imgs)
                    self.chapters[season].append((_id, name, number, summary, rating, duration, genre, itemposter, itemimage, url))

    def getSelection(self):
        index = self["feedlist"].getSelectionIndex()
        if current := self["feedlist"].getCurrent():
            data = current[0]
            return index, data[0], data[1], data[2]
        return None

    def action(self):
        if not (selection := self.getSelection()):
            return
        self.lastAction = self.action
        index, name, __type, _id = selection
        menu = []
        menuact = self.titlemenu
        if __type == "menu":
            self.categoryName = name
            self.films = self.lvod[self.menu[index]]
            menu = self.filmEntries(self.films)
            self["feedlist"].moveToIndex(0)
            self["feedlist"].setList(menu)
            self.titlemenu = name
            self.title = _("PlutoTV") + " - " + self.titlemenu
            self.history.append((index, menuact))
        elif __type == "series":
            self["loading"].show()
            self._series_name = name
            self._series_index = index
            self._series_menuact = menuact
            threads.deferToThread(plutoRequest.getVOD, _id, self.country).addCallback(self._getVODCallback)
        elif __type == "seasons":
            for key in self.chapters[_id]:
                sname = key[1].decode("utf-8")
                stype = "episode"
                sid = key[0]
                menu.append(self["feedlist"].listentry(_("Episode") + " " + key[2] + ". " + sname, stype, _id, key[0], key[5]))
            self["feedlist"].setList(menu)
            self.titlemenu = menuact.split(" - ")[0] + " - " + name
            self.title = _("PlutoTV") + " - " + self.titlemenu
            self.history.append((index, menuact))
            self["feedlist"].moveToIndex(0)
        elif __type == "movie":
            film = self.films[index]
            sid = film[0]
            name = film[1].decode("utf-8")
            url = film[9]
            self.playVOD(name, sid, url, film[2].decode("utf-8"), film[5])
        elif __type == "episode":
            film = self.chapters[_id][index]
            sid = film[0]
            name = film[1].decode("utf-8")
            url = film[9]
            self.playVOD(name, sid, url, film[3].decode("utf-8"), film[5])

    def back(self):
        if not (selection := self.getSelection()):
            return
        self.lastAction = self.back
        _index, _name, __type, _id = selection
        menu = []
        if self.history:
            hist = self.history[-1][0]
            histname = self.history[-1][1]
            if __type in {"movie", "series"}:
                for key in self.menu:
                    menu.append(self["feedlist"].listentry(key.decode("utf-8"), "menu", ""))
                self["vtitle"].text = ""
                self.vinfo = ""
                self.description = ""
            elif __type == "seasons":
                menu = self.filmEntries(self.films)
            elif __type == "episode":
                for key in list(self.chapters.keys()):
                    sname = str(key)
                    stype = "seasons"
                    sid = str(key)
                    menu.append(self["feedlist"].listentry(_("Season") + " " + sname, stype, sid))
            self["feedlist"].setList(menu)
            self.history.pop()
            self["feedlist"].moveToIndex(hist)
            self.titlemenu = histname
            self.title = _("PlutoTV") + " - " + self.titlemenu
            if not self.history:
                self["poster"].hide()

    def _getVODCallback(self, chapters):
        self.buildchapters(chapters)
        menu = []
        for key in list(self.chapters.keys()):
            menu.append(self["feedlist"].listentry(_("Season") + " " + key, "seasons", key))
        self["feedlist"].setList(menu)
        self.titlemenu = self._series_name + " - " + _("Seasons")
        self.title = _("PlutoTV") + " - " + self.titlemenu
        self.history.append((self._series_index, self._series_menuact))
        self["feedlist"].moveToIndex(0)
        self["loading"].hide()

    def playVOD(self, name, sid, url=None, description="", duration=0):
        if url:
            self._play_name = name
            self._play_sid = sid
            self._play_description = description
            self._play_duration = duration  # seconds, from PlutoTV's movie info
            threads.deferToThread(plutoRequest.buildVodStreamURL, url, self.country).addCallback(self._playVODCallback)

    def _playVODCallback(self, url):
        if url and self._play_name:
            string = f"4097:0:0:0:0:0:0:0:0:0:{quote(url)}:{quote(self._play_name)}"
            reference = eServiceReference(string)
            if "m3u8" in url.lower() or "127.0.0.1" in url:
                self.service_center.setMovie(self._play_name, self._play_description, self._play_duration)
                self.last_service = self.session.nav.getCurrentlyPlayingServiceOrGroup()
                if config.plugins.plutotvcockpit.vod_record.value and self.startVodRecording(reference):
                    return   # vodRecordingCheck opens the player on the file
                self.openPlayer(reference)

    def openPlayer(self, reference):
        """Play the HLS stream of the proxy."""
        self.session.openWithCallback(self._playerClosed, PRSCockpitPlayer, reference, self._play_sid, resumePointsInstance, self.service_center, config.plugins.plutotvcockpit)

    def openFilePlayer(self, recording):
        """Play the file that is recorded (see VodRecording) with the regular player of the recordings."""
        self.session.openWithCallback(self._playerClosed, VodPlayer, recording.reference(), self._play_sid, resumePointsInstance, self.service_center, config.plugins.plutotvcockpit, recording)

    def startVodRecording(self, reference):
        """Record the asset to a file and play that (see VodRecording) instead of the HLS stream of the proxy. False
        if it can't be recorded: the stream is played then."""
        self.stopVodRecording()
        recording = VodRecording()
        try:
            started = recording.start(reference, self._play_name, self._play_duration, self._play_description)
        except Exception as exc:  # pylint: disable=broad-except  # whatever goes wrong, the title still plays
            logger.error("the recording can't be started: %s", exc, exc_info=True)
            started = False
        if not started:
            recording.stop(keep_channel=True)
            return False
        self.vodRecording = recording
        self.vodRecordingReference = reference
        self.vodRecordingDeadline = monotonic() + VOD_RECORDING_START_TIMEOUT
        self["loading"].setText(_("Buffering stream..."))
        self["loading"].show()
        self.vodRecordingTimer.start(250, False)
        return True

    def endBuffering(self):
        """Hide the message of the wait for the first seconds of the recording, and give the label its text for the
        loading of the lists back."""
        self["loading"].hide()
        self["loading"].setText(_("Loading data... Please wait"))

    def vodRecordingCheck(self):
        recording = self.vodRecording
        if recording is None:
            self.vodRecordingTimer.stop()
        elif recording.ready():
            self.vodRecordingTimer.stop()
            self.endBuffering()
            logger.debug("%s bytes recorded, playing %s", recording.size(), recording.path)
            self.openFilePlayer(recording)
        elif monotonic() > self.vodRecordingDeadline:
            # nothing (or too little) came: play the stream, the way it was done before
            self.vodRecordingTimer.stop()
            logger.debug("%s bytes recorded after %s s, playing the stream", recording.size(), VOD_RECORDING_START_TIMEOUT)
            self.stopVodRecording(keep_channel=True)
            self.endBuffering()
            self.openPlayer(self.vodRecordingReference)

    def stopVodRecording(self, keep_channel=False):
        """Let go of the recording of the asset: a temporary one is stopped, its file deleted and the channel of the
        proxy closed (not with *keep_channel*: the stream of it is played next); one that was made permanent with the
        record key goes on to the end of the asset (see VodRecording.stop)."""
        self.vodRecordingTimer.stop()
        recording, self.vodRecording = self.vodRecording, None
        if recording:
            recording.stop(keep_channel)

    def _playerClosed(self, *_args):
        if self.vodRecording:
            self.session.nav.stopService()   # the file must not play on while it is deleted
        self.stopVodRecording()
        self.session.nav.playService(self.last_service)
        self["feedlist"].refresh()  # pick up the resume point saved on leaving the player

    def green(self):
        locations = [x for x in getselectedcountries() if x] or [config.plugins.plutotvcockpit.country.value]
        if len(locations) <= 1:
            self.session.openWithCallback(self.endupdateLive, PlutoTVDownload)
            return
        choices = [(_("All"), None)] + [(COUNTRY_NAMES.get(cc, cc), cc) for cc in locations]
        self.session.openWithCallback(
            self.greenChoice,
            ChoiceBox,
            title=_("Select a Live-TV bouquet to update"),
            list=choices,
            keys=[]
        )

    def greenChoice(self, result=None):
        if result is None:
            return
        self.session.openWithCallback(self.endupdateLive, PlutoTVDownload, locations=[result[1]] if result[1] else None)

    def endupdateLive(self, _ret=None):
        if _ret:
            countries = ", ".join(COUNTRY_NAMES.get(cc, cc) for cc in _ret)
            self.session.openWithCallback(self.updatebutton, MessageBox, _("The Pluto TV bouquets for %s in your channel list have been updated.\n\nThey will now be rebuilt automatically every 5 hours.") % countries, type=MessageBox.TYPE_INFO, timeout=10)
        else:
            self.updatebutton()

    def updatebutton(self, _ret=None):
        with open("/etc/enigma2/bouquets.tv", "r", encoding="utf-8") as f:
            bouquets = f.read()
        nodata = loadNoDataLocations(NODATA_FILE)
        # A selected country missing its bouquet is only "still needs updating" if
        # we haven't actually tried it yet - one that came back empty last run (e.g.
        # geo-blocked) is settled, since pressing GREEN again wouldn't change that.
        if fileExists(TIMER_FILE) and all(((BOUQUET_FILE % cc) in bouquets or cc in nodata) for cc in [x for x in getselectedcountries() if x]):
            with open(TIMER_FILE, "r", encoding="utf-8") as f:
                last = float(f.read().replace("\n", "").replace("\r", ""))
            updated = strftime(" %x %H:%M", localtime(int(last)))
            self["key_green"].text = _("Update Live-TV Bouquet")
            self["updated"].text = _("Live-TV Bouquet last updated:") + updated
        elif "plutotvcockpit" in bouquets:
            self["key_green"].text = _("Update Live-TV Bouquet")
            self["updated"].text = _("Live-TV Bouquet needs updating. Press GREEN.")
        else:
            self["key_green"].text = _("Create Live-TV Bouquet")
            self["updated"].text = ""

    def exit(self, *_args, **_kwargs):
        if self.history:
            self.back()
        else:
            self.close()

    def MDB(self):
        if not (selection := self.getSelection()):
            return
        _index, name, __type, _id = selection
        if __type in {"movie", "series"} and self.mdb:
            if self.mdb == "tmdb":
                from Plugins.Extensions.tmdb.tmdb import tmdbScreen
                self.session.open(tmdbScreen, name, 2)
            else:
                from Plugins.Extensions.IMDb.plugin import IMDB
                self.session.open(IMDB, name, False)

    def loadSetup(self):
        def loadSetupCallback(_result=None):
            if config.plugins.plutotvcockpit.country.value != self.country:
                self.country = config.plugins.plutotvcockpit.country.value
                self.initialise()
                self.getCategories()
        self.session.openWithCallback(loadSetupCallback, PlutoSetup)

    def switchCountry(self):
        def switchCountryCallback(result=None):
            if result and result[1] != self.country:
                self.country = result[1]
                self.initialise()
                self.getCategories()
        self.session.openWithCallback(
            switchCountryCallback,
            ChoiceBox,
            title=_("VOD Country Selection"),
            text=_("Temporarily switch the VoD list to another country"),
            list=list(zip(config.plugins.plutotvcockpit.country.description, config.plugins.plutotvcockpit.country.choices)),
            selection=config.plugins.plutotvcockpit.country.choices.index(self.country),
            keys=[]
        )

    def close(self, *_args, **_kwargs):
        if self.updatebutton in Silent.afterUpdate:
            Silent.afterUpdate.remove(self.updatebutton)
        Screen.close(self)
