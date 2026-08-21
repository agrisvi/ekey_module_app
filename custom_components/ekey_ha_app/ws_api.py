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

import logging
import uuid
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .api import EkeyApiError, EkeyAuthError, EkeyNotFoundError
from .const import DOMAIN, EVENT_CONNECTION_LOST, EVENT_USERS_CHANGED
from .enroll import EVENT_ENROLL_PROGRESS, EnrollError
from .person_map import user_person

_LOGGER = logging.getLogger(__name__)

ERR_NOT_FOUND = "not_found"
ERR_BACKEND = "backend_error"
ERR_AUTH = "backend_unauthorized"
ERR_INVALID = "invalid_request"

MAX_FINGER = 10

# Events the panel needs in order to stay live without polling.
PANEL_EVENTS = (
    EVENT_ENROLL_PROGRESS,
    EVENT_USERS_CHANGED,
    EVENT_CONNECTION_LOST,
)


class _Runtime:
    """The per-entry objects a command needs, resolved in one place."""

    def __init__(self, bucket: dict[str, Any]) -> None:
        self.client = bucket["app_client"]
        self.coordinator = bucket["app_coordinator"]
        self.enroll = bucket["enroll_manager"]


def _runtime(hass: HomeAssistant, entry_id: str) -> _Runtime:
    bucket = (hass.data.get(DOMAIN) or {}).get(entry_id)
    if not isinstance(bucket, dict) or "app_client" not in bucket:
        raise KeyError(entry_id)
    return _Runtime(bucket)


def _fail(connection, msg, err: Exception) -> None:
    """Map a backend exception onto a typed websocket error."""
    if isinstance(err, EkeyAuthError):
        connection.send_error(msg["id"], ERR_AUTH, str(err))
    elif isinstance(err, EkeyNotFoundError):
        connection.send_error(msg["id"], ERR_NOT_FOUND, str(err))
    elif isinstance(err, (EnrollError, ValueError)):
        connection.send_error(msg["id"], ERR_INVALID, str(err))
    else:
        connection.send_error(msg["id"], ERR_BACKEND, str(err))


def _handle_errors(func):
    """Wrap a command so backend failures become typed errors, never tracebacks."""

    async def wrapper(hass, connection, msg):
        try:
            await func(hass, connection, msg)
        except KeyError:
            connection.send_error(
                msg["id"], ERR_NOT_FOUND, "that scanner is not set up (unknown entry_id)"
            )
        except (EkeyApiError, EnrollError, ValueError) as err:
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
        ws_serial_get,
        ws_serial_set,
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
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_handle_errors
async def ws_users_get(hass: HomeAssistant, connection, msg) -> None:
    """Users, plus the sensor-versus-document differences.

    ``unassigned`` and ``missing`` are the two states an installer has to be able
    to act on, and neither is visible from the user list alone.
    """
    rt = _runtime(hass, msg["entry_id"])
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


# --------------------------------------------------------------- subscription


# ---------------------------------------------------------------- serial port


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ekey_ha_app/serial/get",
        vol.Required("entry_id"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_handle_errors
async def ws_serial_get(hass: HomeAssistant, connection, msg) -> None:
    """Which serial port the scanner is on, and what else this backend offers.

    A backend where the port is not a setting — a device with the sensor on fixed UART
    pins — answers 501, which arrives here as EkeyNotFoundError and is reported as such
    so the panel omits the section instead of showing a control it cannot honour.
    """
    rt = _runtime(hass, msg["entry_id"])
    connection.send_result(msg["id"], await rt.client.async_get_serial())


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ekey_ha_app/serial/set",
        vol.Required("entry_id"): str,
        vol.Required("path"): vol.All(str, vol.Length(min=1, max=255)),
        vol.Optional("confirm_console", default=False): bool,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
@_handle_errors
async def ws_serial_set(hass: HomeAssistant, connection, msg) -> None:
    """Choose the port. The reply is the new state, straight from the backend.

    No validation of the path here on purpose: whether a given node is a serial device,
    a system console, or the one the daemon was started with is something only the
    backend can answer, and duplicating a guess at it in Python would be a second
    opinion that can disagree with the one that matters.
    """
    rt = _runtime(hass, msg["entry_id"])
    result = await rt.client.async_set_serial(
        msg["path"], confirm_console=msg.get("confirm_console", False)
    )
    connection.send_result(msg["id"], result)


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
