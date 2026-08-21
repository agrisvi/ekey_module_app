"""Connection descriptor for the ekey integration.

One integration now speaks to two kinds of backend:

* **Local** — the ``ekey-ha-daemon`` running on the HA host, reached over plain
  ``http://`` on localhost/127.0.0.1, with an *optional* bearer token.
* **Remote** — an ekey ESP32 device, reached over ``https://`` on its LAN address
  with a *required* bearer token and a self-signed certificate (so SSL
  verification is off by default).

``EkeyConnection`` captures everything needed to build a URL and authorize a
request, so the coordinator / SSE listener / config flow all share one source of
truth instead of re-deriving ``http://host:port`` in several places.
"""
from __future__ import annotations

from dataclasses import dataclass

import aiohttp
from homeassistant.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_SSL,
    CONF_TOKEN,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DEFAULT_HOST, DEFAULT_PORT, DEFAULT_VERIFY_SSL


@dataclass(frozen=True)
class EkeyConnection:
    """How to reach one ekey backend (the daemon or an ESP32 device)."""

    host: str
    port: int
    use_ssl: bool = False
    token: str | None = None
    verify_ssl: bool = False

    @property
    def scheme(self) -> str:
        """``https`` for remote devices, ``http`` for the local daemon."""
        return "https" if self.use_ssl else "http"

    @property
    def base_url(self) -> str:
        """Scheme + host + port, e.g. ``https://192.168.1.20:8080``."""
        return f"{self.scheme}://{self.host}:{self.port}"

    @property
    def scanner_id(self) -> str:
        """Stable per-backend id used to tag events / registry entries.

        Kept scheme-less (``host:port``) so it matches the identifier used
        before the two-mode change and so event routing stays consistent.
        """
        return f"{self.host}:{self.port}"

    def headers(self) -> dict[str, str]:
        """Request headers — send ``Authorization`` only when a token is set.

        Local (daemon) connections may omit the token entirely, in which case no
        auth header is sent and the daemon serves the request as before.
        """
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}

    @classmethod
    def from_entry(cls, entry) -> "EkeyConnection":
        """Build a connection from a config entry's stored data.

        Tolerant of legacy (v1) entries that predate the SSL/token fields.
        """
        data = entry.data
        return cls(
            host=data.get(CONF_HOST, DEFAULT_HOST),
            port=data.get(CONF_PORT, DEFAULT_PORT),
            use_ssl=bool(data.get(CONF_SSL, False)),
            token=data.get(CONF_TOKEN) or None,
            verify_ssl=bool(data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)),
        )


def get_session(hass: HomeAssistant, conn: EkeyConnection) -> aiohttp.ClientSession:
    """Return a shared aiohttp session appropriate for the connection.

    For self-signed HTTPS (an ESP32 device with ``verify_ssl`` off) Home
    Assistant hands back a cached session with certificate verification
    disabled; otherwise the default (verifying) session is reused. Both are
    HA-managed and must not be closed by us.
    """
    if conn.use_ssl and not conn.verify_ssl:
        return async_get_clientsession(hass, verify_ssl=False)
    return async_get_clientsession(hass)
