"""Coordinator for the app layer: users, the sensor's APID list, capabilities.

Separate from :class:`~.coordinator.EkeyDataUpdateCoordinator` on purpose. That one
polls ``/api/v1/device`` and ``/api/v1/fingerprints`` with ``update_interval=None``
and a fixed two-key data shape that entities and one shipped blueprint depend on.
Widening it would risk that; adding a second coordinator costs nothing.

The refresh reads two things together because the interesting facts are in the
*difference* between them:

* ``users`` — what the backend says the people are, and which finger slots they own;
* ``scanner_aps`` — what the sensor actually holds.

A fingerprint in ``users`` but not on the sensor is *missing on scanner* (an
enrollment that never completed there). One on the sensor but in no user is
*unassigned* (an enrollment that finished after a UI gave up, or a failed delete).
Neither is visible without both lists, and both are things an installer has to be
able to see and fix — so they are computed here once rather than in each consumer.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import EkeyApiError, EkeyAppClient, EkeyAuthError, EkeyNotFoundError
from .capabilities import Capabilities, SOURCE_ABSENT, async_detect
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Slow poll, not none. Almost every change comes through an event (an enrollment
# finishing, a delete, another admin editing on the device's own page), so polling
# is only a backstop for the last case — someone editing users in the device's web
# UI while a panel is open. Five minutes is cheap: it is one small file read.
UPDATE_INTERVAL = timedelta(minutes=5)


class EkeyAppCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Keeps the backend's app-layer view fresh for one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: EkeyAppClient,
        entry_id: str,
        config_entry=None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_app_{client.conn.scanner_id}",
            update_interval=UPDATE_INTERVAL,
            config_entry=config_entry,
        )
        self.client = client
        self.entry_id = entry_id
        self.capabilities: Capabilities | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Read capabilities once, then users and the sensor's APID list."""
        if self.capabilities is None:
            try:
                self.capabilities = await async_detect(self.client)
            except EkeyAuthError as err:
                raise UpdateFailed(f"not authorized: {err}") from err

        caps = self.capabilities
        if caps is not None and caps.source == SOURCE_ABSENT:
            # No app layer. Report that plainly and keep the entry usable — the
            # scanner half of the integration works regardless.
            return {
                "users": [],
                "scanner_aps": [],
                "unassigned": [],
                "missing": [],
                "capabilities": caps.as_dict(),
                "app_api": False,
                "read_at": time.time(),
            }

        try:
            users = await self.client.async_get_users()
        except EkeyNotFoundError:
            # The backend lost its app layer between the probe and now (a
            # downgrade, or a different device on the same address). Re-detect
            # next cycle rather than caching a stale answer.
            self.capabilities = None
            raise UpdateFailed("the backend no longer serves /app/v1")
        except EkeyAuthError as err:
            raise UpdateFailed(f"not authorized: {err}") from err
        except EkeyApiError as err:
            raise UpdateFailed(str(err)) from err

        # The sensor list is best-effort: it is an RS-485 round trip and may be
        # busy. Users are still worth showing without it — the badges just cannot
        # be drawn, which the panel reports rather than guessing.
        scanner_aps: list[str] | None
        try:
            scanner_aps = await self.client.async_list_fingerprints()
        except EkeyApiError as err:
            _LOGGER.debug("Could not read the sensor's fingerprint list: %s", err)
            scanner_aps = None

        assigned = {
            finger.get("apid")
            for user in users
            for finger in (user.get("fingers") or [])
            if isinstance(finger, dict) and isinstance(finger.get("apid"), str)
        }

        if scanner_aps is None:
            unassigned: list[str] = []
            missing: list[str] = []
        else:
            on_sensor = set(scanner_aps)
            unassigned = sorted(on_sensor - assigned)
            missing = sorted(assigned - on_sensor)

        return {
            "users": users,
            "scanner_aps": scanner_aps if scanner_aps is not None else [],
            "scanner_list_known": scanner_aps is not None,
            "unassigned": unassigned,
            "missing": missing,
            "capabilities": (caps.as_dict() if caps else None),
            "app_api": bool(caps and caps.has_app_api),
            # When these two lists were actually read off the scanner. The poll is
            # five minutes apart, so a consumer that compares scanners against each
            # other — the storage matrix, and the push that decides what to write
            # from it — has to be able to say how old its picture is.
            "read_at": time.time(),
        }

    async def async_refresh_now(self) -> None:
        """Refresh immediately — the call to use before announcing a change.

        ``async_request_refresh`` is DEBOUNCED: Home Assistant's default cooldown is
        ten seconds, so awaiting it usually returns without having refreshed
        anything. That is the right call for "something may have changed, catch up
        eventually" and the wrong one for "a write just landed and a listener is
        about to re-read".

        Every websocket read (``ws_users_get``) is served from ``self.data``, this
        coordinator's cached snapshot. So the ordering rule is: **write, refresh,
        then announce** — never announce first. Announcing first is what made an
        enrollment appear not to refresh the list: the panel reloads the instant it
        is told the enrollment finished, read the pre-write snapshot, and showed a
        user without the finger that had just been enrolled. It corrected itself on
        the next poll, minutes later, which is exactly long enough to look broken.
        """
        await self.async_refresh()

    async def async_invalidate_capabilities(self) -> None:
        """Forget the cached capabilities, e.g. after the backend was upgraded."""
        self.capabilities = None
        await self.async_refresh_now()
