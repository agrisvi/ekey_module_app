"""Config & options flow for the ekey Home Assistant App integration.

The initial step is a menu that picks the connection mode:

* **Local**  → ``http://`` to the daemon on this host, optional bearer token.
* **Remote** → ``https://`` to an ESP32 device, required token, SSL verification
  off by default (the device ships a self-signed cert).

The options flow — the entry's **Configure** dialog — is where the settings that
belong to *reaching* a backend live:

* the serial port the scanner is wired to, on a backend where that is a setting
  (offered to local daemons and devices alike, because only the backend can say
  whether it has one);
* on an ESP32, pushing Wi-Fi credentials via its ``/config`` API, or resetting its
  Wi-Fi back to setup mode.

The serial port used to be a card on the sidebar panel. It sits here instead
because it is a connection setting, next to the host and token that reach the same
backend, and because a Home Assistant with two scanners then has one dialog per
scanner rather than one page that has to say which it means.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_SSL,
    CONF_TOKEN,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import EkeyApiError, EkeyAppClient, EkeyAuthError
from .connection import EkeyConnection, get_session
from .const import (
    API_CONFIG,
    API_HEALTH,
    API_REBOOT,
    API_WIFI_RESET,
    DEFAULT_PORT,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    MODE_LOCAL,
    MODE_REMOTE,
)

_LOGGER = logging.getLogger(__name__)

# Local mode nudges the user toward the loopback host; remote has no default.
DEFAULT_LOCAL_HOST = "127.0.0.1"

# The serial-port step's fields. ``path`` matches what the backend's PUT body calls
# it, so the form key and the wire name cannot drift apart.
CONF_SERIAL_PATH = "path"
CONF_SERIAL_CONFIRM = "confirm"


async def validate_input(hass: HomeAssistant, conn: EkeyConnection) -> dict[str, Any]:
    """Confirm we can reach the backend and (for token backends) that auth works.

    Reaching the HTTP layer at all proves host/port/scheme are right; a 401 means
    the token is wrong. Raises ``CannotConnect`` / ``InvalidAuth`` otherwise.
    """
    session = get_session(hass, conn)
    url = f"{conn.base_url}{API_HEALTH}"

    try:
        async with session.get(
            url, headers=conn.headers(), timeout=aiohttp.ClientTimeout(total=5)
        ) as response:
            if response.status == 401:
                raise InvalidAuth
            if response.status not in (200, 503):
                # Any other HTTP status still proves reachability, but flag it.
                _LOGGER.warning("ekey health at %s returned HTTP %s", url, response.status)
            return {"title": f"ekey Scanner ({conn.scanner_id})"}
    except InvalidAuth:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        _LOGGER.error("Cannot connect to ekey backend at %s - %s", conn.base_url, err)
        raise CannotConnect from err


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for ekey Home Assistant App."""

    VERSION = 2

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """First step: choose the connection mode."""
        return self.async_show_menu(
            step_id="user",
            menu_options=[MODE_LOCAL, MODE_REMOTE],
        )

    async def async_step_local(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Local daemon over HTTP (optional token)."""
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=DEFAULT_LOCAL_HOST): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                vol.Optional(CONF_TOKEN): str,
            }
        )
        if user_input is None:
            return self.async_show_form(step_id="local", data_schema=schema)

        data = {
            CONF_HOST: user_input[CONF_HOST],
            CONF_PORT: user_input[CONF_PORT],
            CONF_SSL: False,
            CONF_TOKEN: (user_input.get(CONF_TOKEN) or "").strip() or None,
            CONF_VERIFY_SSL: False,
        }
        return await self._async_finish(data, "local", schema)

    async def async_step_remote(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Remote ESP32 device over HTTPS (required token)."""
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                vol.Required(CONF_TOKEN): str,
                vol.Optional(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): bool,
            }
        )
        if user_input is None:
            return self.async_show_form(step_id="remote", data_schema=schema)

        data = {
            CONF_HOST: user_input[CONF_HOST],
            CONF_PORT: user_input[CONF_PORT],
            CONF_SSL: True,
            CONF_TOKEN: user_input[CONF_TOKEN].strip(),
            CONF_VERIFY_SSL: user_input.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
        }
        return await self._async_finish(data, "remote", schema)

    async def _async_finish(
        self, data: dict[str, Any], step_id: str, schema: vol.Schema
    ) -> FlowResult:
        """Dedupe, validate, and create the entry (or re-show the form on error)."""
        errors: dict[str, str] = {}

        conn = EkeyConnection(
            host=data[CONF_HOST],
            port=data[CONF_PORT],
            use_ssl=data[CONF_SSL],
            token=data.get(CONF_TOKEN) or None,
            verify_ssl=data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
        )

        # One config entry per host:port — they share the device identifier.
        await self.async_set_unique_id(conn.scanner_id)
        self._abort_if_unique_id_configured()

        try:
            info = await validate_input(self.hass, conn)
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except InvalidAuth:
            errors["base"] = "invalid_auth"
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"
        else:
            return self.async_create_entry(title=info["title"], data=data)

        return self.async_show_form(step_id=step_id, data_schema=schema, errors=errors)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Start a reauth flow — the stored token stopped being accepted.

        This is not a hypothetical. A factory reset on the backend mints a new API
        token, and the app layer requires one; without this step the integration
        would simply sit there unavailable, giving the operator no way to supply the
        new token short of deleting and re-adding the entry.
        """
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask for a new token and verify it before storing."""
        entry = self._get_reauth_entry()
        schema = vol.Schema({vol.Required(CONF_TOKEN): str})

        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=schema,
                description_placeholders={"host": entry.data.get(CONF_HOST, "")},
            )

        token = (user_input.get(CONF_TOKEN) or "").strip()
        conn = EkeyConnection(
            host=entry.data.get(CONF_HOST, ""),
            port=entry.data.get(CONF_PORT, DEFAULT_PORT),
            use_ssl=bool(entry.data.get(CONF_SSL, False)),
            token=token or None,
            verify_ssl=bool(entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)),
        )

        errors: dict[str, str] = {}
        try:
            await validate_input(self.hass, conn)
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except InvalidAuth:
            errors["base"] = "invalid_auth"
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception during reauth")
            errors["base"] = "unknown"
        else:
            return self.async_update_reload_and_abort(
                entry, data_updates={CONF_TOKEN: token or None}
            )

        return self.async_show_form(
            step_id="reauth_confirm", data_schema=schema, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "EkeyOptionsFlow":
        """Return the options flow handler (serial port, and Wi-Fi on an ESP32)."""
        return EkeyOptionsFlow(config_entry)


class EkeyOptionsFlow(config_entries.OptionsFlow):
    """Connection settings for one backend: its serial port, and an ESP32's Wi-Fi.

    Which entries the menu offers is decided per *backend*, not per connection mode.
    Wi-Fi stays behind ``use_ssl`` because only a device owns its own network
    settings. The serial port is **asked for** instead of assumed: a Linux host
    enumerates its ports and answers 200, a device with the sensor on fixed UART pins
    answers 501, and an add-on answers 200 with ``editable: false`` because the port
    comes from its own configuration. All three are real deployments, and the only
    thing that can tell them apart is the backend.

    Nothing here is stored in the entry's options: every step writes straight to the
    backend, which owns these settings. The flow ends on an abort carrying the result
    rather than an empty ``async_create_entry``, so the dialog can say whether the new
    port is live or waiting on a restart — a fact only the reply knows.
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Stash the entry we are configuring."""
        self._config_entry = config_entry
        # Carried between steps: the backend's last serial reply, and the port picked
        # in the form while the console confirmation is being asked for.
        self._serial: dict[str, Any] | None = None
        self._serial_path: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Menu: the serial port, and on a device its Wi-Fi."""
        conn = EkeyConnection.from_entry(self._config_entry)

        self._serial = await self._async_serial_state()
        # The token is always offered, and first: every entry has one (optional on a
        # local daemon, required on a device), and it gates the steps below — they all
        # call /app/v1, which is what the token guards. There is therefore no longer a
        # case where this dialog has nothing to show, which is why the old
        # `no_options` abort is gone.
        menu_options: list[str] = ["token"]
        if self._serial is not None:
            menu_options.append("serial")
        if conn.use_ssl:
            menu_options.extend(["wifi_push", "wifi_reset"])

        if menu_options == ["token"]:
            # One entry is not a menu. A device with the sensor on fixed pins, or a
            # daemon whose port was pinned with -d, has nothing else to set here.
            return await self.async_step_token()

        return self.async_show_menu(step_id="init", menu_options=menu_options)

    # --------------------------------------------------------------- API token

    async def async_step_token(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Set, replace or clear the API token this entry authenticates with.

        This is the one step here that writes to Home Assistant rather than to the
        backend: the token is a credential the rest of the integration reads out of
        ``entry.data`` through ``EkeyConnection.from_entry``.

        The reauth flow cannot cover this. Reauth only starts when a *stored* token is
        rejected, and on a local daemon the token is optional — so an entry created
        without one has nothing to reject, and an operator who later regenerates the
        token on the System tab had no way in short of deleting the entry and building
        it again.
        """
        entry = self._config_entry
        conn = EkeyConnection.from_entry(entry)
        host = entry.data.get(CONF_HOST, "")

        # Required on a device (HTTPS): its whole API sits behind the bearer token, so
        # a blank one would take the entry offline. Optional on a local daemon, where
        # clearing it is a legitimate choice — /api/v1 there needs no token, and the
        # integration keeps working read-only without one.
        key = vol.Required if conn.use_ssl else vol.Optional
        schema = vol.Schema(
            {
                key(
                    CONF_TOKEN,
                    description={"suggested_value": entry.data.get(CONF_TOKEN) or ""},
                ): str
            }
        )

        def _form(errors: dict[str, str] | None = None) -> FlowResult:
            return self.async_show_form(
                step_id="token",
                data_schema=schema,
                errors=errors or {},
                description_placeholders={"host": host},
            )

        if user_input is None:
            return _form()

        token = (user_input.get(CONF_TOKEN) or "").strip()
        probe = EkeyConnection(
            host=conn.host,
            port=conn.port,
            use_ssl=conn.use_ssl,
            token=token or None,
            verify_ssl=conn.verify_ssl,
        )

        # Reachability, and on a device the token itself — an ESP32 answers 401 on
        # /api/v1/health when the bearer is wrong.
        try:
            await validate_input(self.hass, probe)
        except CannotConnect:
            return _form({"base": "cannot_connect"})
        except InvalidAuth:
            return _form({"base": "invalid_auth"})
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception while setting the API token")
            return _form({"base": "unknown"})

        # On a local daemon /api/v1 is unauthenticated, so the check above proves
        # nothing about the token — what the token guards is /app/v1. Probe that too,
        # or a wrong token would be stored without complaint and fail only later, which
        # is the confusion this step exists to end.
        unverified = False
        if token:
            try:
                await EkeyAppClient(
                    probe, get_session(self.hass, probe)
                ).async_get_users()
            except EkeyAuthError:
                return _form({"base": "invalid_auth"})
            except EkeyApiError as err:
                # No app layer, or briefly unreachable: there is nothing to
                # authenticate against, so the token cannot be judged either way.
                # Store it and say so rather than implying it was checked.
                _LOGGER.debug("Could not verify the token against /app/v1: %s", err)
                unverified = True

        stored = token or None
        if stored == (entry.data.get(CONF_TOKEN) or None):
            return self.async_abort(reason="token_unchanged")

        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_TOKEN: stored}
        )
        # Reload: the app client, the coordinators and the panel each captured the old
        # token when the entry was set up, so none of them would pick this up on its own.
        self.hass.async_create_task(
            self.hass.config_entries.async_reload(entry.entry_id)
        )

        if stored is None:
            return self.async_abort(reason="token_cleared")
        return self.async_abort(
            reason="token_saved_unverified" if unverified else "token_saved"
        )

    # ------------------------------------------------------------- serial port

    def _bucket(self) -> dict[str, Any]:
        """This entry's runtime objects, or ``{}`` when it is not loaded."""
        bucket = (self.hass.data.get(DOMAIN) or {}).get(self._config_entry.entry_id)
        return bucket if isinstance(bucket, dict) else {}

    def _client(self) -> EkeyAppClient:
        """The app-layer client for this entry, reusing the loaded one if there is one.

        A fresh client for an unloaded entry is not a fallback for tidiness: a wrong
        port is one of the reasons an entry fails to load, so the dialog that fixes it
        has to work without one.
        """
        client = self._bucket().get("app_client")
        if isinstance(client, EkeyAppClient):
            return client
        conn = EkeyConnection.from_entry(self._config_entry)
        return EkeyAppClient(conn, get_session(self.hass, conn))

    def _serial_may_exist(self) -> bool:
        """Whether asking this backend about its port is worth a round trip.

        Only a backend that *told* us it has no such setting is skipped. Anything
        else — no capabilities endpoint, an entry that never loaded — is asked,
        because "unknown" must not turn into "not offered" for the installations
        where this control is the one that matters.
        """
        caps = getattr(self._bucket().get("app_coordinator"), "capabilities", None)
        if caps is None or not caps.known:
            return True
        return caps.has_feature("serial_port")

    async def _async_serial_state(self) -> dict[str, Any] | None:
        """``GET /app/v1/serial``, or ``None`` when there is nothing to offer.

        Every failure collapses to ``None`` deliberately: 501 (the sensor is on fixed
        pins), 404 (no app layer), a timeout, a rotated token — none of them is
        something this dialog can act on, and a menu entry that leads to an error is
        worse than an absent one. A rejected token still reaches the operator, as the
        entry's own reauth, which is where a token is meant to be fixed.
        """
        if not self._serial_may_exist():
            return None
        try:
            state = await self._client().async_get_serial()
        except EkeyApiError as err:
            _LOGGER.debug(
                "No serial-port setting on %s: %s", self._config_entry.title, err
            )
            return None

        ports = state.get("ports") if isinstance(state, dict) else None
        if not isinstance(ports, list) or not ports:
            return None
        return state

    async def async_step_serial(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Choose which serial port the scanner is wired to."""
        if self._serial is None:
            self._serial = await self._async_serial_state()
        state = self._serial
        if state is None:
            return self.async_abort(reason="serial_unavailable")

        ports = self._usable_ports(state)
        if not state.get("editable") or not ports:
            # The add-on and a ``-d`` command line both land here. Showing where the
            # port is set, and which one is in use, is the whole value of the step in
            # that case — it is the read-only half of what the panel used to display.
            return self.async_abort(
                reason="serial_read_only",
                description_placeholders={
                    "port": state.get("active") or state.get("selected") or "none",
                    "where": (
                        "the daemon's -d option or the add-on configuration"
                        if state.get("source") == "cli"
                        else "outside Home Assistant"
                    ),
                },
            )

        if user_input is not None:
            path = user_input[CONF_SERIAL_PATH]
            chosen = next((p for p in ports if p["path"] == path), None)
            if chosen is not None and chosen.get("console"):
                # Opening the machine's own terminal can switch that UART into RS485
                # mode, so it takes a second, deliberate act — matching the
                # ``confirm_console`` the backend refuses to proceed without.
                self._serial_path = path
                return await self.async_step_serial_console()
            return await self._async_apply_serial(path)

        return self._serial_form()

    async def async_step_serial_console(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm using a port that is the machine's own system console."""
        path = self._serial_path or ""

        if user_input is not None:
            if user_input.get(CONF_SERIAL_CONFIRM):
                return await self._async_apply_serial(path, confirm_console=True)
            # Declined: back to the picker, not an abort, so another port can be chosen
            # without reopening the dialog.
            return await self.async_step_serial()

        return self.async_show_form(
            step_id="serial_console",
            data_schema=vol.Schema(
                {vol.Required(CONF_SERIAL_CONFIRM, default=False): bool}
            ),
            description_placeholders={"port": path},
        )

    async def _async_apply_serial(
        self, path: str, *, confirm_console: bool = False
    ) -> FlowResult:
        """Send the choice, then report what it takes for the scanner to be on it."""
        try:
            state = await self._client().async_set_serial(
                path, confirm_console=confirm_console
            )
        except EkeyAuthError:
            return self._serial_form("invalid_auth")
        except EkeyApiError as err:
            _LOGGER.warning("Could not set the scanner port to %s: %s", path, err)
            # 409 is its own answer, not a bad request: this installation keeps the
            # port somewhere else, so no choice made here would have been accepted.
            return self._serial_form(
                "serial_elsewhere" if "HTTP 409" in str(err) else "serial_failed"
            )

        # The reply IS the new state, so there is nothing to re-read and no window in
        # which this dialog could promise something the backend has since changed.
        if isinstance(state, dict) and state.get("ports"):
            self._serial = state
        applies = state.get("applies") if isinstance(state, dict) else None
        selected = state.get("selected") if isinstance(state, dict) else None
        return self.async_abort(
            reason="serial_saved_restart" if applies == "restart" else "serial_saved",
            description_placeholders={"port": selected or path},
        )

    def _serial_form(self, error: str | None = None) -> FlowResult:
        """The port picker, optionally carrying an error from a rejected write."""
        state = self._serial or {}
        return self.async_show_form(
            step_id="serial",
            data_schema=self._serial_schema(state),
            errors={"base": error} if error else None,
            description_placeholders=self._serial_placeholders(state),
        )

    @staticmethod
    def _usable_ports(state: dict[str, Any]) -> list[dict[str, Any]]:
        """The entries of the backend's port list that can actually be selected."""
        return [
            p
            for p in state.get("ports") or []
            if isinstance(p, dict) and isinstance(p.get("path"), str) and p["path"]
        ]

    @classmethod
    def _serial_schema(cls, state: dict[str, Any]) -> vol.Schema:
        """A labelled dropdown of the ports the backend enumerated.

        The label is assembled here rather than taken from the backend's ``label``
        alone because two flags have to be visible *before* choosing: a port that is
        the machine's console, and one another process already holds. ``busy`` is not
        filtered out — the holder is often a previous instance of this very daemon,
        and hiding the port would hide the only place the scanner is.
        """
        options: list[SelectOptionDict] = []
        for port in cls._usable_ports(state):
            marks = []
            if port.get("console"):
                marks.append("system console")
            if port.get("busy"):
                marks.append("in use")
            label = f"{port.get('label') or port['path']} — {port['path']}"
            if marks:
                label = f"{label}  [{', '.join(marks)}]"
            options.append(SelectOptionDict(value=port["path"], label=label))

        return vol.Schema(
            {
                vol.Required(
                    CONF_SERIAL_PATH, default=cls._current_path(state)
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=options, mode=SelectSelectorMode.DROPDOWN
                    )
                )
            }
        )

    @classmethod
    def _current_path(cls, state: dict[str, Any]) -> Any:
        """Which port to preselect, as a raw device node.

        The backend stores its selection under whichever name udev preferred, which
        for any real USB adapter is a ``/dev/serial/by-id`` alias and not the node.
        Comparing against ``path`` alone therefore matched nothing in exactly the
        common case — the bug the daemon's own list had — so both names are checked.
        """
        ports = cls._usable_ports(state)
        for name in (state.get("selected"), state.get("active")):
            if not name:
                continue
            for port in ports:
                if name in (port["path"], port.get("by_id")):
                    return port["path"]
        return vol.UNDEFINED

    @staticmethod
    def _serial_placeholders(state: dict[str, Any]) -> dict[str, str]:
        """What the form says about this backend's current state.

        ``applies`` is read from the reply, never fixed text: whether a change needs a
        restart depends on whether the scanner library has already bound to a port,
        which is a fact about right now and only the backend has it.
        """
        active = state.get("active") or ""
        selected = state.get("selected") or ""
        if active:
            current = active
        elif selected:
            current = f"{selected} (chosen, not connected yet)"
        else:
            current = "none chosen yet"

        return {
            "current": current,
            "applies": (
                "A different port takes effect when the daemon restarts — it is "
                "already connected to the current one."
                if state.get("applies") == "restart"
                else "No scanner is connected yet, so a port chosen here is tried "
                "within 30 seconds; no restart is needed."
            ),
        }

    # ------------------------------------------------------------------- Wi-Fi

    async def async_step_wifi_push(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Push new Wi-Fi (and optional mDNS/port) settings to the device."""
        conn = EkeyConnection.from_entry(self._config_entry)
        session = get_session(self.hass, conn)
        errors: dict[str, str] = {}

        if user_input is not None:
            payload: dict[str, Any] = {"wifi_ssid": user_input["wifi_ssid"]}
            # A blank password means "keep the stored one" — the device only
            # changes the password when wifi_pass is present.
            if user_input.get("wifi_pass"):
                payload["wifi_pass"] = user_input["wifi_pass"]
            if user_input.get("mdns_host"):
                payload["mdns_host"] = user_input["mdns_host"]
            if user_input.get("https_port"):
                payload["https_port"] = user_input["https_port"]

            try:
                async with session.post(
                    f"{conn.base_url}{API_CONFIG}",
                    json=payload,
                    headers=conn.headers(),
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 401:
                        errors["base"] = "invalid_auth"
                    elif resp.status != 200:
                        errors["base"] = "config_failed"

                # The device applies a Wi-Fi change on the next boot (trial +
                # rollback), so a reboot is needed for it to take effect.
                if not errors and user_input.get("reboot_now", True):
                    try:
                        async with session.post(
                            f"{conn.base_url}{API_REBOOT}",
                            headers=conn.headers(),
                            timeout=aiohttp.ClientTimeout(total=10),
                        ):
                            pass
                    except (aiohttp.ClientError, asyncio.TimeoutError):
                        # Rebooting drops the connection before it can reply.
                        _LOGGER.info("Reboot request link dropped (expected)")
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                _LOGGER.warning("Wi-Fi push to %s failed: %s", conn.base_url, err)
                errors["base"] = "cannot_connect"

            if not errors:
                return self.async_create_entry(title="", data={})

        # Best-effort prefill from the device's current settings.
        prefill: dict[str, Any] = {}
        try:
            async with session.get(
                f"{conn.base_url}{API_CONFIG}",
                headers=conn.headers(),
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    prefill = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            pass

        schema = vol.Schema(
            {
                vol.Required("wifi_ssid", default=prefill.get("wifi_ssid", "")): str,
                vol.Optional("wifi_pass"): str,
                vol.Optional("mdns_host", default=prefill.get("mdns_host", "")): str,
                vol.Optional(
                    "https_port", default=prefill.get("https_port", conn.port)
                ): int,
                vol.Optional("reboot_now", default=True): bool,
            }
        )
        return self.async_show_form(
            step_id="wifi_push", data_schema=schema, errors=errors
        )

    async def async_step_wifi_reset(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Clear the device's Wi-Fi credentials and reboot it into setup mode."""
        conn = EkeyConnection.from_entry(self._config_entry)
        session = get_session(self.hass, conn)
        errors: dict[str, str] = {}

        if user_input is not None:
            if user_input.get("confirm"):
                try:
                    async with session.post(
                        f"{conn.base_url}{API_WIFI_RESET}",
                        headers=conn.headers(),
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status == 401:
                            errors["base"] = "invalid_auth"
                        elif resp.status != 200:
                            errors["base"] = "config_failed"
                except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                    # The device reboots into setup mode and drops the link —
                    # a missing reply here is the expected success case.
                    _LOGGER.info("wifi-reset link dropped (expected on reboot): %s", err)
            if not errors:
                return self.async_create_entry(title="", data={})

        schema = vol.Schema({vol.Required("confirm", default=False): bool})
        return self.async_show_form(
            step_id="wifi_reset", data_schema=schema, errors=errors
        )


class CannotConnect(Exception):
    """Error to indicate we cannot connect."""


class InvalidAuth(Exception):
    """Error to indicate the bearer token was rejected."""
