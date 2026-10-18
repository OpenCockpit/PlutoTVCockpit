# Copyright (C) 2026 by xcentaurix
# Copyright (C) 2021 by Team OpenSPA
# License: GNU General Public License v3.0

import ipaddress
import random

from Components.config import ConfigDirectory, ConfigSelection, ConfigSubsection, ConfigYesNo, config

from . import _
from .CountryCodes import ISO3166
from .Variables import NUMBER_OF_LIVETV_BOUQUETS
from .CockpitTVConfig import setupLocationSlots


# Geo-spoofing IP ranges per country code, used for X-Forwarded-For headers.
# Each value is the /24 containing a previously-verified anchor address for
# that country. pickForwardIP() draws a random host from the range instead
# of always sending the one exact address, so the header value isn't pinned
# to a single IP that could get deallocated or rate-limited over time.
X_FORWARD_NETS = {
    "us": "185.236.200.0/24",
    "gb": "185.199.220.0/24",
    "de": "85.214.132.0/24",
    "es": "88.26.241.0/24",
    "ca": "192.206.151.0/24",
    "br": "177.47.27.0/24",
    "mx": "200.68.128.0/24",
    "fr": "176.31.84.0/24",
    "at": "2.18.68.0/24",
    "ch": "5.144.31.0/24",
    "it": "5.133.48.0/24",
    "ar": "104.103.238.0/24",
    "co": "181.204.4.0/24",
    "cr": "138.122.24.0/24",
    "pe": "190.42.0.0/24",
    "ve": "103.83.193.0/24",
    "cl": "161.238.0.0/24",
    "bo": "186.27.64.0/24",
    "sv": "190.53.128.0/24",
    "gt": "190.115.2.0/24",
    "hn": "181.115.0.0/24",
    "ni": "186.76.0.0/24",
    "pa": "168.77.0.0/24",
    "uy": "179.24.0.0/24",
    "ec": "181.196.0.0/24",
    "py": "177.250.0.0/24",
    "do": "152.166.0.0/24",
    "se": "185.39.146.0/24",
    "dk": "80.63.84.0/24",
    "no": "84.214.150.0/24",
    "au": "144.48.37.0/24",
    "fi": "85.194.236.0/24",
}

_forwardIPCache = {}


def pickForwardIP(country):
    """Return an X-Forwarded-For address for *country*, or None if unmapped.

    Picked once per country and cached for the life of the process (not
    re-rolled per call), so a single boot/API session doesn't see its
    apparent origin IP change mid-flight. Restarting the plugin re-rolls
    it, spreading requests across the subnet over time.
    """
    net = X_FORWARD_NETS.get(country)
    if net is None:
        return None
    if country not in _forwardIPCache:
        _forwardIPCache[country] = str(random.choice(list(ipaddress.ip_network(net).hosts())))
    return _forwardIPCache[country]


# ISO3166 is sorted in English, sorted will sort by locale.
COUNTRY_NAMES = {cc: country[0].split("(")[0].strip() for country in sorted(ISO3166) if (cc := country[1].lower()) in X_FORWARD_NETS}

TSIDS = {cc: f"{i:X}" for i, cc in enumerate(COUNTRY_NAMES, 1)}


# --- Config subsection ---------------------------------------------------

config.plugins.plutotv = ConfigSubsection()
config.plugins.plutotv.country = ConfigSelection(default="local", choices=[("local", _("Local"))] + list(COUNTRY_NAMES.items()))
# How far ahead the guide is fetched. Pluto has about 28 hours of it.
config.plugins.plutotv.epg_hours = ConfigSelection(default="24", choices=[(str(hours), _("%d hours") % hours) for hours in range(4, 29, 4)])
config.plugins.plutotv.picons = ConfigSelection(default="snp", choices=[("snp", _("service name")), ("srp", _("service reference")), ("", _("None"))])
# On: the bouquet holds lazy pluto:// addresses, resolved by StreamProxyCockpit when a channel is played or recorded.
# Off: the bouquet holds the addresses of the stream source (live_tv_mode).
config.plugins.plutotv.stream_proxy = ConfigYesNo(default=True)
# The stream source, with and without the stream proxy: the stitcher of Pluto itself (with a token of its own) or the
# jmp2.uk redirect.
if config.plugins.plutotv.content.stored_values.get("live_tv_mode") == "mjh":
    # i.mjh.nz was a third source: its playlist is gone, and it listed the same channels with the same JMP2 addresses.
    # Changed before the setting is created: a stored value that is not one of its choices makes it raise.
    config.plugins.plutotv.content.stored_values["live_tv_mode"] = "jmp2"
config.plugins.plutotv.live_tv_mode = ConfigSelection(default="stitcher", choices=[("stitcher", _("Stitcher")), ("jmp2", _("JMP2"))])
config.plugins.plutotv.auto_update_check = ConfigSelection(default="yes", choices=[("yes", _("Yes")), ("no", _("No"))])
config.plugins.plutotv.config_folder = ConfigDirectory(default="/etc/enigma2")
# Read by CockpitPlayer; not offered in the setup screen. PRSCockpitPlayer implements the resume prompt itself.
# Off: LiveProxyUnmuxed serves VOD as a sliding live-style window that always starts at the first segment,
# so seeking to a saved position can't be honoured. Saving the position (list progress) is unaffected.
config.plugins.plutotv.movie_resume_at_last_pos = ConfigYesNo(default=False)
config.plugins.plutotv.movie_start_position = ConfigSelection(default="beginning", choices=[("beginning", _("beginning"))])


# --- Live-TV country config items -----------------------------------------

getselectedcountries = setupLocationSlots(config.plugins.plutotv, "live_tv_country", COUNTRY_NAMES, NUMBER_OF_LIVETV_BOUQUETS, _("None"))
