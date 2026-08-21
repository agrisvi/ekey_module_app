"""Sensor platform for ekey Home Assistant App."""
import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.storage import Store

from . import person_map
from .const import (
    DOMAIN,
    CONF_DAEMON_HOST,
    CONF_DAEMON_PORT,
    EVENT_FINGER_TOUCH,
    EVENT_FINGERPRINT_MATCHED,
    EVENT_FINGERPRINT_NOT_MATCHED,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .coordinator import EkeyDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ekey sensor platform."""
    coordinator: EkeyDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    sensors = [
        EkeyDeviceInfoSensor(coordinator, entry),
        EkeyFingerprintCountSensor(coordinator, entry),
        EkeyLastAccessSensor(entry),
    ]

    async_add_entities(sensors)


class EkeyDeviceInfoSensor(CoordinatorEntity, SensorEntity):
    """Sensor showing ekey scanner device information."""

    def __init__(self, coordinator: EkeyDataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_name = "ekey Scanner Info"
        self._attr_unique_id = f"{entry.entry_id}_device_info"
        self._attr_device_class = None
        self._entry = entry
        
        host = entry.data.get(CONF_DAEMON_HOST, "localhost")
        port = entry.data.get(CONF_DAEMON_PORT, 8080)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{host}:{port}")},
            name=f"ekey Scanner ({host}:{port})",
        )

    @property
    def native_value(self) -> str | None:
        """Return the state of the sensor."""
        if not self.coordinator.data or "device" not in self.coordinator.data:
            return None
        
        device = self.coordinator.data["device"]
        sw_version = device.get("sw_version", "unknown")
        return f"v{sw_version}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return device attributes."""
        if not self.coordinator.data or "device" not in self.coordinator.data:
            return {}
        
        device = self.coordinator.data["device"]
        return {
            "fw_api_version": device.get("fw_api_version"),
            "sw_version": device.get("sw_version"),
            "prod_sn": device.get("prod_sn"),
            "prod_sn_pcb": device.get("prod_sn_pcb"),
            "hw_version": device.get("hw_version"),
            "dev_typ": device.get("dev_typ"),
            "dev_sub_typ": device.get("dev_sub_typ"),
            "dev_line": device.get("dev_line"),
            "dev_variant": device.get("dev_variant"),
            "dev_sub_variant": device.get("dev_sub_variant"),
        }


class EkeyFingerprintCountSensor(CoordinatorEntity, SensorEntity):
    """Sensor showing number of enrolled fingerprints."""

    def __init__(self, coordinator: EkeyDataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_name = "ekey Enrolled Fingerprints"
        self._attr_unique_id = f"{entry.entry_id}_fingerprint_count"
        self._attr_native_unit_of_measurement = "fingerprints"
        self._entry = entry
        
        host = entry.data.get(CONF_DAEMON_HOST, "localhost")
        port = entry.data.get(CONF_DAEMON_PORT, 8080)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{host}:{port}")},
            name=f"ekey Scanner ({host}:{port})",
        )

    @property
    def native_value(self) -> int | None:
        """Return the number of enrolled fingerprints."""
        if not self.coordinator.data or "fingerprints" not in self.coordinator.data:
            return None
        
        fingerprints = self.coordinator.data["fingerprints"]
        return fingerprints.get("num_aps", 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return fingerprint list."""
        if not self.coordinator.data or "fingerprints" not in self.coordinator.data:
            return {}

        fingerprints = self.coordinator.data["fingerprints"]
        return {
            "fingerprints": fingerprints.get("aps", []),
        }


class EkeyLastAccessSensor(SensorEntity):
    """Sensor that records the most recent fingerprint access result.

    Its state changes on every fingerprint event (granted or denied).
    HA's recorder always stores entity state changes, so every access
    attempt automatically appears in the device Activity tab and the
    global Logbook — no logbook platform discovery required.
    """

    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        self._attr_name = "ekey Last Access"
        self._attr_unique_id = f"{entry.entry_id}_last_access"
        self._attr_native_value = None
        self._entry = entry

        host = entry.data.get(CONF_DAEMON_HOST, "localhost")
        port = entry.data.get(CONF_DAEMON_PORT, 8080)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{host}:{port}")},
        )

    async def async_added_to_hass(self) -> None:
        """Register event listeners when entity joins HA."""
        self.async_on_remove(
            self.hass.bus.async_listen(EVENT_FINGER_TOUCH, self._on_finger_touch)
        )
        self.async_on_remove(
            self.hass.bus.async_listen(EVENT_FINGERPRINT_MATCHED, self._on_matched)
        )
        self.async_on_remove(
            self.hass.bus.async_listen(EVENT_FINGERPRINT_NOT_MATCHED, self._on_not_matched)
        )

    @callback
    def _on_finger_touch(self, event) -> None:
        """Record finger touch — shown in activity log before the match result arrives."""
        self._attr_native_value = "Notify fingerprint touch"
        self.async_write_ha_state()

    @callback
    def _on_matched(self, event) -> None:
        """Schedule async person-name resolution and state update."""
        self.hass.async_create_task(
            self._resolve_and_set_granted(event.data.get("apid", ""))
        )

    async def _resolve_and_set_granted(self, apid: str) -> None:
        """Look up person name in storage, then update sensor state."""
        person_name = "Unknown"
        finger = None

        if apid:
            try:
                # The backend's user document is authoritative; person_map renders
                # it into the legacy shape this loop already understands, and falls
                # back to the preserved legacy map when the backend is unreachable.
                data = await person_map.async_person_map(self.hass, self._entry.entry_id)
                for pid, pdata in data.items():
                    for fid, stored_apid in pdata.get("fingerprints", {}).items():
                        if stored_apid == apid:
                            person_state = self.hass.states.get(pid)
                            if person_state:
                                person_name = person_state.attributes.get(
                                    "friendly_name", pid
                                )
                            finger = int(fid)
                            break
                    if finger is not None:
                        break
            except Exception as err:
                _LOGGER.warning("Could not resolve person for APID %s: %s", apid[:8], err)

        value = f"Granted: {person_name}"
        if finger is not None:
            value += f" (finger {finger})"

        _LOGGER.info("ekey last access → %s", value)
        self._attr_native_value = value
        self.async_write_ha_state()

    @callback
    def _on_not_matched(self, event) -> None:
        """Update sensor state immediately on denial (no async lookup needed)."""
        reason = event.data.get("apfar_desc", "unknown reason")
        value = f"Denied: {reason}"
        _LOGGER.info("ekey last access → %s", value)
        self._attr_native_value = None
        self.async_write_ha_state()
        self._attr_native_value = value
        self.async_write_ha_state()
