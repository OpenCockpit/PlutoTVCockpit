# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0


import os
import re
import subprocess

from Components.Console import Console
from Screens.MessageBox import MessageBox
from Screens.Standby import TryQuitMainloop, QUIT_RESTART
from Tools import Notifications
from Tools.Directories import fileReadLines
from twisted.internet import threads

from .Version import PLUGIN
from . import _
from .Debug import logger


COCKPIT_FEED_NAME = "cockpit-all"


def _version_sort_key(version):
    version = str(version or "")
    key = []
    for part in re.split(r"([0-9]+)", version):
        if not part:
            continue
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part.lower()))
    return tuple(key)


def _isVersionNewer(candidate_version, current_version):
    if not candidate_version or not current_version:
        return False
    if candidate_version == current_version:
        return False
    return _version_sort_key(candidate_version) > _version_sort_key(current_version)


def _opkgListsDir():
    for line in fileReadLines("/etc/opkg/opkg.conf") or []:
        parts = line.split()
        if len(parts) >= 2 and parts[-2] == "lists_dir":
            return parts[-1]
    return "/var/lib/opkg/lists"


def getInstalledPackageVersion(package_name):
    control_file = os.path.join("/var/lib/opkg/info", f"{package_name}.control")
    if not os.path.exists(control_file):
        return None

    for line in fileReadLines(control_file) or []:
        if line.startswith("Version:"):
            return line.split("Version:", 1)[1].strip().split("+", 1)[0]

    return None


def _seedCockpitFeedConf():
    """Run this plugin's own bundled feed_install.sh (if present) to
    register the cockpit-all opkg feed, if opkg doesn't already have that
    feed's list file - a first-run/reflashed box case where "opkg update"
    below would otherwise never even attempt to fetch the cockpit feed at
    all, since opkg was never told the feed's source URL exists in the
    first place. No-op (not an error) on a plugin build that doesn't
    bundle feed_install.sh at all, or on any box where the feed is
    already registered by some other means (e.g. provisioned into
    /etc/opkg/ some other way) - this is only ever a fallback.
    """
    if os.path.exists(os.path.join(_opkgListsDir(), COCKPIT_FEED_NAME)):
        return

    feed_install_sh = f"/usr/lib/enigma2/python/Plugins/Extensions/{PLUGIN}/feed_install.sh"
    if not os.path.exists(feed_install_sh):
        return

    try:
        subprocess.run([feed_install_sh], check=False)
    except OSError as exc:
        logger.debug("failed to run %s to seed the %s opkg feed: %r", feed_install_sh, COCKPIT_FEED_NAME, exc)


def _refreshOpkgLists():
    """Update the opkg package lists (seeding the cockpit feed if need be). False if that isn't possible."""
    opkg = "/usr/bin/opkg"
    if not os.path.exists(opkg):
        return False

    _seedCockpitFeedConf()

    try:
        subprocess.run([opkg, "update"], capture_output=True, text=True, check=False)
    except OSError:
        return False
    return True


def getAvailablePackageVersion(package_name):
    if not _refreshOpkgLists():
        return None
    return _availablePackageVersionFromLists(package_name)


def _availablePackageVersionFromLists(package_name):
    """The version of *package_name* in the cockpit feed list as it is now (no "opkg update")."""
    latest_version = None
    in_package = False

    for line in fileReadLines(os.path.join(_opkgListsDir(), COCKPIT_FEED_NAME)) or []:
        if line.startswith("Package:"):
            in_package = line.split(":", 1)[1].strip() == package_name
        elif in_package and line.startswith("Version:"):
            latest_version = line.split(":", 1)[1].strip()
            in_package = False

    if latest_version is None:
        return None

    return {
        "path": None,
        "version": latest_version,
    }


def askForRestart(session, text, default=True, timeout=-1, notification=False):
    """Ask whether to restart Enigma2 now, and restart it if the answer is yes.

    Used after a plugin update (see checkPluginUpdateAndOpen) and by the TV plugins when the stream addresses of
    their channels changed. *default* is the answer a *timeout* (seconds, -1 = none) gives.

    *notification* is for a caller that is a background job and doesn't know what is on screen: the question is
    opened on top of whatever screen is running (a plugin screen, the infobar), so it is seen at once. Enigma2
    refuses to open a modal dialog while no screen is running (a RuntimeError, see Session.open), e.g. in the moment
    a screen is closing; the question is queued as a notification then. Notifications are only shown by the
    infobar, which is why they aren't used right away: with a plugin screen open the question would wait there
    until that screen is left.

    No second confirmation from TryQuitMainloop itself (see Screens.Standby.TryQuitMainloop - it only re-prompts
    when there's an active recording/timeshift/background job to warn about) - same call pattern SkinSelector/
    ClientMode/MultiBootSelector all use after their own restart-confirm prompt."""
    def _restartConfirmed(answer):
        if answer:
            session.open(TryQuitMainloop, retvalue=QUIT_RESTART)

    if notification and not session.in_exec:
        Notifications.AddNotificationWithCallback(_restartConfirmed, MessageBox, text, type=MessageBox.TYPE_YESNO, default=default, timeout=timeout)
    else:
        session.openWithCallback(_restartConfirmed, MessageBox, text, type=MessageBox.TYPE_YESNO, default=default, timeout=timeout)


