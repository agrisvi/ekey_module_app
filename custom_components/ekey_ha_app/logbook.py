"""Logbook support for ekey Home Assistant App."""
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN, EVENT_ACCESS_GRANTED, EVENT_ACCESS_DENIED


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event,
) -> None:
    """Describe logbook events for ekey access activity."""

    @callback
    def describe_access_granted(event) -> dict:
        person_name = event.data.get("person_name", "Unknown")
        finger = event.data.get("finger")
        entity_id = event.data.get("entity_id")
        msg = f"access granted: {person_name}"
        if finger:
            msg += f" (finger {finger})"
        result = {"name": "ekey", "message": msg}
        if entity_id:
            result["entity_id"] = entity_id
        return result

    @callback
    def describe_access_denied(event) -> dict:
        reason = event.data.get("apfar_desc", "unknown reason")
        entity_id = event.data.get("entity_id")
        result = {"name": "ekey", "message": f"access denied: {reason}"}
        if entity_id:
            result["entity_id"] = entity_id
        return result

    # (domain, event_name, describe_callback) — three arguments. Called with two, the
    # whole logbook platform fails to load with a TypeError at startup, and every ekey
    # access event then appears in the logbook as a raw event with no description.
    async_describe_event(DOMAIN, EVENT_ACCESS_GRANTED, describe_access_granted)
    async_describe_event(DOMAIN, EVENT_ACCESS_DENIED, describe_access_denied)
