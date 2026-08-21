"""Services for ekey Home Assistant App."""
import logging
import uuid
import asyncio
import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store  # noqa: F401 — re-exported, see below

from . import person_map
from .api import EkeyApiError
from .const import DOMAIN, STORAGE_KEY, STORAGE_VERSION  # noqa: F401 — re-exported

_LOGGER = logging.getLogger(__name__)

SERVICE_ENROLL_FINGERPRINT = "enroll_fingerprint"
SERVICE_DELETE_FINGERPRINT = "delete_fingerprint"
SERVICE_SET_LED_BRIGHTNESS = "set_led_brightness"

ATTR_PERSON_ID = "person_id"
ATTR_FINGER = "finger"
ATTR_BRIGHTNESS = "brightness"
ATTR_SCANNER = "scanner"

SERVICE_ENROLL_FINGERPRINT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_PERSON_ID): cv.string,
        vol.Required(ATTR_FINGER): vol.All(vol.Coerce(int), vol.Range(min=1, max=10)),
        vol.Optional(ATTR_SCANNER): cv.string,
    }
)

SERVICE_DELETE_FINGERPRINT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_PERSON_ID): cv.string,
        vol.Required(ATTR_FINGER): vol.All(vol.Coerce(int), vol.Range(min=1, max=10)),
        vol.Optional(ATTR_SCANNER): cv.string,
    }
)

SERVICE_SET_LED_BRIGHTNESS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_BRIGHTNESS): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
        vol.Optional(ATTR_SCANNER): cv.string,
    }
)


async def _strip_apid_from_backend(hass: HomeAssistant, entry_id: str, apid: str) -> None:
    """Remove one APID from the backend's user document, best effort.

    Called after the sensor has confirmed the template is gone. Best effort on
    purpose: the authoritative deletion already happened on the sensor, and a
    momentarily unreachable backend must not turn a successful delete into a
    service error. The stale entry then shows as "missing on scanner" in the panel,
    which is exactly the signal that says what to do about it.
    """
    bucket = (hass.data.get(DOMAIN) or {}).get(entry_id) or {}
    client = bucket.get("app_client")
    coordinator = bucket.get("app_coordinator")
    if client is None or not (getattr(coordinator, "data", None) or {}).get("app_api"):
        return
    try:
        users = await client.async_get_users()
        touched = False
        for user in users:
            fingers = [
                f for f in (user.get("fingers") or [])
                if not (isinstance(f, dict) and f.get("apid") == apid)
            ]
            if len(fingers) != len(user.get("fingers") or []):
                touched = True
            user["fingers"] = fingers
        if touched:
            await client.async_put_users(users)
            await coordinator.async_request_refresh()
    except EkeyApiError as err:
        _LOGGER.warning(
            "Deleted %s from the scanner but could not update the backend user list: %s",
            apid[:8], err,
        )


def _resolve_entry_id(hass: HomeAssistant, call: ServiceCall) -> str:
    """Resolve which scanner (config entry) a service call targets.

    Uses the optional ``scanner`` device_id when given; otherwise falls back to the
    sole configured scanner, and raises a clear error when the choice is ambiguous.
    """
    entries = hass.data.get(DOMAIN, {})
    dev_id = call.data.get(ATTR_SCANNER)
    if dev_id:
        device = dr.async_get(hass).async_get(dev_id)
        for eid in (device.config_entries if device else ()):
            if eid in entries:
                return eid
        raise HomeAssistantError(
            f"Selected device '{dev_id}' is not a configured ekey scanner."
        )
    if len(entries) == 1:
        return next(iter(entries))
    if not entries:
        raise HomeAssistantError("No ekey scanner is configured.")
    raise HomeAssistantError(
        "Multiple ekey scanners are configured — specify the 'scanner' field."
    )