def checkPluginUpdates(package_names):
    """The packages of *package_names* that have a newer version in the feed, with one "opkg update" for all of them.
    A list of {"package", "installed", "version", "path"}, in the order of *package_names*; empty if none."""
    if not _refreshOpkgLists():
        return []

    updates = []
    for package_name in package_names:
        installed_version = getInstalledPackageVersion(package_name)
        available_info = _availablePackageVersionFromLists(package_name)
        if available_info and _isVersionNewer(available_info["version"], installed_version):
            updates.append({
                "package": package_name,
                "installed": installed_version,
                "version": available_info["version"],
                "path": available_info["path"],
            })
    return updates


def checkPluginUpdate(package_name):
    updates = checkPluginUpdates([package_name])
    return updates[0] if updates else None


def checkPluginUpdateAndOpen(session, package_name, plugin_name, main_screen, auto_update_config, also_check=(), **screen_kwargs):
    """Open *main_screen* immediately, then separately check *package_name*
    for a newer opkg version and prompt to install it if one is found.

    *auto_update_config* is a ConfigYesNo-style value (e.g.
    config.plugins.plutotv.auto_update_check) - the check itself is skipped
    entirely unless its value is "yes"; main_screen still opens either way.
    *also_check* is a sequence of (package name, display name) of further packages the plugin depends on (for
    the TV plugins StreamProxyCockpit): they are checked in the same background job, with the same single
    "opkg update", and installed together with the plugin if they have a newer version. They are not told apart:
    the question and the restart question are the same, and name the plugin.
    **screen_kwargs are forwarded to the session.open(main_screen, ...)
    call, for a caller whose main screen needs constructor arguments.

    Extracted from PlutoTVCockpit/SamsungTVCockpit/RakutenTVCockpit's
    near-identical plugin.py `system()` entry points, which differed only
    in the opkg package name, the display name in the confirmation
    prompt, and which Screen to open - see each plugin's own plugin.py
    for the call site.

    checkPluginUpdates() runs in a background thread via
    threads.deferToThread - it shells out to "opkg update" (refreshing
    every configured feed, not just this plugin's own) with no timeout,
    which can take a long time or hang outright on a slow/unreachable
    network. main_screen is opened up front, before that check even
    starts, so the user isn't stuck staring at the menu waiting for an
    opkg update to finish before the screen they asked for shows up - any
    update-available prompt this finds just stacks on top of main_screen
    once the check resolves, whenever that is. Both branches of the
    deferred result (found an update, or the check itself failed) still
    need to get back onto the GUI thread to call session.open()/
    session.openWithCallback() - Twisted's Deferred callbacks already run
    on the reactor thread, which is the same thread as Enigma2's own main
    loop, so no extra dispatch is needed for that part.

    After a successful install, this asks (default yes) whether to
    restart Enigma2 now rather than calling plugins.reloadPlugins():
    reloadPlugins() only re-scans for plugin.py files that were never
    imported before in this process (added/removed plugins) - Python's
    own import system caches already-imported modules in sys.modules, so
    calling it again on an already-running plugin's package (this one,
    right now) never re-executes a single line of the just-installed
    code. A real restart is the only way to actually pick it up.
    """
    session.open(main_screen, **screen_kwargs)

    if auto_update_config.value != "yes":
        return

    def _updateChecked(updates):
        if not updates:
            return

        def _installUpdate(answer):
            if not answer:
                return

            def _installFinished(_data, _retval, _extra_args):
                askForRestart(session, _("%s has been updated. Restart Enigma2 now to use the new version?") % plugin_name)

            install_cmd = "opkg install " + " ".join(update["path"] or update["package"] for update in updates)
            Console().ePopen(install_cmd, _installFinished)

        # the packages of also_check belong to the plugin: one question that names the plugin, no version
        session.openWithCallback(
            _installUpdate,
            MessageBox,
            _("A newer %s package is available. Do you want to install it now?") % plugin_name,
            type=MessageBox.TYPE_YESNO,
            default=False
        )

    def _updateCheckFailed(failure):
        logger.debug("checkPluginUpdate(%s) failed: %s", package_name, failure)

    threads.deferToThread(checkPluginUpdates, [package_name] + [package for package, _name in also_check]).addCallbacks(_updateChecked, _updateCheckFailed)
