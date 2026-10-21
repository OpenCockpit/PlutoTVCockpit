# Copyright (C) 2026 by xcentaurix
# Copyright (C) 2021 by Team OpenSPA
# License: GNU General Public License v3.0

"""X-Forwarded-For addresses for the FAST-channel TV Cockpit plugins (Pluto TV, Samsung TV Plus).

The services and their CDNs decide by the address a request comes from which content they serve, and some of them
honour an X-Forwarded-For header for it. A plugin sends the address of the country whose channels it wants as a
header of every request of the channel: the API calls, the playlists and the segments.
"""

import ipaddress
import random

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


def rerollForwardIP(country):
    """Forget the address picked for *country* and pick another one, for the next request.

    Pluto's stitcher refuses the session token of a few of the addresses of a range with a 401 (about one in twenty
    of the US one). The address is picked once per process, so a bad draw would stop every channel until the next
    restart of Enigma2.
    """
    _forwardIPCache.pop(country, None)
    return pickForwardIP(country)
