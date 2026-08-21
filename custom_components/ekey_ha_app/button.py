"""Button platform for ekey Home Assistant App.

Two buttons, both of which do something Home Assistant cannot do any other way:
make the scanner's LED signal. Everything else that used to be a button here is
now in the sidebar panel, which is a better place for it — see the note at the
bottom of this file for what went where.
"""
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN, CONF_DAEMON_HOST, CONF_DAEMON_PORT
from .coordinator import EkeyDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ekey button platform."""
    coordinator: EkeyDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    async_add_entities([
        EkeyLEDGreenButton(coordinator, entry),
        EkeyLEDRedButton(coordinator, entry),
    ])


class EkeyLEDGreenButton(ButtonEntity):
    """Button to turn LED green."""

    def __init__(self, coordinator: EkeyDataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the button."""
        self.coordinator = coordinator
        self._attr_name = "ekey LED Green"
        self._attr_unique_id = f"{entry.entry_id}_led_green"
        self._attr_icon = "mdi:led-on"

        host = entry.data.get(CONF_DAEMON_HOST, "localhost")
        port = entry.data.get(CONF_DAEMON_PORT, 8080)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{host}:{port}")},
            name=f"ekey Scanner ({host}:{port})",
        )

    async def async_press(self) -> None:
        """Handle the button press."""
        await self.coordinator.set_led_state(4)  # 4 = green


class EkeyLEDRedButton(ButtonEntity):
    """Button to turn LED red."""

    def __init__(self, coordinator: EkeyDataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the button."""
        self.coordinator = coordinator
        self._attr_name = "ekey LED Red"
        self._attr_unique_id = f"{entry.entry_id}_led_red"
        self._attr_icon = "mdi:led-on"

        host = entry.data.get(CONF_DAEMON_HOST, "localhost")
        port = entry.data.get(CONF_DAEMON_PORT, 8080)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{host}:{port}")},
            name=f"ekey Scanner ({host}:{port})",
        )

    async def async_press(self) -> None:
        """Handle the button press."""
        await self.coordinator.set_led_state(5)  # 5 = red


# Removed buttons, and where their job lives now.
#
# "Enroll" / "Delete" (removed earlier): they only raised a notification pointing
#   at Developer Tools. Enrolment is now the panel's Enroll dialog, with live
#   progress; the ekey_ha_app.enroll_fingerprint and .delete_fingerprint services
#   remain for scripted use.
#
# "Check Orphaned Fingerprints": the panel lists unassigned fingerprints
#   continuously under the user list and can assign one to a user in two clicks.
#   The button could only tell you a count and print curl commands into a
#   notification — it could not fix anything, which is why it goes.
#
# "Person Fingerprints": rendered the person -> finger map into a persistent
#   notification. That IS the panel's user list, live and editable.
#
# Both of those read person_map, which is why this file no longer imports it.
