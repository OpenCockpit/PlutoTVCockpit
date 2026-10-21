# Copyright (C) 2026 by xcentaurix
# License: GNU General Public License v3.0

import os
import re

from Components.ActionMap import HelpableActionMap
from Components.config import config
from Components.Sources.StaticText import StaticText
from Screens.Setup import Setup

from . import _
from .CockpitTVDownload import NOPROXY_SUFFIX, removeBouquetVariants
from .PlutoTVDownload import PlutoTVDownload, Silent
from .PiconFetcher import PiconFetcher
from .Variables import BOUQUET_FILE, NUMBER_OF_LIVETV_BOUQUETS
from .Version import VERSION


class PlutoSetup(Setup):
    def __init__(self, session):
        Setup.__init__(self, session, setup="PlutoTV")
        if "key_yellow" not in self:
            self["key_yellow"] = StaticText()
            self["key_yellowActions"] = HelpableActionMap(self, ["ColorActions"], {
                "yellow": (self.yellow, _("Remove picons")),
            }, prio=1, description=_("PlutoTV Setup Actions"))
        if "key_blue" not in self:
            self["key_blue"] = StaticText()
            self["key_blueActions"] = HelpableActionMap(self, ["ColorActions"], {
                "blue": (self.blue, _("Remove Live-TV Bouquet")),
            }, prio=1, description=_("PlutoTV Setup Actions"))
        self.updateYellowButton()
        self.updateBlueButton()
        self.setTitle(_("PlutoTV Setup") + f" ({VERSION})")

    def createSetup(self):
        configList = []
        configList.append((_("VoD country"), config.plugins.plutotvcockpit.country, _("Select the country that the VoD list will be created for.")))
        configList.append(("---",))
        for n in range(1, NUMBER_OF_LIVETV_BOUQUETS + 1):
            if n == 1 or getattr(config.plugins.plutotvcockpit, "live_tv_country" + str(n - 1)).value:
                configList.append((_("Live-TV bouquet %s") % n, getattr(config.plugins.plutotvcockpit, "live_tv_country" + str(n)), _("Country for which Live-TV bouquet %s will be created.") % n))
        configList.append(("---",))
        configList.append((_("Stream proxy"), config.plugins.plutotvcockpit.stream_proxy, _("Play and record through StreamProxyCockpit, which looks the stream address up when a channel is played and keeps the ad breaks of the streams from disturbing playback and recordings. Off: the bouquet holds the addresses of the stream source below. The bouquet of each setting is kept, so changing it switches between them; one that doesn't exist yet, or is older than 5 hours, is generated.")))
        configList.append((_("Stream source"), config.plugins.plutotvcockpit.live_tv_mode, _("Where the streams come from, with and without the stream proxy. Stitcher is the native Pluto server with a token of its own, JMP2 the jmp2.uk redirect. The bouquet is generated again when this is changed.")))
        configList.append((_("Guide duration"), config.plugins.plutotvcockpit.epg_hours, _("How many hours of programme guide are fetched. Pluto has about 28 hours of it; a longer guide is more data to download and import, so the update takes longer. It applies to the next update.")))
        configList.append((_("Picon type"), config.plugins.plutotvcockpit.picons, _("Using service name picons means they will continue to work even if the service reference changes. Also, they can be shared between channels of the same name that don't have the same service references.")))
        configList.append((_("Automatic update check"), config.plugins.plutotvcockpit.auto_update_check, _("Automatically check for a newer package update when the plugin GUI is opened.")))
        configList.append((_("Data location"), config.plugins.plutotvcockpit.config_folder, _("Location the config data are stored in.")))
        configList.append((_("Log level"), config.plugins.plutotvcockpit.debug_log_level, _("Select the level for debug logging.")))
        self["config"].list = configList

    def _locationConfigChanged(self):
        if config.plugins.plutotvcockpit.country.isChanged():
            return True
        if config.plugins.plutotvcockpit.live_tv_mode.isChanged() and not config.plugins.plutotvcockpit.stream_proxy.value:
            return True   # the bouquet in use holds addresses of the source
        return any(getattr(config.plugins.plutotvcockpit, "live_tv_country" + str(n)).isChanged() for n in range(1, NUMBER_OF_LIVETV_BOUQUETS + 1))

    def keySave(self):
        if config.plugins.plutotvcockpit.live_tv_mode.isChanged():
            removeBouquetVariants(BOUQUET_FILE, (NOPROXY_SUFFIX,))   # the stored ones hold addresses of the old source
        if self._locationConfigChanged():
            # generates the bouquets for the current "Stream proxy" setting as well
            self.session.openWithCallback(lambda *_: Setup.keySave(self), PlutoTVDownload)
        elif config.plugins.plutotvcockpit.stream_proxy.isChanged():
            # the locations without a stored bouquet are generated on top of the setup, like above
            missing = Silent.switchBouquetVariants()
            if missing:
                self.session.openWithCallback(lambda *_: Setup.keySave(self), PlutoTVDownload, missing)
            else:
                Setup.keySave(self)
        else:
            Setup.keySave(self)

    def updateYellowButton(self):
        if os.path.isdir(PiconFetcher(config.plugins.plutotvcockpit.picons).pluginPiconDir):
            self["key_yellow"].text = _("Remove picons")
        else:
            self["key_yellow"].text = ""

    def updateBlueButton(self):
        with open("/etc/enigma2/bouquets.tv", "r", encoding="utf-8") as f:
            bouquets = f.read()
        if "plutotvcockpit" in bouquets:
            self["key_blue"].text = _("Remove Live-TV Bouquet")
        else:
            self["key_blue"].text = ""

    def yellow(self):
        if self["key_yellow"].text:
            PiconFetcher(config.plugins.plutotvcockpit.picons).removeall()
            self.updateYellowButton()

    def blue(self):
        if self["key_blue"].text:
            Silent.stop()
            from enigma import eDVBDB
            eDVBDB.getInstance().removeBouquet(re.escape(BOUQUET_FILE) % ".*")
            removeBouquetVariants(BOUQUET_FILE)
            self.updateBlueButton()
