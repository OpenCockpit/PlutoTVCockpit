# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0

"""Let a recording that the Cockpit plugins make themselves count as a recording for the front panel led (and the other
users of RecordState: the REC labels of the skins).

A timeshift recording (a copy job) and a recording of a stream of a plugin (a simulated record service of the proxy) are no
recordings of the navigation: session.nav.getRecordings() does not know them, so the "recording" state of the session
(session.screen["RecordState"]) stays off and the led does not blink. The RecordState source of the session is switched to a
subclass that adds the number of these recordings to the number of real recordings. The source then tells its downstream
elements (the FrontpanelLed renderers of SessionGlobals among them) that it changed, so they set the led pattern the way they
do for a normal recording, and set it back when the last one has ended, whatever else (a real recording, standby) is going on.

Every plugin has a copy of this module (it is a file of Common): what is shared is the state on the RecordState instance
(led_owners), not a class, so that the recordings of several plugins add up."""

from Components.Sources.RecordState import RecordState
from Screens.InfoBar import InfoBar
from .Debug import logger


class RecordLedState(RecordState):
    """RecordState that counts the recordings of the plugins (the owners in led_owners) as well."""

    def getRecordsRunning(self):
        return self._records + len(self.led_owners)

    def setRecordsRunning(self, value):
        # RecordState.gotRecordEvent stores the number of real recordings here
        self._records = value

    records_running = property(getRecordsRunning, setRecordsRunning)


def _getRecordState(session):
    if session is None:
        session = getattr(InfoBar.instance, "session", None)
    try:
        record_state = session.screen["RecordState"]
    except Exception as e:
        logger.debug("no RecordState: %s", str(e))
        return None
    if getattr(record_state, "led_owners", None) is None:
        # the instance attribute of the original class would hide the property of the subclass
        records = record_state.__dict__.pop("records_running", 0)
        record_state.__class__ = RecordLedState
        record_state._records = records
        record_state.led_owners = set()
    return record_state


def startRecordingLed(owner, session=None):
    """A recording of a plugin runs: *owner* stands for it (any object, the same one is given to stopRecordingLed)."""
    record_state = _getRecordState(session)
    if record_state is not None and owner not in record_state.led_owners:
        logger.info("owner: %s", owner)
        record_state.led_owners.add(owner)
        record_state.changed((record_state.CHANGED_ALL,))


def stopRecordingLed(owner, session=None):
    """The recording of *owner* has ended (also when it never started)."""
    record_state = _getRecordState(session)
    if record_state is not None and owner in record_state.led_owners:
        logger.info("owner: %s", owner)
        record_state.led_owners.discard(owner)
        record_state.changed((record_state.CHANGED_ALL,))
