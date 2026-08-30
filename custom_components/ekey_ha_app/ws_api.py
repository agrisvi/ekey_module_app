"""Websocket commands — the only thing the panel is allowed to talk to.

Why the browser does not call the backend directly, even though it could reach it
on the LAN: doing so would put the backend's bearer token into page JavaScript, and
neither backend sends CORS headers (adding them would create a second, weaker
authentication path into the same API). So the panel speaks to Home Assistant over
the websocket connection it already has, and the integration — which legitimately
holds the token — makes the call.

Websocket commands rather than an HTTP view, for four concrete reasons:
``@require_admin`` replaces a hand-rolled ``request["hass_user"].is_admin`` check;
``hass.callWS()`` needs no token, base URL or ingress-path handling in the panel;
live progress is a native subscription instead of a re-implemented event stream;
and errors are typed instead of encoded as HTTP statuses.

Every command carries ``entry_id`` because a Home Assistant may have several
scanners, each with its own user list. There is one sidebar panel, not one per
scanner — matching how the existing services take a ``scanner`` field.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import instance_id

from . import backup as backup_mod
from . import vault as vault_mod
from .api import EkeyApiError, EkeyAuthError, EkeyNotFoundError
from .const import (
    DOMAIN,
    EVENT_CONNECTION_LOST,
    EVENT_USERS_CHANGED,
    EVENT_VAULT_CHANGED,
    EVENT_VAULT_JOB,
)
from .enroll import EVENT_ENROLL_PROGRESS, EnrollError
from .jobs import (
    TEMPLATE_API_KEY,
    JobBusy,
    UnknownScannerJob,
    async_get_jobs,
    async_refresh_scanners,
)
from .panel import PANEL_VERSION_KEY
from .person_map import user_person
from .transfers import async_get_transfers

_LOGGER = logging.getLogger(__name__)

ERR_NOT_FOUND = "not_found"
ERR_BACKEND = "backend_error"
ERR_AUTH = "backend_unauthorized"
ERR_INVALID = "invalid_request"
ERR_BUSY = "job_running"

MAX_FINGER = 10

# Events the panel needs in order to stay live without polling.
PANEL_EVENTS = (
    EVENT_ENROLL_PROGRESS,
    EVENT_USERS_CHANGED,
    EVENT_CONNECTION_LOST,
    # Both are fired with ``entry_id: None``, which the subscription filter below
    # lets through to a scanner-scoped listener — so a fingerprint job stays
    # visible whichever view the panel happens to be showing.
    EVENT_VAULT_JOB,
    EVENT_VAULT_CHANGED,
)


class UnknownScanner(KeyError):
    """The entry_id a command named is not a set-up, loaded scanner.

    A named exception rather than a bare ``KeyError`` because ``_handle_errors``
    used to translate *every* KeyError into "that scanner is not set up" — so a
    genuine bug anywhere inside a command reported a wrong cause and sent whoever
    was debugging it looking at config entries. A KeyError subclass, so callers
    that already catch KeyError keep working.
    """


class _Runtime:
    """The per-entry objects a command needs, resolved in one place."""

    def __init__(self, bucket: dict[str, Any]) -> None:
        self.client = bucket["app_client"]
        self.coordinator = bucket["app_coordinator"]
        self.enroll = bucket["enroll_manager"]


def _runtime(hass: HomeAssistant, entry_id: str) -> _Runtime:
    bucket = (hass.data.get(DOMAIN) or {}).get(entry_id)
    if not isinstance(bucket, dict) or "app_client" not in bucket:
        raise UnknownScanner(entry_id)
    return _Runtime(bucket)


def _fail(connection, msg, err: Exception) -> None:
    """Map a backend exception onto a typed websocket error."""
    if isinstance(err, EkeyAuthError):
        connection.send_error(msg["id"], ERR_AUTH, str(err))
    elif isinstance(err, EkeyNotFoundError):
        connection.send_error(msg["id"], ERR_NOT_FOUND, str(err))
    elif isinstance(err, JobBusy):
        # Its own code so the panel can say "a job is already running" rather than
        # presenting a refusal as a malformed request.
        connection.send_error(msg["id"], ERR_BUSY, str(err))
    elif isinstance(err, (EnrollError, ValueError)):
        connection.send_error(msg["id"], ERR_INVALID, str(err))
    else:
        connection.send_error(msg["id"], ERR_BACKEND, str(err))


def _handle_errors(func):
    """Wrap a command so backend failures become typed errors, never tracebacks."""

    async def wrapper(hass, connection, msg):
        try:
            await func(hass, connection, msg)
        except (UnknownScanner, UnknownScannerJob) as err:
            # Only THESE KeyErrors mean what the message says. A bare `except
            # KeyError` here used to report any dictionary slip inside any command
            # as "that scanner is not set up", which is a wrong answer that costs
            # real debugging time; other KeyErrors now surface as themselves, with
            # a traceback in the log. A job raises its own type, and carries a
            # reason worth repeating (a scanner with no app layer cannot enrol).
            detail = str(err.args[0]) if err.args else ""
            connection.send_error(
                msg["id"],
                ERR_NOT_FOUND,
                detail if isinstance(err, UnknownScannerJob) and detail
                else "that scanner is not set up (unknown entry_id)",
            )
        except (EkeyApiError, EnrollError, JobBusy, ValueError) as err:
            _fail(connection, msg, err)

    wrapper.__name__ = func.__name__
    return wrapper


@callback
def async_register(hass: HomeAssistant) -> None:
    """Register every panel command. Safe to call once per HA run."""
    for command in (
        ws_scanners,
        ws_persons,
        ws_users_get,
        ws_user_add,
        ws_user_update,
        ws_user_delete,
        ws_fingerprint_assign,
        ws_fingerprint_delete,
        ws_enroll_start,
        ws_enroll_cancel,
        ws_storage_get,
        ws_storage_scanner_preview,
        ws_storage_sync_from_scanner,
        ws_storage_push,
        ws_storage_enroll,
        ws_storage_purge_fingerprint,
        ws_storage_clean,
        ws_storage_job_cancel,
        ws_storage_backup_begin,
        ws_storage_backup_chunk,
        ws_storage_backup_end,
        ws_storage_restore_begin,
        ws_storage_restore_chunk,
        ws_storage_restore_inspect,
        ws_storage_restore_commit,
        ws_storage_restore_abort,
        ws_subscribe,
    ):
        websocket_api.async_register_command(hass, command)


# ------------------------------------------------------------------ discovery


@websocket_api.websocket_command({vol.Required("type"): "ekey_ha_app/scanners"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_scanners(hass: HomeAssistant, connection, msg) -> None:
    """Every configured backend, with what it can do.

    Reported even when a backend has no app layer, so the panel can say *why*
    user management is unavailable instead of showing an empty list.
    """
    scanners = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        bucket = (hass.data.get(DOMAIN) or {}).get(entry.entry_id)
        if not isinstance(bucket, dict) or "app_client" not in bucket:
            scanners.append(
                {
                    "entry_id": entry.entry_id,
                    "title": entry.title,
                    "loaded": False,
                    "app_api": False,
                    "capabilities": None,
                }
            )
            continue
        coordinator = bucket["app_coordinator"]
        data = coordinator.data or {}
        scanners.append(
            {
                "entry_id": entry.entry_id,
                "title": entry.title,
                "scanner_id": bucket["app_client"].conn.scanner_id,
                "loaded": True,
                "app_api": bool(data.get("app_api")),
                "capabilities": data.get("capabilities"),
                "available": coordinator.last_update_success,
            }
        )
    connection.send_result(msg["id"], {"scanners": scanners})


@websocket_api.websocket_command({vol.Required("type"): "ekey_ha_app/persons"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_persons(hass: HomeAssistant, connection, msg) -> None:
    """The ``person.*`` entities available to link a user to."""
    persons = [
        {
            "entity_id": state.entity_id,
            "name": state.attributes.get("friendly_name", state.entity_id),
        }
        for state in hass.states.async_all("person")
    ]
    persons.sort(key=lambda p: p["name"].lower())
    connection.send_result(msg["id"], {"persons": persons})


# ---------------------------------------------------------------------- users


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ekey_ha_app/users/get",
        vol.Required("entry_id"): str,
        vol.Optional("refresh", default=False): bool,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_handle_errors
async def ws_users_get(hass: HomeAssistant, connection, msg) -> None:
    """Users, plus the sensor-versus-document differences.

    ``unassigned`` and ``missing`` are the two states an installer has to be able
    to act on, and neither is visible from the user list alone.

    ``refresh`` re-reads the scanner first. Everything here is served from the
    coordinator's snapshot, and that poll is five minutes apart — so a Refresh
    button that did not set this would re-render the same picture and look broken,
    which is exactly what it did.
    """
    rt = _runtime(hass, msg["entry_id"])
    if msg.get("refresh"):
        await rt.coordinator.async_refresh_now()
    data = rt.coordinator.data or {}
    if not data.get("app_api"):
        connection.send_result(
            msg["id"],
            {
                "app_api": False,
                "users": [],
                "unassigned": [],
                "missing": [],
                "capabilities": data.get("capabilities"),
            },
        )
        return
    connection.send_result(
        msg["id"],
        {
            "app_api": True,
            "users": data.get("users") or [],
            "unassigned": data.get("unassigned") or [],
            "missing": data.get("missing") or [],
            "scanner_list_known": bool(data.get("scanner_list_known")),
            "capabilities": data.get("capabilities"),
            "enroll": rt.enroll.status(),
        },
    )


async def _put_users(rt: _Runtime, users: list[dict[str, Any]], hass, entry_id: str,
                     reason: str) -> None:
    """Write the document, refresh the cache, THEN make every consumer notice.

    The order is the whole point — see ``async_refresh_now``. Every consumer that
    hears the event re-reads through the coordinator's cached snapshot, so
    announcing before the cache catches up hands them the state from before the
    write.
    """
    await rt.client.async_put_users(users)
    await rt.coordinator.async_refresh_now()
    hass.bus.async_fire(EVENT_USERS_CHANGED, {"entry_id": entry_id, "reason": reason})


def _check_person_unique(
    users: list[dict[str, Any]], person_id: str | None, *, exclude_id: str | None = None
) -> None:
    """One person ↔ at most one app user per scanner.

    Enforced here rather than only in the UI: the same rule is what makes
    "who was that?" answerable from an APID, and a second front-end must not be
    able to break it.
    """
    if not person_id:
        return
    for user in users:
        if user.get("id") == exclude_id:
            continue
        if user_person(user) == person_id:
            raise ValueError(
                f"{person_id} is already linked to \"{user.get('username')}\" on this scanner"
            )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ekey_ha_app/users/add",
        vol.Required("entry_id"): str,
        vol.Required("username"): vol.All(str, vol.Length(min=1, max=64)),
        vol.Optional("ha_person"): vol.Any(None, str),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_handle_errors
async def ws_user_add(hass: HomeAssistant, connection, msg) -> None:
    """Create a user. No fingerprints yet — enrollment attaches those."""
    rt = _runtime(hass, msg["entry_id"])
    username = msg["username"].strip()
    if not username:
        raise ValueError("the username cannot be blank")
    person_id = msg.get("ha_person") or None

    users = await rt.client.async_get_users()
    _check_person_unique(users, person_id)

    user: dict[str, Any] = {"id": str(uuid.uuid4()), "username": username, "fingers": []}
    if person_id:
        user["ha_person"] = person_id
    users.append(user)
    await _put_users(rt, users, hass, msg["entry_id"], "user_added")
    connection.send_result(msg["id"], {"user": user})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ekey_ha_app/users/update",
        vol.Required("entry_id"): str,
        vol.Required("user_id"): str,
        vol.Optional("username"): vol.All(str, vol.Length(min=1, max=64)),
        vol.Optional("ha_person"): vol.Any(None, str),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_handle_errors
async def ws_user_update(hass: HomeAssistant, connection, msg) -> None:
    """Rename a user and/or change which person it is linked to."""
    rt = _runtime(hass, msg["entry_id"])
    users = await rt.client.async_get_users()
    user = next((u for u in users if u.get("id") == msg["user_id"]), None)
    if user is None:
        connection.send_error(msg["id"], ERR_NOT_FOUND, "no such user")
        return

    if "username" in msg:
        name = msg["username"].strip()
        if not name:
            raise ValueError("the username cannot be blank")
        user["username"] = name

    if "ha_person" in msg:
        person_id = msg["ha_person"] or None
        _check_person_unique(users, person_id, exclude_id=user.get("id"))
        if person_id:
            user["ha_person"] = person_id
        else:
            user.pop("ha_person", None)

    await _put_users(rt, users, hass, msg["entry_id"], "user_updated")
    connection.send_result(msg["id"], {"user": user})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ekey_ha_app/users/delete",
        vol.Required("entry_id"): str,
        vol.Required("user_id"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_handle_errors
async def ws_user_delete(hass: HomeAssistant, connection, msg) -> None:
    """Delete a user and its fingerprints, sensor first and verified.

    The order is not cosmetic. A fingerprint that still answers on the sensor
    still opens the door, so it must never disappear from the user list: each
    template is deleted on the sensor, the sensor's list is re-read, and the user
    is removed only if nothing of theirs survives. A partial failure keeps the
    user and says which templates are still there.
    """
    rt = _runtime(hass, msg["entry_id"])
    users = await rt.client.async_get_users()
    user = next((u for u in users if u.get("id") == msg["user_id"]), None)
    if user is None:
        connection.send_error(msg["id"], ERR_NOT_FOUND, "no such user")
        return

    apids = [
        f["apid"]
        for f in (user.get("fingers") or [])
        if isinstance(f, dict) and isinstance(f.get("apid"), str)
    ]
    for apid in apids:
        try:
            await rt.client.async_delete_fingerprint(apid)
        except EkeyApiError as err:
            _LOGGER.warning("Deleting %s from the sensor failed: %s", apid[:8], err)

    survivors: list[str] = []
    if apids:
        try:
            on_sensor = set(await rt.client.async_list_fingerprints())
            survivors = [a for a in apids if a in on_sensor]
        except EkeyApiError as err:
            connection.send_error(
                msg["id"],
                ERR_BACKEND,
                f"could not confirm the deletion with the scanner — user kept ({err})",
            )
            return

    if survivors:
        connection.send_error(
            msg["id"],
            ERR_BACKEND,
            f"{len(survivors)} of {len(apids)} fingerprint(s) could not be deleted "
            "from the scanner — the user has been kept. Try again.",
        )
        await rt.coordinator.async_refresh_now()
        return

    users = [u for u in users if u.get("id") != msg["user_id"]]
    await _put_users(rt, users, hass, msg["entry_id"], "user_deleted")
    connection.send_result(msg["id"], {"deleted": msg["user_id"]})


# --------------------------------------------------------------- fingerprints


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ekey_ha_app/fingerprints/assign",
        vol.Required("entry_id"): str,
        vol.Required("apid"): str,
        vol.Required("user_id"): str,
        vol.Required("finger"): vol.All(int, vol.Range(min=1, max=MAX_FINGER)),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_handle_errors
async def ws_fingerprint_assign(hass: HomeAssistant, connection, msg) -> None:
    """Give an existing template an owner, or move it.

    Purely a document edit — nothing is sent to the sensor, so nobody has to
    present a finger again. This is how an unassigned fingerprint (one the sensor
    holds but no user claims) is recovered instead of deleted and re-enrolled.
    """
    rt = _runtime(hass, msg["entry_id"])
    apid, finger = msg["apid"], msg["finger"]
    users = await rt.client.async_get_users()
    target = next((u for u in users if u.get("id") == msg["user_id"]), None)
    if target is None:
        connection.send_error(msg["id"], ERR_NOT_FOUND, "no such user")
        return

    # Detach from wherever it currently is.
    for user in users:
        user["fingers"] = [
            dict(f)
            for f in (user.get("fingers") or [])
            if isinstance(f, dict) and f.get("apid") != apid
        ]

    evicted = [f for f in target["fingers"] if f.get("finger") == finger]
    target["fingers"] = [f for f in target["fingers"] if f.get("finger") != finger]
    target["fingers"].append({"apid": apid, "finger": finger})

    await _put_users(rt, users, hass, msg["entry_id"], "fingerprint_assigned")
    connection.send_result(
        msg["id"],
        {
            "assigned": apid,
            "user_id": msg["user_id"],
            "finger": finger,
            # Say so explicitly: the evicted template is still on the sensor and
            # still works — it is now unassigned, not gone.
            "evicted": [f.get("apid") for f in evicted],
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ekey_ha_app/fingerprints/delete",
        vol.Required("entry_id"): str,
        vol.Required("apid"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_handle_errors
async def ws_fingerprint_delete(hass: HomeAssistant, connection, msg) -> None:
    """Delete one template from the sensor, then drop it from the document.

    Same ordering rule as deleting a user: verified on the sensor first.
    """
    rt = _runtime(hass, msg["entry_id"])
    apid = msg["apid"]
    await rt.client.async_delete_fingerprint(apid)

    try:
        on_sensor = set(await rt.client.async_list_fingerprints())
    except EkeyApiError as err:
        connection.send_error(
            msg["id"], ERR_BACKEND, f"could not confirm the deletion ({err})"
        )
        return

    if apid in on_sensor:
        connection.send_error(
            msg["id"],
            ERR_BACKEND,
            "the scanner still holds this fingerprint — it has not been removed "
            "from the user. Try again.",
        )
        await rt.coordinator.async_refresh_now()
        return

    users = await rt.client.async_get_users()
    touched = False
    for user in users:
        fingers = [
            dict(f)
            for f in (user.get("fingers") or [])
            if isinstance(f, dict) and f.get("apid") != apid
        ]
        if len(fingers) != len(user.get("fingers") or []):
            touched = True
        user["fingers"] = fingers
    if touched:
        await _put_users(rt, users, hass, msg["entry_id"], "fingerprint_deleted")
    else:
        await rt.coordinator.async_refresh_now()
    connection.send_result(msg["id"], {"deleted": apid})


# ------------------------------------------------------------------ enrolment


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ekey_ha_app/enroll/start",
        vol.Required("entry_id"): str,
        vol.Required("user_id"): str,
        vol.Required("finger"): vol.All(int, vol.Range(min=1, max=MAX_FINGER)),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_handle_errors
async def ws_enroll_start(hass: HomeAssistant, connection, msg) -> None:
    """Start an enrollment; progress arrives through the subscription."""
    rt = _runtime(hass, msg["entry_id"])
    apid = await rt.enroll.async_start(msg["user_id"], msg["finger"])
    connection.send_result(msg["id"], {"apid": apid, "status": rt.enroll.status()})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ekey_ha_app/enroll/cancel",
        vol.Required("entry_id"): str,
        vol.Required("apid"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_handle_errors
async def ws_enroll_cancel(hass: HomeAssistant, connection, msg) -> None:
    """Abort a running enrollment on the sensor as well as here."""
    rt = _runtime(hass, msg["entry_id"])
    await rt.enroll.async_cancel(msg["apid"])
    connection.send_result(msg["id"], {"cancelled": msg["apid"]})


# ------------------------------------------------ the fingerprint database
#
# The panel reaches these through a virtual "Fingerprint storage" entry in its
# scanner dropdown, but note what is NOT here: no sentinel entry_id. The sentinel
# never leaves the browser, so `_runtime()` above can never be handed one and no
# existing command changes behaviour. Only the commands that genuinely name a
# scanner take an `entry_id`.


def _scanner_rows(hass: HomeAssistant) -> list[dict[str, Any]]:
    """One row per configured scanner: what it holds, and whether we could ask.

    ``list_known`` is the load-bearing field. When a scanner's fingerprint list
    could not be read, ``on_scanner`` is empty *and* ``list_known`` is false, and
    the panel must render "unknown" rather than "missing" — the standing rule in
    this codebase, because a wrongly-shown "missing" invites a pointless
    minutes-long push and a wrongly-shown "ok" hides a door that is out of step.
    """
    rows: list[dict[str, Any]] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        bucket = (hass.data.get(DOMAIN) or {}).get(entry.entry_id)
        if not isinstance(bucket, dict) or "app_client" not in bucket:
            rows.append(
                {
                    "entry_id": entry.entry_id,
                    "title": entry.title,
                    "loaded": False,
                    "list_known": False,
                    "on_scanner": [],
                    "on_scanner_count": 0,
                    "dev_variant": None,
                    "prod_sn": None,
                    "template_api": None,
                    "as_of": None,
                }
            )
            continue

        app = bucket.get("app_coordinator")
        scanner = bucket.get("coordinator")
        app_data = getattr(app, "data", None) or {}
        device = (getattr(scanner, "data", None) or {}).get("device") or {}
        # A failed refresh leaves the previous data in place, so the flag alone
        # would keep reporting the list this scanner held before it went quiet.
        # Stale is unknown, and unknown is never missing.
        fresh = bool(getattr(app, "last_update_success", True))
        known = fresh and bool(app_data.get("scanner_list_known"))
        aps = app_data.get("scanner_aps") if known else None
        on_scanner = [a for a in aps if isinstance(a, str)] if isinstance(aps, list) else []

        rows.append(
            {
                "entry_id": entry.entry_id,
                "title": entry.title,
                "loaded": True,
                "list_known": known,
                "on_scanner": on_scanner,
                "on_scanner_count": len(on_scanner),
                "dev_variant": device.get("dev_variant"),
                "prod_sn": device.get("prod_sn"),
                # None until a transfer has actually been attempted — see
                # jobs.remember_template_api for why this is learned rather than
                # probed. False means an older backend that cannot take part.
                "template_api": bucket.get(TEMPLATE_API_KEY),
                # When this row was actually read off the scanner, so the view can
                # say how old the comparison is instead of implying it is live.
                "as_of": app_data.get("read_at"),
            }
        )
    return rows


def _extras(hass: HomeAssistant, rows: list[dict[str, Any]], known: set[str]):
    """Fingerprints a scanner holds that the database does not.

    They work today — what is missing is Home Assistant's copy, and without it that
    finger cannot be restored, moved to a new scanner, or repaired. Reported so the
    panel can offer to adopt them; never deleted, and never adopted on their own.
    """
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not row["list_known"]:
            continue
        bucket = (hass.data.get(DOMAIN) or {}).get(row["entry_id"]) or {}
        users = ((getattr(bucket.get("app_coordinator"), "data", None) or {})
                 .get("users") or [])
        owner: dict[str, tuple[str, Any]] = {}
        for user in users:
            for finger in user.get("fingers") or []:
                if isinstance(finger, dict) and finger.get("apid"):
                    owner[str(finger["apid"]).lower()] = (
                        user.get("username"),
                        finger.get("finger"),
                    )

        for apid in row["on_scanner"]:
            if apid.lower() in known:
                continue
            username, finger = owner.get(apid.lower(), (None, None))
            record = seen.setdefault(
                apid,
                {
                    "apid": apid,
                    "entry_ids": [],
                    "scanners": [],
                    "user_hint": username,
                    "finger_hint": finger,
                },
            )
            record["entry_ids"].append(row["entry_id"])
            record["scanners"].append(row["title"])
    return sorted(seen.values(), key=lambda r: r["apid"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ekey_ha_app/storage/get",
        vol.Optional("refresh", default=False): bool,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_handle_errors
async def ws_storage_get(hass: HomeAssistant, connection, msg) -> None:
    """Everything the storage view renders — and deliberately no template hex.

    The blobs are ~14.6 kB each, the panel has no use for them, and shipping
    biometric data to a browser so it can draw a badge would be a copy nobody asked
    for. ``has_template`` per finger is what the view actually needs.

    The running job is folded in so a page that loads mid-job adopts it, the same
    way ``users/get`` hands over the live enrolment.

    ``refresh`` asks every scanner for its current list first. The panel sets it
    when the view is opened and when Refresh is pressed, and leaves it off for the
    reloads an event triggers — those follow a change this integration just made,
    and a burst of them must not turn into a burst of RS-485 round trips.
    """
    vault = vault_mod.async_get_vault(hass)
    await vault.async_load()

    if msg.get("refresh"):
        await async_refresh_scanners(hass)

    rows = _scanner_rows(hass)
    view = vault_mod.build_records_view(vault.data)
    view["scanners"] = rows
    view["extras"] = _extras(hass, rows, vault_mod.stored_apids(vault.data))
    view["job"] = async_get_jobs(hass).status()
    connection.send_result(msg["id"], view)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ekey_ha_app/storage/scanner_preview",
        vol.Required("entry_id"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_handle_errors
async def ws_storage_scanner_preview(hass: HomeAssistant, connection, msg) -> None:
    """What copying *this* scanner into the database would take in.

    Shown before anything happens, because the alternative is asking someone to
    approve a minutes-long operation over a list they cannot see. That list is read
    from the scanner first: approving a copy of a list that is minutes old is the
    same as not seeing it.
    """
    vault = vault_mod.async_get_vault(hass)
    await vault.async_load()
    stored = vault_mod.stored_apids(vault.data)

    await async_refresh_scanners(hass, [msg["entry_id"]])

    rows = {row["entry_id"]: row for row in _scanner_rows(hass)}
    row = rows.get(msg["entry_id"])
    if row is None:
        raise UnknownScanner(msg["entry_id"])

    bucket = (hass.data.get(DOMAIN) or {}).get(msg["entry_id"]) or {}
    users = ((getattr(bucket.get("app_coordinator"), "data", None) or {})
             .get("users") or [])
    owner: dict[str, tuple[str, Any]] = {}
    for user in users:
        for finger in user.get("fingers") or []:
            if isinstance(finger, dict) and finger.get("apid"):
                owner[str(finger["apid"]).lower()] = (
                    user.get("username"),
                    finger.get("finger"),
                )

    items = [
        {
            "apid": apid,
            "user_hint": owner.get(apid.lower(), (None, None))[0],
            "finger_hint": owner.get(apid.lower(), (None, None))[1],
            "in_database": apid.lower() in stored,
        }
        for apid in row["on_scanner"]
    ]
    connection.send_result(
        msg["id"],
        {
            "title": row["title"],
            "list_known": row["list_known"],
            "dev_variant": row["dev_variant"],
            "items": items,
            "new_count": sum(1 for i in items if not i["in_database"]),
            "known_count": sum(1 for i in items if i["in_database"]),
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ekey_ha_app/storage/sync_from_scanner",
        vol.Required("entry_id"): str,
        vol.Optional("apids"): [str],
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_handle_errors
async def ws_storage_sync_from_scanner(hass: HomeAssistant, connection, msg) -> None:
    """Copy templates off a scanner into the database. Writes nothing to it.

    One APID is the "adopt this one" case — the same job with ``total: 1``, because
    a three-second read that can fail three different ways deserves the same report
    as a thirty-item sweep.
    """
    status = await async_get_jobs(hass).async_sync_from_scanner(
        msg["entry_id"], msg.get("apids")
    )
    connection.send_result(msg["id"], status)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ekey_ha_app/storage/push",
        vol.Optional("apids"): [str],
        vol.Optional("entry_ids"): [str],
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_handle_errors
async def ws_storage_push(hass: HomeAssistant, connection, msg) -> None:
    """Write stored templates to the scanners that are missing them.

    Reached only from a button. Nothing in this integration calls it on a timer or
    when a scanner reconnects: writing a fingerprint to a door controller grants
    physical access, and that is the one operation worth keeping a person in front
    of.
    """
    status = await async_get_jobs(hass).async_push(
        msg.get("apids"), msg.get("entry_ids")
    )
    connection.send_result(msg["id"], status)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ekey_ha_app/storage/enroll",
        vol.Required("entry_id"): str,
        vol.Required("user_id"): str,
        vol.Required("finger"): vol.All(int, vol.Range(min=1, max=MAX_FINGER)),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_handle_errors
async def ws_storage_enroll(hass: HomeAssistant, connection, msg) -> None:
    """Enrol on one scanner, then copy the result to every other one.

    The same enrollment the scanner page runs — same manager, same session, same
    progress — wrapped in a job that continues afterwards: the template goes into
    the database and out to the rest of the fleet, so one presentation of a finger
    produces one identity on every door instead of one per door.
    """
    status = await async_get_jobs(hass).async_enroll(
        msg["entry_id"], msg["user_id"], msg["finger"]
    )
    connection.send_result(msg["id"], status)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ekey_ha_app/storage/purge_fingerprint",
        vol.Required("apid"): str,
        vol.Required("confirm"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_handle_errors
async def ws_storage_purge_fingerprint(hass: HomeAssistant, connection, msg) -> None:
    """Delete one fingerprint from every scanner, and from the database last.

    ``confirm`` must be the APID being deleted. Checked here rather than trusted
    from the panel for the same reason ``storage/clean`` re-checks its word: this is
    reachable from anything holding an admin token, and the panel is not the only
    possible caller.
    """
    apid = str(msg["apid"]).strip().lower()
    if str(msg["confirm"]).strip().lower() != apid:
        # ValueError, not vol.Invalid: voluptuous's Invalid is not a ValueError, so
        # it escapes _handle_errors and surfaces as an unhandled exception instead of
        # the refusal it is. Same shape as storage/clean's word check.
        raise ValueError("the confirmation does not name the fingerprint to delete")
    status = await async_get_jobs(hass).async_purge_fingerprint(apid)
    connection.send_result(msg["id"], status)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ekey_ha_app/storage/clean",
        vol.Required("confirm"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_handle_errors
async def ws_storage_clean(hass: HomeAssistant, connection, msg) -> None:
    """Delete every record. The scanners are untouched.

    The confirmation word is checked *here* as well as in the page, on the same
    principle the device's own admin page applies to its destructive routes: a check
    that only ever happens in the browser proves nothing about what reached the
    server.
    """
    if msg["confirm"] != "DELETE":
        raise ValueError("the confirmation word did not match")
    removed = await vault_mod.async_get_vault(hass).async_clean()
    connection.send_result(msg["id"], {"removed": removed})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ekey_ha_app/storage/job/cancel",
        vol.Optional("job_id"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_handle_errors
async def ws_storage_job_cancel(hass: HomeAssistant, connection, msg) -> None:
    """Ask the running job to stop after the fingerprint it is on."""
    cancelling = async_get_jobs(hass).async_cancel(msg.get("job_id"))
    connection.send_result(msg["id"], {"cancelling": cancelling})


# ------------------------------------------------------------ backup / restore


async def _installation_id(hass: HomeAssistant) -> str:
    """A short, stable marker for "this Home Assistant".

    Home Assistant's own instance id, hashed and truncated: enough for a restore to
    notice that a file came from somewhere else, not enough to be an identifier
    worth carrying around in a file that may be shared.
    """
    raw = await instance_id.async_get(hass)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _version(hass: HomeAssistant) -> str:
    """What to stamp a backup with, so a file says which build wrote it."""
    version = (hass.data.get(DOMAIN) or {}).get(PANEL_VERSION_KEY)
    return f"ekey module App {version}" if version else "ekey module App"


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ekey_ha_app/storage/backup/begin",
        vol.Optional("encrypt", default=True): bool,
        vol.Optional("passphrase"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_handle_errors
async def ws_storage_backup_begin(hass: HomeAssistant, connection, msg) -> None:
    """Build the file and hand back the handle to pull it down with."""
    encrypt = msg.get("encrypt", True)
    passphrase = msg.get("passphrase") if encrypt else None
    if encrypt and not passphrase:
        raise ValueError(
            "a passphrase is required, or choose to save without encryption"
        )

    vault = vault_mod.async_get_vault(hass)
    await vault.async_load()
    payload, filename = await backup_mod.async_create(
        hass,
        vault.data,
        passphrase=passphrase,
        created_by=_version(hass),
        installation=await _installation_id(hass),
    )
    connection.send_result(
        msg["id"], async_get_transfers(hass).start_download(payload, filename)
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ekey_ha_app/storage/backup/chunk",
        vol.Required("download_id"): str,
        vol.Required("index"): int,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_handle_errors
async def ws_storage_backup_chunk(hass: HomeAssistant, connection, msg) -> None:
    """One piece of a prepared backup, base64-encoded."""
    connection.send_result(
        msg["id"],
        async_get_transfers(hass).download_chunk(msg["download_id"], msg["index"]),
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ekey_ha_app/storage/backup/end",
        vol.Required("download_id"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_handle_errors
async def ws_storage_backup_end(hass: HomeAssistant, connection, msg) -> None:
    """Release the buffer. Also the cancel path, which is why it never errors."""
    async_get_transfers(hass).end_download(msg["download_id"])
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ekey_ha_app/storage/restore/begin",
        vol.Required("filename"): str,
        vol.Required("size"): int,
        vol.Required("chunks"): int,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_handle_errors
async def ws_storage_restore_begin(hass: HomeAssistant, connection, msg) -> None:
    """Announce an upload. The size is refused here if it cannot be a backup."""
    upload_id = async_get_transfers(hass).start_upload(
        msg["filename"], msg["size"], msg["chunks"]
    )
    connection.send_result(msg["id"], {"upload_id": upload_id})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ekey_ha_app/storage/restore/chunk",
        vol.Required("upload_id"): str,
        vol.Required("index"): int,
        vol.Required("data"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_handle_errors
async def ws_storage_restore_chunk(hass: HomeAssistant, connection, msg) -> None:
    """One piece of an incoming backup."""
    connection.send_result(
        msg["id"],
        async_get_transfers(hass).upload_chunk(
            msg["upload_id"], msg["index"], msg["data"]
        ),
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ekey_ha_app/storage/restore/inspect",
        vol.Required("upload_id"): str,
        vol.Optional("passphrase"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_handle_errors
async def ws_storage_restore_inspect(hass: HomeAssistant, connection, msg) -> None:
    """Describe an uploaded file, and preview it once it can be opened.

    Called twice for an encrypted backup: once with no passphrase, which returns
    the plaintext header so the dialog can say what the file *claims* to hold, and
    once with the passphrase for the real preview. Nothing is written either way.
    """
    transfers = async_get_transfers(hass)
    raw = transfers.uploaded_bytes(msg["upload_id"])
    header, encrypted = backup_mod.read_header(raw)

    result: dict[str, Any] = {
        "filename": transfers.upload_name(msg["upload_id"]),
        "encrypted": encrypted,
        "header": header,
        "needs_passphrase": encrypted and not msg.get("passphrase"),
        "preview": None,
        "problems": [],
        "foreign": header.get("installation") != await _installation_id(hass),
    }
    if result["needs_passphrase"]:
        connection.send_result(msg["id"], result)
        return

    opened = await backup_mod.async_open_payload(hass, raw, msg.get("passphrase"))
    good, problems = backup_mod.validate_records(opened["records"])

    vault = vault_mod.async_get_vault(hass)
    await vault.async_load()
    stored = vault_mod.stored_apids(vault.data)

    people = opened.get("people") or {}
    by_person: dict[str, dict[str, Any]] = {}
    for apid, record in good.items():
        key = record.get("person_key") or ""
        row = by_person.setdefault(
            key,
            {
                "username": (people.get(key) or {}).get("name")
                or record.get("username")
                or key.removeprefix(vault_mod.NAME_KEY_PREFIX),
                "new": 0,
                "known": 0,
                "total": 0,
            },
        )
        row["total"] += 1
        if apid in stored:
            row["known"] += 1
        else:
            row["new"] += 1

    result["problems"] = problems
    result["preview"] = {
        "users": sorted(by_person.values(), key=lambda r: (r["username"] or "").casefold()),
        "record_count": len(good),
        "new_count": sum(1 for a in good if a not in stored),
        "refresh_count": sum(1 for a in good if a in stored),
        "db_only_count": len(stored - set(good)),
    }
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ekey_ha_app/storage/restore/commit",
        vol.Required("upload_id"): str,
        vol.Optional("passphrase"): str,
        vol.Optional("mode", default="merge"): vol.In(["merge", "replace"]),
        vol.Optional("confirm_delete", default=0): int,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_handle_errors
async def ws_storage_restore_commit(hass: HomeAssistant, connection, msg) -> None:
    """Write the uploaded records into the database. Touches no scanner.

    A restore never writes to a sensor, by design: afterwards the presence matrix
    shows which scanners are missing the restored fingerprints and the operator
    chooses what to push. That keeps "recover the database" and "change what opens a
    door" as two separate decisions.

    ``confirm_delete`` must equal the number of database-only records the panel
    showed. That closes the window where the database changed between preview and
    commit — the same reason the device's own page re-checks a confirmation
    server-side.
    """
    transfers = async_get_transfers(hass)
    raw = transfers.uploaded_bytes(msg["upload_id"])
    opened = await backup_mod.async_open_payload(hass, raw, msg.get("passphrase"))
    good, problems = backup_mod.validate_records(opened["records"])

    vault = vault_mod.async_get_vault(hass)
    await vault.async_load()
    existing = dict(vault.data.get("records") or {})
    people = dict(vault.data.get("people") or {})
    db_only = set(existing) - set(good)

    # Read with .get: the schema fills these in on a real websocket call, and a
    # direct call (a test, or any future in-process caller) then behaves the same
    # instead of raising a KeyError that looks like something else entirely.
    mode = msg.get("mode", "merge")
    if mode == "replace" and msg.get("confirm_delete", 0) != len(db_only):
        raise ValueError(
            f"the database now has {len(db_only)} record(s) the file does not, but "
            f"{msg.get('confirm_delete', 0)} was confirmed — review the preview again"
        )

    added = sum(1 for apid in good if apid not in existing)
    refreshed = len(good) - added
    if mode == "replace":
        records = dict(good)
        merged_people = dict(opened.get("people") or {})
        deleted = len(db_only)
    else:
        records = {**existing, **good}
        merged_people = {**people, **(opened.get("people") or {})}
        deleted = 0

    await vault.async_replace_all(
        {**vault.data, "records": records, "people": merged_people}
    )
    transfers.abort_upload(msg["upload_id"])
    connection.send_result(
        msg["id"],
        {
            "restored": len(good),
            "added": added,
            "refreshed": refreshed,
            "deleted": deleted,
            "problems": problems,
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ekey_ha_app/storage/restore/abort",
        vol.Required("upload_id"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_handle_errors
async def ws_storage_restore_abort(hass: HomeAssistant, connection, msg) -> None:
    """Throw an upload away. Never errors — it is the cancel path."""
    async_get_transfers(hass).abort_upload(msg["upload_id"])
    connection.send_result(msg["id"], {"ok": True})


# --------------------------------------------------------------- subscription

# The serial-port pair that used to live here is gone: the port is a connection
# setting and now belongs to the config entry's Configure dialog (see
# ``EkeyOptionsFlow`` in config_flow.py), which reaches the same client directly.
# Keeping the commands as well would leave a second, unused path into the same
# write — the kind that stops being tested and then stops working.


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ekey_ha_app/subscribe",
        vol.Optional("entry_id"): str,
    }
)
@websocket_api.require_admin
@callback
def ws_subscribe(hass: HomeAssistant, connection, msg) -> None:
    """Stream enrollment progress and change notifications to the panel.

    A plain event relay rather than a state machine: the panel re-reads whatever
    it needs when told something changed, so a missed message costs a refresh
    rather than a wrong screen.
    """
    entry_id = msg.get("entry_id")

    @callback
    def forward(event) -> None:
        data = dict(event.data or {})
        if entry_id and data.get("entry_id") not in (None, entry_id):
            return
        connection.send_message(
            websocket_api.event_message(
                msg["id"], {"event_type": event.event_type, "data": data}
            )
        )

    unsubs = [hass.bus.async_listen(name, forward) for name in PANEL_EVENTS]

    @callback
    def unsubscribe() -> None:
        for unsub in unsubs:
            unsub()

    connection.subscriptions[msg["id"]] = unsubscribe
    connection.send_result(msg["id"])
