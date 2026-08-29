"""HTTP client for the backend's app layer (``/app/v1``) and the scanner routes.

Why this module exists at all: :mod:`connection` is only a *descriptor* — it knows
how to build a URL and an ``Authorization`` header and nothing else — while the
actual HTTP verbs grew up scattered across ``coordinator.py``, ``sse_listener.py``
and ``config_flow.py``. The app layer needs a dozen more calls, so they get one
owner here instead of a fourth copy of the same ``async with session.get(...)``
boilerplate.

Deliberately **not** merged into ``EkeyDataUpdateCoordinator``: that coordinator
runs with ``update_interval=None`` and a fixed two-key data shape, and the app
layer wants its own refresh cadence. Leaving it untouched keeps scanner polling
exactly as it was.

The one non-obvious rule in this whole file — and the reason a plain
``resp.raise_for_status()`` is not enough — is that **a scanner-level refusal
arrives as HTTP 200**. The daemon and the ESP32 both only report *transport*
failures through the status code; when the sensor itself says no (storage full,
an enrollment already running, an unknown APID) the body carries
``rpc_error_code: "Error"`` with a 200. Missing that turns "the scanner refused"
into "success" — so :meth:`EkeyAppClient._check_rpc` raises on it, and every
scanner call goes through it.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from typing import Any

import aiohttp

from .connection import EkeyConnection
from .const import (
    API_APP_ACTIONS,
    API_APP_CAPABILITIES,
    API_APP_EVENTS,
    API_APP_KNX,
    API_APP_LINKS,
    API_APP_MQTT,
    API_APP_SERIAL,
    API_APP_USERS,
    API_DEVICE,
    API_FINGERPRINT_TEMPLATE,
    API_FINGERPRINTS,
    API_FINGERPRINTS_ENROLL,
    API_FINGERPRINTS_ENROLL_CONFIRM,
    API_FINGERPRINTS_ENROLL_QUIT,
    API_FINGERPRINTS_ENROLL_STATE,
    API_FINGERPRINTS_TEMPLATE,
)
from .templates import DEFAULT_DOMAIN_ID, TemplateInfo, parse_template_hex
from .util import clean_json_string, pick_rpc_reply, split_json_documents

_LOGGER = logging.getLogger(__name__)

# Separate timeouts, because these operations are not alike. A document read is a
# file read on the device; an enrollment start puts the sensor into a mode and can
# sit behind an RS-485 round trip that is already in flight.
TIMEOUT_DOC = aiohttp.ClientTimeout(total=15)
TIMEOUT_SCANNER = aiohttp.ClientTimeout(total=20)
TIMEOUT_PROBE = aiohttp.ClientTimeout(total=8)


class EkeyApiError(Exception):
    """Any failure talking to the backend."""


class EkeyAuthError(EkeyApiError):
    """401/403 — the token is missing, wrong, or has been rotated.

    Surfaced separately so the config entry can start a reauth flow instead of
    reporting a generic outage: a factory reset on the backend rotates the token,
    and "unavailable forever" is the wrong way to tell someone that.
    """


class EkeyNotFoundError(EkeyApiError):
    """404 — the route does not exist on this backend.

    For ``/app/v1/*`` this is the normal answer from a backend that has no app
    layer yet, which is why :mod:`capabilities` treats it as information rather
    than an error.
    """


class EkeyBusyError(EkeyApiError):
    """504 — the scanner did not answer in time (it is busy, not broken)."""


class EkeyScannerRefused(EkeyApiError):
    """HTTP 200 with ``rpc_error_code: "Error"`` — the sensor itself said no."""


class EkeyTemplateRejected(EkeyApiError):
    """A template was sent and the scanner did not keep it.

    Its own class because this is *not* a transport failure and reads like a
    success everywhere except in one field: HTTP 200, ``rpc_error_code: "OK"``,
    and ``verified: false``. The usual cause is a device-variant or ``domainID``
    mismatch, i.e. a fingerprint that can never be copied to that scanner — so the
    caller wants to report it as a permanent skip rather than retry it forever.

    ``verdict`` distinguishes "the device answered and refused"
    (``device_response``) from "only the transport ever acknowledged it"
    (``transport_ack_only``), which is the difference between a definite no and an
    unconfirmed write worth verifying against the saved-AP list.
    """

    def __init__(
        self,
        message: str,
        *,
        apid: str | None = None,
        verdict: str | None = None,
        verified: Any = None,
    ) -> None:
        super().__init__(message)
        self.apid = apid
        self.verdict = verdict
        self.verified = verified


class EkeyAppClient:
    """One method per backend resource this integration needs.

    Holds no state beyond the connection descriptor and the HA-managed session,
    so it is cheap to construct and safe to keep on ``hass.data``.
    """

    def __init__(self, conn: EkeyConnection, session: aiohttp.ClientSession) -> None:
        self._conn = conn
        self._session = session

    @property
    def conn(self) -> EkeyConnection:
        """The connection this client speaks to (host/port/token/scanner_id)."""
        return self._conn

    # ---------------------------------------------------------------- plumbing

    def _url(self, path: str) -> str:
        return f"{self._conn.base_url}{path}"

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        headers = dict(self._conn.headers())
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    @staticmethod
    def _check_rpc(payload: Any, what: str) -> Any:
        """Raise if a 200 response is actually a scanner refusal.

        See the module docstring: this is the failure mode that a status-code
        check cannot see.
        """
        if isinstance(payload, dict) and payload.get("rpc_error_code") == "Error":
            raise EkeyScannerRefused(
                f"{what}: {payload.get('error_message') or 'the scanner rejected it'}"
            )
        return payload

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        timeout: aiohttp.ClientTimeout = TIMEOUT_DOC,
        expect_json: bool = True,
    ) -> Any:
        """Perform one request and map failures onto this module's exceptions."""
        url = self._url(path)
        try:
            async with self._session.request(
                method,
                url,
                headers=self._headers(json_body=body is not None),
                json=body if body is not None else None,
                timeout=timeout,
            ) as resp:
                text = await resp.text()

                if resp.status in (401, 403):
                    raise EkeyAuthError(f"{method} {path}: not authorized ({resp.status})")
                # 501 alongside 404: to a caller deciding whether to OFFER a feature they
                # mean the same thing — this backend does not have it. The daemon uses
                # 501 where the route exists but the platform provides no hook (the
                # serial-port picker on a device with the sensor on fixed pins), which is
                # a more precise answer than 404 but the same decision for us.
                if resp.status in (404, 501):
                    raise EkeyNotFoundError(f"{method} {path}: not available ({resp.status})")
                if resp.status == 504:
                    raise EkeyBusyError(f"{method} {path}: scanner timeout")
                if resp.status >= 400:
                    raise EkeyApiError(f"{method} {path}: HTTP {resp.status} {text[:200]}")

                if not expect_json or not text.strip():
                    return None
                return self._parse(text, f"{method} {path}")
        except (EkeyApiError, asyncio.CancelledError):
            raise
        except TimeoutError as err:
            raise EkeyApiError(f"{method} {path}: timed out") from err
        except aiohttp.ClientError as err:
            raise EkeyApiError(f"{method} {path}: {err}") from err

    @staticmethod
    def _parse(text: str, what: str) -> Any:
        """Parse JSON, tolerating the two ways the daemon bends it.

        First: literal control characters inside string values, which
        :func:`clean_json_string` strips. The existing coordinator already copes
        this way, so the app layer is not made stricter than the rest of the
        integration.

        Second: several documents in one body — see :func:`split_json_documents`.
        The one that answers the request is picked by ``rpc_error_code``, which
        every RPC reply carries and no notification does. Taking the first
        document instead would be wrong in exactly the case that matters: a
        scanner refusal arrives as HTTP 200 with ``rpc_error_code: "Error"``, so
        reading a notification here would report a refusal as success.
        """
        try:
            return json.loads(text)
        except ValueError:
            pass

        cleaned = clean_json_string(text)
        try:
            return json.loads(cleaned)
        except ValueError:
            pass

        reply = pick_rpc_reply(split_json_documents(cleaned))
        if reply is not None:
            return reply

        raise EkeyApiError(f"{what}: response was not JSON")

    # ----------------------------------------------------------- capabilities

    async def async_capabilities(self) -> dict[str, Any] | None:
        """``GET /app/v1/capabilities``, or ``None`` if the backend has none.

        A backend that predates the endpoint answers 404. That is not an error —
        it means "ask the old way" (see :mod:`capabilities`).
        """
        try:
            payload = await self._request("GET", API_APP_CAPABILITIES, timeout=TIMEOUT_PROBE)
        except EkeyNotFoundError:
            return None
        return payload if isinstance(payload, dict) else None

    async def async_has_app_api(self) -> bool:
        """True when this backend serves the app layer at all.

        Probes ``/app/v1/users`` because every backend with an app layer has it,
        and it is the document this integration needs first. A 404 means no app
        layer; an auth failure is propagated, because "you gave me the wrong
        token" must not be reported as "your device is too old".
        """
        try:
            await self._request("GET", API_APP_USERS, timeout=TIMEOUT_PROBE)
        except EkeyNotFoundError:
            return False
        return True

    # ----------------------------------------------------------------- users

    async def async_get_users(self) -> list[dict[str, Any]]:
        """``GET /app/v1/users``. An absent document is served as ``[]``."""
        payload = await self._request("GET", API_APP_USERS)
        if payload is None:
            return []
        if not isinstance(payload, list):
            raise EkeyApiError("GET /app/v1/users: expected a JSON array")
        return payload

    async def async_put_users(self, users: list[dict[str, Any]]) -> None:
        """``PUT /app/v1/users`` — whole-document replace.

        The backend validates only that the top level is an array and re-emits it
        minified, preserving keys it does not know. That is what lets this
        integration annotate a user with ``ha_person`` without any firmware
        change, and it is also why the caller must send the *complete* list: a
        partial PUT deletes everyone missing from it.
        """
        if not isinstance(users, list):
            raise EkeyApiError("PUT /app/v1/users: users must be a list")
        await self._request("PUT", API_APP_USERS, body=users)

    # ---------------------------------------------------- other app documents

    async def async_get_actions(self) -> list[dict[str, Any]]:
        """``GET /app/v1/actions``. Read-only here; editing is a later phase."""
        payload = await self._request("GET", API_APP_ACTIONS)
        return payload if isinstance(payload, list) else []

    async def async_get_links(self) -> list[dict[str, Any]]:
        """``GET /app/v1/links``. Read-only here; editing is a later phase."""
        payload = await self._request("GET", API_APP_LINKS)
        return payload if isinstance(payload, list) else []

    async def async_get_mqtt(self) -> dict[str, Any]:
        """``GET /app/v1/mqtt``. The password is never returned by the backend."""
        payload = await self._request("GET", API_APP_MQTT)
        return payload if isinstance(payload, dict) else {}

    async def async_get_knx(self) -> dict[str, Any]:
        """``GET /app/v1/knx``."""
        payload = await self._request("GET", API_APP_KNX)
        return payload if isinstance(payload, dict) else {}

    # --------------------------------------------------------- serial port

    async def async_get_serial(self) -> dict[str, Any]:
        """``GET /app/v1/serial`` — which port the scanner is on, and the alternatives.

        Present only on a backend where the port is a setting: a device with the
        sensor on fixed UART pins answers 501, which surfaces as
        :class:`EkeyNotFoundError` and means "do not offer this". The reply carries
        ``editable`` and ``applies``, both of which depend on how *this* backend was
        started, so neither may be assumed by the caller.

        The probe timeout, not the document one: the options flow calls this before it
        can draw its menu, so an unreachable backend must cost the operator a few
        seconds of a dialog opening, not fifteen.
        """
        payload = await self._request("GET", API_APP_SERIAL, timeout=TIMEOUT_PROBE)
        return payload if isinstance(payload, dict) else {}

    async def async_set_serial(
        self, path: str, *, confirm_console: bool = False
    ) -> dict[str, Any]:
        """``PUT /app/v1/serial`` — choose the port.

        Returns the same body ``async_get_serial`` would, because the backend answers
        the write with the new state: one round trip, and no window in which the
        caller shows a stale "takes effect immediately" for a daemon that has since
        bound to something.

        ``confirm_console`` is required for a port the backend flagged as the
        machine's own console — opening one can switch an on-board UART into RS485
        mode, so it takes a deliberate second act rather than a silent success.
        """
        payload = await self._request(
            "PUT",
            API_APP_SERIAL,
            body={"path": path, "confirm_console": bool(confirm_console)},
        )
        return payload if isinstance(payload, dict) else {}

    async def async_get_events(
        self, *, limit: int = 20, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        """``GET /app/v1/events`` — one page plus the total from ``X-Event-Count``.

        Returns ``(rows, total)``. Rows come back oldest-first within the page;
        the total is what the pager needs and is *not* derivable from the page.
        Implemented with a raw request because this is the only call that needs a
        response header.
        """
        url = self._url(f"{API_APP_EVENTS}?limit={int(limit)}&offset={int(offset)}")
        try:
            async with self._session.get(
                url, headers=self._headers(), timeout=TIMEOUT_DOC
            ) as resp:
                if resp.status in (401, 403):
                    raise EkeyAuthError("GET /app/v1/events: not authorized")
                if resp.status == 404:
                    raise EkeyNotFoundError("GET /app/v1/events: not found")
                if resp.status >= 400:
                    raise EkeyApiError(f"GET /app/v1/events: HTTP {resp.status}")
                text = await resp.text()
                total_hdr = resp.headers.get("X-Event-Count")
        except (EkeyApiError, asyncio.CancelledError):
            raise
        except (TimeoutError, aiohttp.ClientError) as err:
            raise EkeyApiError(f"GET /app/v1/events: {err}") from err

        rows = self._parse(text, "GET /app/v1/events") if text.strip() else []
        if not isinstance(rows, list):
            rows = []
        try:
            total = int(total_hdr) if total_hdr is not None else len(rows)
        except ValueError:
            total = len(rows)
        return rows, total

    # ------------------------------------------------- scanner (``/api/v1``)

    async def async_list_fingerprints(self) -> list[str]:
        """APIDs the *sensor* actually holds.

        The sensor — not this integration and not ``users.json`` — is the
        authority on which fingerprints exist, which is what makes
        "unassigned fingerprint" and "missing on scanner" detectable at all.
        ``aps`` is absent (not empty) when the count is zero.
        """
        payload = await self._request("GET", API_FINGERPRINTS, timeout=TIMEOUT_SCANNER)
        self._check_rpc(payload, "list fingerprints")
        if isinstance(payload, dict):
            aps = payload.get("aps")
            return [a for a in aps if isinstance(a, str)] if isinstance(aps, list) else []
        return []

    async def async_enroll_start(self, apid: str) -> dict[str, Any]:
        """``POST /api/v1/fingerprints/enroll`` — put the sensor into enrollment.

        ``Apnot: 1`` asks the sensor to *push* progress notifications, which is
        what makes the live progress in the panel possible instead of polling the
        RS-485 bus.
        """
        payload = await self._request(
            "POST",
            API_FINGERPRINTS_ENROLL,
            body={"Apid": apid, "Apnot": 1},
            timeout=TIMEOUT_SCANNER,
        )
        return self._check_rpc(payload, "start enrollment") or {}

    async def async_enroll_state(self, apid: str) -> dict[str, Any]:
        """``POST /api/v1/fingerprints/enroll/state`` — the polling fallback.

        Only used when the event stream is unavailable. Polling this during an
        enrollment is unreliable by design (the sensor frequently answers with a
        notify frame instead, leaving the response empty), which is exactly why
        the event path is preferred.
        """
        payload = await self._request(
            "POST",
            API_FINGERPRINTS_ENROLL_STATE,
            body={"Apid": apid},
            timeout=TIMEOUT_SCANNER,
        )
        return payload if isinstance(payload, dict) else {}

    async def async_enroll_confirm(self, apid: str) -> dict[str, Any]:
        """``POST /api/v1/fingerprints/enroll/confirm`` — accept the captures."""
        payload = await self._request(
            "POST",
            API_FINGERPRINTS_ENROLL_CONFIRM,
            body={"Apid": apid},
            timeout=TIMEOUT_SCANNER,
        )
        return self._check_rpc(payload, "confirm enrollment") or {}

    async def async_enroll_quit(self, apid: str) -> dict[str, Any]:
        """``POST /api/v1/fingerprints/enroll/quit`` — abort on the sensor.

        Must be sent when the operator cancels: the sensor otherwise sits waiting
        for a finger with its LED held until its own timeout expires.
        """
        payload = await self._request(
            "POST",
            API_FINGERPRINTS_ENROLL_QUIT,
            body={"Apid": apid},
            timeout=TIMEOUT_SCANNER,
        )
        return payload if isinstance(payload, dict) else {}

    async def async_delete_fingerprint(self, apid: str) -> dict[str, Any]:
        """``DELETE /api/v1/fingerprints/<apid>`` — remove it from the sensor.

        The caller must verify against :meth:`async_list_fingerprints` before
        dropping the record: a fingerprint that still answers on the sensor still
        opens the door, and must never disappear from the user list.
        """
        from urllib.parse import quote

        payload = await self._request(
            "DELETE",
            f"{API_FINGERPRINTS}/{quote(apid, safe='')}",
            timeout=TIMEOUT_SCANNER,
        )
        return payload if isinstance(payload, dict) else {}

    # ------------------------------------------------------- templates

    async def async_get_template(
        self, apid: str, *, domain_id: str | None = None
    ) -> TemplateInfo:
        """``GET /api/v1/fingerprints/<apid>/template`` — read a finger's template.

        Returns a *validated* :class:`~.templates.TemplateInfo` rather than the raw
        body, because a template that has not been checked has no business being
        stored: the blob carries its own length and its own AP-ID, and both are
        compared here (see :mod:`.templates` for the failure this prevents).

        ``domain_id`` is sent only when it differs from the backend's own default.
        It is the salt in the device's transport-key derivation, so the value used
        on the read must be the value used on the eventual write — which is why the
        caller is handed :attr:`~.templates.TemplateInfo` plus the ``domainID`` the
        backend echoes, and is expected to store both. There is no query-parameter
        form for it, so a non-default value travels as a GET body.

        A backend too old for this route answers 404/501 and therefore raises
        :class:`EkeyNotFoundError` — "this scanner cannot take part", not an error.
        """
        from urllib.parse import quote

        body: dict[str, Any] | None = None
        if domain_id and domain_id != DEFAULT_DOMAIN_ID:
            body = {"domainID": domain_id}

        payload = await self._request(
            "GET",
            API_FINGERPRINT_TEMPLATE.format(apid=quote(apid, safe="")),
            body=body,
            timeout=TIMEOUT_SCANNER,
        )
        self._check_rpc(payload, f"read the template for {apid}")
        if not isinstance(payload, dict):
            raise EkeyApiError(f"read the template for {apid}: unexpected reply")

        # Absent means the backend used its default — the field is only emitted
        # when non-empty, and treating absent as "unknown" would store a record
        # that can never be written back.
        echoed = payload.get("domainID")
        info = parse_template_hex(payload.get("apFingerTemplate"), expect_apid=apid)
        return replace(
            info,
            domain_id=echoed if isinstance(echoed, str) and echoed else DEFAULT_DOMAIN_ID,
        )

    async def async_put_template(
        self, template_hex: str, *, domain_id: str | None = None
    ) -> dict[str, Any]:
        """``PUT /api/v1/fingerprints/template`` — write a template to the sensor.

        No APID anywhere in the request: it travels inside the blob, and the
        backend reads it out of the plaintext header to tell us which finger it
        just wrote.

        **The status code proves nothing here.** The scanner acknowledges the
        transport frames before it has decrypted anything, so a 200 with
        ``rpc_error_code: "OK"`` and ``verified: false`` means the transfer was
        accepted and the template was *not kept* — almost always a device-variant
        or ``domainID`` mismatch. This method therefore raises
        :class:`EkeyTemplateRejected` on anything but ``verified: true``, and the
        exception carries ``verdict`` so the caller can tell "the device said no"
        (``device_response``) from "nobody ever confirmed" (``transport_ack_only``).

        Returns the reply on success, whose ``apid`` is the finger the sensor
        reports having stored — worth comparing against the one intended.
        """
        info = parse_template_hex(template_hex)
        body: dict[str, Any] = {"apFingerTemplate": info.hex}
        if domain_id and domain_id != DEFAULT_DOMAIN_ID:
            body["domainID"] = domain_id

        payload = await self._request(
            "PUT", API_FINGERPRINTS_TEMPLATE, body=body, timeout=TIMEOUT_SCANNER
        )
        self._check_rpc(payload, f"write the template for {info.apid}")
        if not isinstance(payload, dict):
            raise EkeyApiError(f"write the template for {info.apid}: unexpected reply")

        if payload.get("verified") is not True:
            raise EkeyTemplateRejected(
                f"the scanner did not keep the template for {info.apid}",
                apid=payload.get("apid") or info.apid,
                verdict=payload.get("verdict"),
                verified=payload.get("verified"),
            )
        return payload

    async def async_get_device(self) -> dict[str, Any]:
        """``GET /api/v1/device`` — identity, firmware, and the device variant.

        ``dev_variant`` is the one field that decides whether a template can move
        between two scanners at all: the transport key is derived from it together
        with the ``domainID``, so a copy across variants can never be decrypted and
        only ekey can change a device's variant. Read it before offering a push,
        not after failing one.
        """
        payload = await self._request("GET", API_DEVICE, timeout=TIMEOUT_SCANNER)
        self._check_rpc(payload, "read device information")
        return payload if isinstance(payload, dict) else {}
