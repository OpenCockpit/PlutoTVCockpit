# Copyright (C) 2026 by xcentaurix
# Copyright (C) 2021 by Team OpenSPA
# License: GNU General Public License v3.0

from Components.config import ConfigDirectory, ConfigSelection, ConfigYesNo, config

from . import _
from . import Debug  # noqa: F401, pylint: disable=unused-import
from .CockpitForwardIP import X_FORWARD_NETS
from .CountryCodes import ISO3166
from .Variables import NUMBER_OF_LIVETV_BOUQUETS
from .CockpitTVConfig import setupLocationSlots


# ISO3166 is sorted in English, sorted will sort by locale.
COUNTRY_NAMES = {cc: country[0].split("(")[0].strip() for country in sorted(ISO3166) if (cc := country[1].lower()) in X_FORWARD_NETS}

TSIDS = {cc: f"{i:X}" for i, cc in enumerate(COUNTRY_NAMES, 1)}


# --- Config subsection ---------------------------------------------------

# Debug creates config.plugins.plutotvcockpit (with debug_log_level): the settings below join that subsection, it must
# not be created again here.
# The settings used to be config.plugins.plutotv: take over what is stored there and drop it, so it isn't saved again.
if oldValues := config.plugins.content.stored_values.pop("plutotv", None):
    for key, value in oldValues.items():
        config.plugins.plutotvcockpit.content.stored_values.setdefault(key, value)
config.plugins.plutotvcockpit.country = ConfigSelection(default="local", choices=[("local", _("Local"))] + list(COUNTRY_NAMES.items()))
# How far ahead the guide is fetched. Pluto has about 28 hours of it.
config.plugins.plutotvcockpit.epg_hours = ConfigSelection(default="24", choices=[(str(hours), _("%d hours") % hours) for hours in range(4, 29, 4)])
config.plugins.plutotvcockpit.picons = ConfigSelection(default="snp", choices=[("snp", _("service name")), ("srp", _("service reference")), ("", _("None"))])
# On: the bouquet holds lazy pluto:// addresses, resolved by StreamProxyCockpit when a channel is played or recorded.
# Off: the bouquet holds the addresses of the stream source (live_tv_mode).
config.plugins.plutotvcockpit.stream_proxy = ConfigYesNo(default=True)
# The stream source, with and without the stream proxy: the stitcher of Pluto itself (with a token of its own) or the
# jmp2.uk redirect.
if config.plugins.plutotvcockpit.content.stored_values.get("live_tv_mode") == "mjh":
    # i.mjh.nz was a third source: its playlist is gone, and it listed the same channels with the same JMP2 addresses.
    # Changed before the setting is created: a stored value that is not one of its choices makes it raise.
    config.plugins.plutotvcockpit.content.stored_values["live_tv_mode"] = "jmp2"
config.plugins.plutotvcockpit.live_tv_mode = ConfigSelection(default="stitcher", choices=[("stitcher", _("Stitcher")), ("jmp2", _("JMP2"))])
config.plugins.plutotvcockpit.auto_update_check = ConfigSelection(default="yes", choices=[("yes", _("Yes")), ("no", _("No"))])
config.plugins.plutotvcockpit.config_folder = ConfigDirectory(default="/etc/enigma2")
# Read by CockpitPlayer; not offered in the setup screen. PRSCockpitPlayer implements the resume prompt itself.
# Off: LiveProxyUnmuxed serves VOD as a sliding live-style window that always starts at the first segment,
# so seeking to a saved position can't be honoured. Saving the position (list progress) is unaffected.
config.plugins.plutotvcockpit.movie_resume_at_last_pos = ConfigYesNo(default=False)
config.plugins.plutotvcockpit.movie_start_position = ConfigSelection(default="beginning", choices=[("beginning", _("beginning"))])


# --- Live-TV country config items -----------------------------------------

getselectedcountries = setupLocationSlots(config.plugins.plutotvcockpit, "live_tv_country", COUNTRY_NAMES, NUMBER_OF_LIVETV_BOUQUETS, _("None"))