async def async_setup_services(hass: HomeAssistant) -> None:
    """Set up services for ekey integration.

    These are the original person-based services and they keep working exactly as
    documented, because scripts, the shipped blueprints and users' dashboards call
    them. What changed underneath is where the truth lives: the backend's user
    document is authoritative now, so lookups go through :mod:`person_map` (which
    prefers the backend and falls back to the preserved legacy map) and deletions
    are mirrored into the backend as well as the legacy map.
    """
    store = person_map.async_get_store(hass)

    async def handle_enroll_fingerprint(call: ServiceCall) -> None:
        """Handle enroll_fingerprint service call."""
        person_id = call.data[ATTR_PERSON_ID]
        finger = call.data[ATTR_FINGER]
        
        # Resolve which scanner to enroll on (from the optional 'scanner' field).
        entry_id = _resolve_entry_id(hass, call)
        coordinator = hass.data[DOMAIN][entry_id]["coordinator"]

        # Generate unique APID (UUID)
        apid = str(uuid.uuid4())

        _LOGGER.info("Starting fingerprint enrollment for %s, finger %s on %s, APID %s",
                     person_id, finger, coordinator.base_url, apid)

        try:
            # Get person friendly name first
            person_name = person_id
            person_state = hass.states.get(person_id)
            if person_state:
                person_name = person_state.attributes.get("friendly_name", person_id)

            # Store in the TARGET scanner's pending enrollments so that scanner's SSE
            # listener drives the enrollment-complete flow.
            pending = hass.data[DOMAIN][entry_id].get("pending_enrollments", {})
            pending[apid] = {
                "person_id": person_id,
                "person_name": person_name,
                "finger": finger,
            }
            _LOGGER.debug("Stored pending enrollment: %s finger %d -> %s", person_id, finger, apid[:8] + "...")
            
            # Start enrollment asynchronously with shorter timeout
            # The enrollment progress will be monitored via SSE events
            try:
                # Use asyncio.wait_for with 5 second timeout
                # If daemon doesn't respond quickly, we'll still monitor via SSE
                result = await asyncio.wait_for(
                    coordinator.enroll_fingerprint(apid),
                    timeout=5.0
                )
                _LOGGER.info("Enrollment command accepted: %s", result)

                # Check if enrollment was rejected by daemon
                if result.get("rpc_error_code") == "Error":
                    error_msg = result.get("error_message", "Unknown error")
                    _LOGGER.error("Enrollment rejected by daemon: %s", error_msg)
                    raise Exception(f"Enrollment rejected: {error_msg}")
                    
            except asyncio.TimeoutError:
                _LOGGER.warning("Enrollment command timed out, but will monitor via SSE events")
                # Don't raise - enrollment might still work via SSE monitoring
            except Exception as enroll_err:
                _LOGGER.warning("Enrollment command failed: %s (will monitor via SSE)", enroll_err)
                # Don't raise - enrollment might still work via SSE monitoring
            
            # Fire event to indicate enrollment started
            hass.bus.async_fire("ekey_enrollment_started", {
                "person_id": person_id,
                "person_name": person_name,
                "finger": finger,
                "apid": apid,
                "status": "monitoring",
            })
            
            # Create initial notification
            await hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "🔐 Enrolling Fingerprint...",
                    "message": (
                        f"**Person:** {person_name}\n"
                        f"**Finger:** {finger}\n\n"
                        f"**Status:** Starting enrollment...\n\n"
                        f"Please place your finger on the scanner when prompted."
                    ),
                    "notification_id": f"ekey_enrollment_{apid}",
                },
            )
            
        except Exception as err:
            _LOGGER.error("Failed to enroll fingerprint: %s", err)
            raise
    
    async def handle_delete_fingerprint(call: ServiceCall) -> None:
        """Handle delete_fingerprint service call."""
        person_id = call.data[ATTR_PERSON_ID]
        finger = call.data[ATTR_FINGER]

        entry_id = _resolve_entry_id(hass, call)
        coordinator = hass.data[DOMAIN][entry_id]["coordinator"]

        _LOGGER.info("Deleting fingerprint for %s, finger %s on %s",
                     person_id, finger, coordinator.base_url)

        try:
            # The effective map: the backend when it is reachable, else the
            # preserved legacy one. Using it here means this service can also
            # delete a fingerprint that was enrolled from the panel.
            effective = await person_map.async_person_map(hass, entry_id)

            if person_id in effective and str(finger) in effective[person_id].get("fingerprints", {}):
                apid = effective[person_id]["fingerprints"][str(finger)]

                # Delete from scanner
                success = await coordinator.delete_fingerprint(apid)

                if success:
                    # Remove from the legacy map when it is mentioned there. The
                    # legacy map is never deleted wholesale, but an entry that has
                    # been acted on should not keep pointing at a template the
                    # sensor no longer holds.
                    data = await person_map.async_load(hass)
                    legacy = data.get("legacy") or {}
                    if person_id in legacy:
                        legacy[person_id].get("fingerprints", {}).pop(str(finger), None)
                        if not legacy[person_id].get("fingerprints"):
                            legacy.pop(person_id, None)
                        await person_map.async_save(hass, data)

                    # And from the backend document, which is the source of truth.
                    await _strip_apid_from_backend(hass, entry_id, apid)

                    hass.bus.async_fire("ekey_ha_storage_updated")

                    _LOGGER.info("Fingerprint deleted successfully from storage: %s", apid)

                    hass.bus.async_fire("ekey_fingerprint_deleted", {
                        "person_id": person_id,
                        "finger": finger,
                        "apid": apid,
                    })
                else:
                    _LOGGER.error("Failed to delete fingerprint from scanner")
            else:
                _LOGGER.warning("No fingerprint found for %s, finger %s", person_id, finger)
        
        except Exception as err:
            _LOGGER.error("Failed to delete fingerprint: %s", err)
            raise
    
    async def handle_set_led_brightness(call: ServiceCall) -> None:
        """Handle set_led_brightness service call."""
        brightness = call.data[ATTR_BRIGHTNESS]

        coordinator = hass.data[DOMAIN][_resolve_entry_id(hass, call)]["coordinator"]

        _LOGGER.info("Setting LED brightness to %s on %s", brightness, coordinator.base_url)

        try:
            result = await coordinator.set_led_brightness(brightness)
            _LOGGER.debug("LED brightness set: %s", result)
        except Exception as err:
            _LOGGER.error("Failed to set LED brightness: %s", err)
            raise
    
    # Register services
    hass.services.async_register(
        DOMAIN,
        SERVICE_ENROLL_FINGERPRINT,
        handle_enroll_fingerprint,
        schema=SERVICE_ENROLL_FINGERPRINT_SCHEMA,
    )
    
    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_FINGERPRINT,
        handle_delete_fingerprint,
        schema=SERVICE_DELETE_FINGERPRINT_SCHEMA,
    )
    
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_LED_BRIGHTNESS,
        handle_set_led_brightness,
        schema=SERVICE_SET_LED_BRIGHTNESS_SCHEMA,
    )


async def async_unload_services(hass: HomeAssistant) -> None:
    """Unload ekey services."""
    hass.services.async_remove(DOMAIN, SERVICE_ENROLL_FINGERPRINT)
    hass.services.async_remove(DOMAIN, SERVICE_DELETE_FINGERPRINT)
    hass.services.async_remove(DOMAIN, SERVICE_SET_LED_BRIGHTNESS)
