"""Enrollment, driven by the scanner's own progress notifications.

A faithful port of the state machine in the device's ``admin.html``, because the
two must agree: an installer who enrols from the device's web page and one who
enrols from the Home Assistant panel have to get the same result, and the awkward
parts of this flow were learned from hardware rather than designed.

The parts that look like paranoia and are not:

* **Subscribe before starting.** The sensor pushes the first state almost
  immediately; a listener attached after ``POST …/enroll`` can miss it.
* **Confirm exactly once, but retry the send.** State 35 means "captures done,
  waiting for you to accept". The confirmation *send* can time out while the
  sensor happily proceeds, so the send is retried — but the decision to confirm is
  taken once, or the sensor sees several confirmations for one session.
* **A terminal state ≥ 50 is not proof of failure once we have confirmed.** State
  60 is also reported when the device gave up on something other than our
  confirmation, and the template may already be stored. So the sensor is asked
  what it holds before anyone is told the enrollment failed. Getting this wrong
  produces the worst outcome in the whole feature: a fingerprint that opens the
  door and appears nowhere.
* **State 70 is definitive.** The sensor refuses to store a finger it already
  holds; retrying cannot help, so it is reported immediately.

Only after the sensor has confirmed the template does the user document change.
The backend owns that document; this module never writes a local copy.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from homeassistant.core import HomeAssistant, callback

from .api import EkeyApiError, EkeyAppClient
from .const import (
    DOMAIN,
    ENROLL_STATE_FINISHED_DUPLICATE,
    ENROLL_STATE_FINISHED_SUCCESS,
    ENROLL_STATE_FINISHED_QUITBYUSER,
    ENROLL_STATE_WAIT_FOR_CONFIRMATION,
    EVENT_ENROLLMENT_STATE,
    EVENT_USERS_CHANGED,
)

_LOGGER = logging.getLogger(__name__)

EVENT_ENROLL_PROGRESS = "ekey_enroll_progress"

# Give up when the sensor goes completely silent. It normally ends an abandoned
# session itself (state 60), so this only catches a link that died mid-session.
IDLE_TIMEOUT = 120.0
# Delays at which the sensor's stored list is re-read when checking whether a
# template survived. Copied from admin.html: the sensor needs a moment after
# confirming before the new APID appears.
VERIFY_DELAYS = (1.5, 2.5, 4.0)
CONFIRM_ATTEMPTS = 5
CONFIRM_RETRY_DELAY = 0.8
# The sensor wants a beat after announcing state 35 before it will accept the
# confirmation. Matches the existing service path's pre-confirm pause.
CONFIRM_SETTLE = 1.0

MAX_FINGER = 10


class EnrollError(Exception):
    """The enrollment could not be started or the request was invalid."""


class EnrollSession:
    """One in-flight enrollment."""

    def __init__(self, apid: str, user_id: str, username: str, finger: int) -> None:
        self.apid = apid
        self.user_id = user_id
        self.username = username
        self.finger = finger
        self.confirmed = False
        self.done = False
        self.ok: bool | None = None
        self.phase = "starting"
        self.message = "Starting…"
        self.enstat: int | None = None
        self.templates = 0
        self.tries = 0
        self.last_seen = 0.0

    def as_dict(self) -> dict[str, Any]:
        """The shape the panel renders."""
        return {
            "apid": self.apid,
            "user_id": self.user_id,
            "username": self.username,
            "finger": self.finger,
            "phase": self.phase,
            "message": self.message,
            "enstat": self.enstat,
            "templates": self.templates,
            "tries": self.tries,
            "done": self.done,
            "ok": self.ok,
        }


def progress_text(enstat: int | None, entryc: int, ennumtpl: int,
                  enextres: int, enaccres: int) -> str:
    """Human wording for one enrollment state.

    Kept as a pure function so the wording can be unit-tested without a scanner,
    and so it stays in one place instead of being rebuilt per call site.
    """
    if enstat == 10:
        return "Waiting for the scanner…"
    if enstat == 20:
        return f"Reading the finger… ({ennumtpl} template(s) so far)"
    if enstat == 30:
        if enextres == 0:
            if enaccres == 0:
                return f"Template {ennumtpl} accepted — place the finger again"
            if enaccres == 20:
                return f"Move the finger slightly and try again (try {entryc})"
            if enaccres == 30:
                return f"Use the same finger (try {entryc})"
            return f"Try again (try {entryc})"
        return {
            20: "Place the finger more centred on the sensor",
            30: "Clean the sensor and try again",
            40: "Place the finger more fully on the sensor",
            50: "Poor quality — clean the finger and try again",
            60: "Finger too dry — moisten it slightly",
            70: "Finger too wet — dry it first",
        }.get(enextres, f"Try again (try {entryc})")
    if enstat == ENROLL_STATE_WAIT_FOR_CONFIRMATION:
        return f"Captures complete ({ennumtpl}) — confirming…"
    if enstat == ENROLL_STATE_FINISHED_SUCCESS:
        return "Enrolled."
    if enstat == ENROLL_STATE_FINISHED_QUITBYUSER:
        return "Cancelled."
    if enstat == 60:
        return "The scanner timed out."
    if enstat == ENROLL_STATE_FINISHED_DUPLICATE:
        return "This finger is already enrolled on the scanner."
    return f"State {enstat}"


class EnrollManager:
    """Runs enrollments for one config entry and reports progress on the bus."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        client: EkeyAppClient,
        coordinator,
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.client = client
        self.coordinator = coordinator
        self.sessions: dict[str, EnrollSession] = {}
        self._unsub = None
        self._watchdog: asyncio.Task | None = None

    # ------------------------------------------------------------- lifecycle

    @callback
    def async_attach(self) -> None:
        """Start listening for scanner enrollment states."""
        if self._unsub is None:
            self._unsub = self.hass.bus.async_listen(
                EVENT_ENROLLMENT_STATE, self._handle_state
            )

    @callback
    def async_detach(self) -> None:
        """Stop listening and cancel the watchdog."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None

    # ----------------------------------------------------------------- start

    async def async_start(self, user_id: str, finger: int) -> str:
        """Begin an enrollment and return its APID.

        Validates against the *current* user document rather than a cached copy:
        the slot may have been filled from the device's own page a moment ago.
        """
        if not isinstance(finger, int) or not 1 <= finger <= MAX_FINGER:
            raise EnrollError(f"finger must be between 1 and {MAX_FINGER}")
        if self.active_session() is not None:
            raise EnrollError(
                "an enrollment is already running on this scanner — cancel it first"
            )

        users = await self.client.async_get_users()
        user = next((u for u in users if u.get("id") == user_id), None)
        if user is None:
            raise EnrollError("that user no longer exists on the backend")

        apid = str(uuid.uuid4())
        session = EnrollSession(
            apid=apid,
            user_id=user_id,
            username=str(user.get("username") or ""),
            finger=finger,
        )
        session.last_seen = asyncio.get_running_loop().time()
        self.sessions[apid] = session
        # Claim the APID before the request goes out, so the legacy enrollment
        # listener in __init__.py leaves it alone (it auto-confirms every APID it
        # sees, which would race this session's single confirmation).
        self._panel_claims()[apid] = {"entry_id": self.entry_id, "finger": finger}

        self.async_attach()
        self._emit(session)

        try:
            await self.client.async_enroll_start(apid)
        except EkeyApiError as err:
            self._release(apid)
            raise EnrollError(f"the scanner refused to start: {err}") from err

        session.phase = "running"
        session.message = "Place and lift the finger as the scanner asks."
        self._emit(session)
        self._arm_watchdog()
        return apid

    async def async_cancel(self, apid: str) -> None:
        """Abort on the sensor as well as locally.

        Without the quit the sensor sits waiting for a finger with its LED held
        until its own timeout expires — the operator sees a scanner that looks
        busy for no reason.
        """
        session = self.sessions.get(apid)
        try:
            await self.client.async_enroll_quit(apid)
        except EkeyApiError as err:
            _LOGGER.debug("Quit for %s failed (continuing): %s", apid[:8], err)
        if session is not None and not session.done:
            self._finish(session, ok=False, message="Cancelled.")

    def active_session(self) -> EnrollSession | None:
        """The running session for this entry, if any."""
        return next((s for s in self.sessions.values() if not s.done), None)

    def status(self) -> dict[str, Any] | None:
        """Current session state for the panel, or ``None`` when idle."""
        session = self.active_session() or next(
            (s for s in self.sessions.values() if s.done), None
        )
        return session.as_dict() if session else None

    # ------------------------------------------------------------- internals

    def _panel_claims(self) -> dict[str, Any]:
        bucket = self.hass.data.setdefault(DOMAIN, {}).setdefault(self.entry_id, {})
        return bucket.setdefault("panel_enrollments", {})

    def _release(self, apid: str) -> None:
        self.sessions.pop(apid, None)
        self._panel_claims().pop(apid, None)

    @callback
    def _emit(self, session: EnrollSession) -> None:
        """Publish progress on the HA bus.

        The panel subscribes to this through the websocket connection it already
        has, so no second transport is needed for live updates.
        """
        self.hass.bus.async_fire(
            EVENT_ENROLL_PROGRESS,
            {"entry_id": self.entry_id, **session.as_dict()},
        )

    @callback
    def _arm_watchdog(self) -> None:
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = self.hass.async_create_task(self._watch())

    async def _watch(self) -> None:
        """Fail a session whose scanner has gone silent."""
        try:
            while True:
                await asyncio.sleep(5)
                session = self.active_session()
                if session is None:
                    return
                idle = asyncio.get_running_loop().time() - session.last_seen
                if idle <= IDLE_TIMEOUT:
                    continue
                if session.confirmed:
                    # We accepted a result but never saw the final state; the
                    # template may well be stored. Ask before declaring failure.
                    await self._verify_and_finish(session)
                    return
                _LOGGER.warning(
                    "No response from the scanner for %.0fs — cancelling enrollment %s",
                    idle,
                    session.apid[:8],
                )
                await self.async_cancel(session.apid)
                return
        except asyncio.CancelledError:
            raise

    @callback
    def _handle_state(self, event) -> None:
        """React to one ``NOTIFY_AP_ENROLL_STATE`` from the bus."""
        data = event.data or {}
        apid = data.get("apid")
        session = self.sessions.get(apid) if isinstance(apid, str) else None
        if session is None or session.done:
            return
        enstat = data.get("enstat")
        if not isinstance(enstat, int):
            return

        session.last_seen = asyncio.get_running_loop().time()
        session.enstat = enstat
        session.templates = int(data.get("ennumtpl") or 0)
        session.tries = int(data.get("entryc") or 0)
        session.message = progress_text(
            enstat,
            session.tries,
            session.templates,
            int(data.get("enextres") or 0),
            int(data.get("enaccres") or 0),
        )

        if enstat == ENROLL_STATE_WAIT_FOR_CONFIRMATION:
            if session.confirmed:
                return
            session.confirmed = True
            session.phase = "confirming"
            self._emit(session)
            self.hass.async_create_task(self._confirm(session))
            return

        if enstat == ENROLL_STATE_FINISHED_SUCCESS:
            self.hass.async_create_task(self._succeed(session))
            return

        if enstat == ENROLL_STATE_FINISHED_DUPLICATE:
            self._finish(session, ok=False, message=session.message)
            return

        if enstat >= ENROLL_STATE_FINISHED_QUITBYUSER:
            if session.confirmed:
                self.hass.async_create_task(self._verify_and_finish(session))
            else:
                self._finish(session, ok=False, message=session.message)
            return

        self._emit(session)

    async def _confirm(self, session: EnrollSession) -> None:
        """Send the confirmation, retrying the send but not the decision."""
        await asyncio.sleep(CONFIRM_SETTLE)
        for attempt in range(1, CONFIRM_ATTEMPTS + 1):
            if session.done:
                return
            try:
                await self.client.async_enroll_confirm(session.apid)
                return
            except EkeyApiError as err:
                _LOGGER.debug(
                    "Confirm attempt %d/%d for %s failed: %s",
                    attempt, CONFIRM_ATTEMPTS, session.apid[:8], err,
                )
                await asyncio.sleep(CONFIRM_RETRY_DELAY)
        # Every send failed. The scanner may still have stored it, so do not
        # declare failure here — the terminal state or the watchdog will verify.
        _LOGGER.warning(
            "Could not deliver the enrollment confirmation for %s; "
            "waiting for the scanner's verdict",
            session.apid[:8],
        )

    async def _verify_and_finish(self, session: EnrollSession) -> None:
        """Ask the sensor whether the template exists, then conclude."""
        for delay in VERIFY_DELAYS:
            await asyncio.sleep(delay)
            try:
                apids = await self.client.async_list_fingerprints()
            except EkeyApiError:
                continue
            if session.apid in apids:
                await self._succeed(session)
                return
        self._finish(
            session,
            ok=False,
            message="The scanner did not store the fingerprint. Try again.",
        )

    async def _succeed(self, session: EnrollSession) -> None:
        """Attach the APID to the user's finger slot in the backend document."""
        if session.done:
            return
        try:
            users = await self.client.async_get_users()
            user = next((u for u in users if u.get("id") == session.user_id), None)
            if user is None:
                self._finish(
                    session,
                    ok=False,
                    message=(
                        "The fingerprint is on the scanner but its user has been "
                        "deleted — assign it from the unassigned list."
                    ),
                )
                return

            fingers = [
                dict(f) for f in (user.get("fingers") or []) if isinstance(f, dict)
            ]
            # One fingerprint per slot. An evicted template stays on the sensor
            # and becomes unassigned — the same behaviour as the device's own page,
            # and deliberately not a silent delete: it still opens the door.
            evicted = [f for f in fingers if f.get("finger") == session.finger]
            fingers = [f for f in fingers if f.get("finger") != session.finger]
            fingers = [f for f in fingers if f.get("apid") != session.apid]
            fingers.append(
                {
                    "apid": session.apid,
                    "finger": session.finger,
                    "enrolled_at": int(time.time()),
                }
            )
            user["fingers"] = fingers
            await self.client.async_put_users(users)
        except EkeyApiError as err:
            self._finish(
                session,
                ok=False,
                message=(
                    "The fingerprint is on the scanner but could not be saved to the "
                    f"user list ({err}). Assign it from the unassigned list."
                ),
            )
            return

        # The database's copy is taken HERE — after the assignment is written, before
        # the terminal event goes out. Every enrollment lands in the database, not
        # just the ones started from the storage view: the copy is what makes a
        # scanner replaceable, and asking someone to remember to press a second
        # button afterwards is how a fleet ends up with fingerprints that exist in
        # exactly one place.
        #
        # Read-only as far as the other scanners are concerned. Copying it OUT to
        # them is a separate, explicit act (the storage view's Enroll, or Push) —
        # writing a fingerprint to a door is never a side effect.
        #
        # Deferred import: jobs.py imports this module for its progress event, and a
        # module-level import here would close that loop.
        from .jobs import async_capture_enrolled

        try:
            await async_capture_enrolled(
                self.hass,
                self.entry_id,
                apid=session.apid,
                username=session.username,
                finger=session.finger,
                ha_person=(user or {}).get("ha_person"),
            )
        except Exception:  # noqa: BLE001 — a missing copy is not a failed enrollment
            # Deliberately not surfaced to the operator as an error: the fingerprint
            # is on the scanner and assigned, and it appears in the storage view as
            # "extra" — one click from being adopted.
            _LOGGER.warning(
                "Enrolled %s finger %s, but the fingerprint database could not take "
                "a copy of the template; it will show as extra in the storage view",
                session.username, session.finger, exc_info=True,
            )

        note = ""
        if evicted:
            note = (
                " The fingerprint that held that slot is now unassigned "
                "(still on the scanner)."
            )

        # Refresh BEFORE saying the enrollment finished. The panel reloads the moment
        # it sees the terminal progress message, and that read is served from the
        # coordinator's cached snapshot — so announcing first showed a user list
        # without the finger that had just been enrolled, until the next poll minutes
        # later. See async_refresh_now(); a refresh failure must not strand a session
        # that actually succeeded, so it is logged rather than raised.
        try:
            await self.coordinator.async_refresh_now()
        except Exception:  # noqa: BLE001 — the fingerprint IS enrolled either way
            _LOGGER.exception(
                "Could not refresh after enrolling %s finger %s; the fingerprint is "
                "stored and the list will catch up on the next poll",
                session.username, session.finger,
            )

        self._finish(session, ok=True, message=f"Enrolled.{note}")
        self.hass.bus.async_fire(
            EVENT_USERS_CHANGED, {"entry_id": self.entry_id, "reason": "enrolled"}
        )

    @callback
    def _finish(self, session: EnrollSession, *, ok: bool, message: str) -> None:
        session.done = True
        session.ok = ok
        session.phase = "done"
        session.message = message
        self._emit(session)
        self._panel_claims().pop(session.apid, None)
        if not ok:
            _LOGGER.info(
                "Enrollment for %s finger %s ended: %s",
                session.username, session.finger, message,
            )
