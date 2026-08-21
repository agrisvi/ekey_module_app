"""Config & options flow for the ekey Home Assistant App integration.

The initial step is a menu that picks the connection mode:

* **Local**  → ``http://`` to the daemon on this host, optional bearer token.
* **Remote** → ``https://`` to an ESP32 device, required token, SSL verification
  off by default (the device ships a self-signed cert).

The options flow (available on remote/ESP32 entries) can push Wi-Fi credentials
to the device via its ``/config`` API, or reset its Wi-Fi back to setup mode.
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
        """Return the options flow handler (Wi-Fi push / reset for ESP32 devices)."""
        return EkeyOptionsFlow(config_entry)


class EkeyOptionsFlow(config_entries.OptionsFlow):
    """Device options: push Wi-Fi credentials to an ESP32, or reset its Wi-Fi.

    Only meaningful for remote (ESP32) entries — the local daemon has no
    ``/config`` API, so local entries abort with a note.
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Stash the entry we are configuring."""
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Menu: push Wi-Fi credentials, or reset the device's Wi-Fi."""
        conn = EkeyConnection.from_entry(self._config_entry)
        if not conn.use_ssl:
            # Local daemon: nothing to configure remotely.
            return self.async_abort(reason="local_no_options")

        return self.async_show_menu(
            step_id="init",
            menu_options=["wifi_push", "wifi_reset"],
        )

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
