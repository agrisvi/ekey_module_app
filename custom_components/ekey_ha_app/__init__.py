"""ekey Home Assistant App - Integration for ekey dLine fingerprint scanner."""
import logging
import asyncio
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.start import async_at_started

from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN,
    CONF_DAEMON_HOST,
    CONF_DAEMON_PORT,
    CONF_SSL,
    CONF_TOKEN,
    CONF_VERIFY_SSL,
    DEFAULT_VERIFY_SSL,
    EVENT_ENROLLMENT_STATE,
    ENROLL_STATE_WAIT_FOR_CONFIRMATION,
    EVENT_FINGERPRINT_MATCHED,
    EVENT_FINGERPRINT_NOT_MATCHED,
    EVENT_ACCESS_GRANTED,
    EVENT_ACCESS_DENIED,
)
from .api import EkeyApiError, EkeyAppClient, EkeyAuthError
from .app_coordinator import EkeyAppCoordinator
from .connection import EkeyConnection, get_session
from .coordinator import EkeyDataUpdateCoordinator
from .enroll import EnrollManager
from .panel import async_register_panel, async_remove_panel
from .sse_listener import EkeySSEListener
from .services import async_setup_services, async_unload_services
from . import person_map, ws_api

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON]

# Entities earlier versions created and this one does not.
#
# All five existed to build a user interface out of entities, because there was no
# user interface: two selects to pick a person and a finger for the enrolment
# script blueprint, a third that was a read-only list dressed up as a dropdown, and
# two buttons that printed information into persistent notifications. The sidebar
# panel does all of it directly, so they are gone.
#
# Home Assistant keeps a registry row for every entity it has ever seen, and a row
# whose platform no longer creates it shows in the UI as "unavailable" — including
# inside the user's existing dashboards and automation editors. Deleting the rows
# here is the difference between an upgrade that cleans up after itself and one
# that leaves five broken entities behind for the user to find.
_RETIRED_ENTITIES: tuple[tuple[str, str], ...] = (
    (Platform.SELECT, "_person_selector"),
    (Platform.SELECT, "_finger_selector"),
    (Platform.SELECT, "_enrolled_fingerprints"),
    (Platform.BUTTON, "_check_orphaned"),
    (Platform.BUTTON, "_show_fingerprints"),
)

# hass.data[DOMAIN] is keyed by config-entry id, plus a few underscore-prefixed
# bookkeeping keys (the shared person store, the panel-registered flag, the
# services-registered flag). Anything counting *entries* must filter those out —
# the previous `len(hass.data[DOMAIN]) == 1` test would otherwise silently stop
# being a test of "is this the first entry".
_SERVICES_REGISTERED = "_services_registered"


def _entry_buckets(hass: HomeAssistant) -> dict[str, Any]:
    """The per-config-entry buckets in ``hass.data[DOMAIN]``, without the extras."""
    return {
        key: value
        for key, value in (hass.data.get(DOMAIN) or {}).items()
        if not key.startswith("_")
    }


@callback
def _async_remove_retired_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete registry rows for the entities listed in ``_RETIRED_ENTITIES``.

    Idempotent and silent when there is nothing to remove, which is the normal case
    on a fresh install. Runs before the platforms are forwarded so a user never sees
    the ghost entity, not even for the moment it takes to set up.
    """
    registry = er.async_get(hass)
    for domain, suffix in _RETIRED_ENTITIES:
        entity_id = registry.async_get_entity_id(
            domain, DOMAIN, f"{entry.entry_id}{suffix}"
        )
        if entity_id is not None:
            _LOGGER.info(
                "Removing %s — superseded by the ekey panel in the sidebar", entity_id
            )
            registry.async_remove(entity_id)


_RETIRED_REFERENCE_ISSUE = "retired_entities_referenced"


@callback
def _async_retired_entity_ids(hass: HomeAssistant, entry: ConfigEntry) -> list[str]:
    """The entity ids this entry's retired entities used to have.

    Recovered from the registry's ``deleted_entities``, not remembered by us. Home
    Assistant keeps a row there for everything it has ever removed, keyed by
    (domain, platform, unique_id) — which is exactly what ``_RETIRED_ENTITIES``
    describes. Reading it back means the check below also works for an installation
    that was upgraded before this check existed, where the rows were removed on an
    earlier start and there is nothing left in memory to have remembered.
    """
    registry = er.async_get(hass)
    ids: list[str] = []
    for domain, suffix in _RETIRED_ENTITIES:
        deleted = registry.deleted_entities.get(
            (domain, DOMAIN, f"{entry.entry_id}{suffix}")
        )
        if deleted is not None:
            ids.append(deleted.entity_id)
    return ids


@callback
def _async_check_retired_references(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Raise a repair when an automation or script still points at a retired entity.

    Removing the registry rows in ``_async_remove_retired_entities`` cleans up the
    entity list, but it says nothing to whoever was USING one — and the symptom on
    the other side is genuinely baffling. An automation built from the old
    relay-pulse blueprint referenced ``select.<device>_enrolled_fingerprints``; with
    the entity gone, its entity picker shows "Unknown entity selected" over an empty
    dropdown, with nothing anywhere naming the cause. This turns that dead end into
    a sentence, which is the whole job.

    Deliberately not fixable in place: the fix is to re-import the blueprint and
    choose the relay again, and the inputs changed, so there is nothing an automatic
    repair could safely rewrite. The issue clears itself on the next start once no
    automation references the entity any more.

    Runs at Home Assistant start rather than during setup, because
    ``automations_with_entity`` reads the automation component's loaded entities and
    would honestly report "nothing references it" if asked too early.
    """
    # Imported here, not at module scope: this is the only place that needs them, and
    # a custom component should not pull the automation and script components into
    # every import of its own package.
    from homeassistant.components.automation import automations_with_entity
    from homeassistant.components.script import scripts_with_entity

    issue_id = f"{_RETIRED_REFERENCE_ISSUE}_{entry.entry_id}"

    referenced: dict[str, list[str]] = {}
    for entity_id in _async_retired_entity_ids(hass, entry):
        users = automations_with_entity(hass, entity_id) + scripts_with_entity(
            hass, entity_id
        )
        if users:
            referenced[entity_id] = users

    if not referenced:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return

    entities = ", ".join(sorted(referenced))
    users = ", ".join(sorted({user for users in referenced.values() for user in users}))
    _LOGGER.warning(
        "These automations or scripts still reference entities this version removed "
        "(%s): %s. They cannot run until they are updated — see blueprints/README.md",
        entities,
        users,
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=_RETIRED_REFERENCE_ISSUE,
        translation_placeholders={"entities": entities, "users": users},
    )


def _fmt_apid(apid: str | None) -> str:
    """Return a short, log-safe representation of an APID."""
    return (apid[:8] + "...") if apid else "None"


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entries to the current format.

    v1 → v2 adds the two-mode connection fields (ssl / token / verify_ssl).
    Existing entries were all local HTTP with no token, so they migrate to
    ``ssl=False, token=None, verify_ssl=False`` — behaviour is unchanged.
    """
    _LOGGER.debug("Migrating config entry from version %s", entry.version)

    if entry.version > 2:
        # Downgrade (newer entry on older code) is not supported.
        return False

    if entry.version == 1:
        new_data = dict(entry.data)

        # Legacy key names (pre-CONF_HOST/CONF_PORT).
        if "daemon_host" in entry.data:
            new_data[CONF_DAEMON_HOST] = entry.data["daemon_host"]
            new_data.pop("daemon_host", None)
            _LOGGER.info("Migrated 'daemon_host' to '%s'", CONF_DAEMON_HOST)

        if "daemon_port" in entry.data:
            new_data[CONF_DAEMON_PORT] = entry.data["daemon_port"]
            new_data.pop("daemon_port", None)
            _LOGGER.info("Migrated 'daemon_port' to '%s'", CONF_DAEMON_PORT)

        # Ensure required keys exist with defaults if missing.
        if CONF_DAEMON_HOST not in new_data:
            new_data[CONF_DAEMON_HOST] = "localhost"
            _LOGGER.warning("Missing host in config entry, defaulting to localhost")

        if CONF_DAEMON_PORT not in new_data:
            new_data[CONF_DAEMON_PORT] = 8080
            _LOGGER.warning("Missing port in config entry, defaulting to 8080")

        # Backfill the two-mode connection fields (existing = local HTTP, no token).
        new_data.setdefault(CONF_SSL, False)
        new_data.setdefault(CONF_TOKEN, None)
        new_data.setdefault(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)

        # Bump to version 2. ``async_update_entry`` accepts ``version`` on newer
        # HA; older HA needs the attribute set directly.
        try:
            hass.config_entries.async_update_entry(entry, data=new_data, version=2)
        except TypeError:
            entry.version = 2
            hass.config_entries.async_update_entry(entry, data=new_data)

        _LOGGER.info("Migration to version 2 complete")

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up ekey HA App from a config entry."""
    # One descriptor for the backend: local daemon (HTTP) or remote ESP32
    # (HTTPS + token). host/port are also kept for log lines and registry ids.
    conn = EkeyConnection.from_entry(entry)
    host = conn.host
    port = conn.port

    _LOGGER.info("Setting up ekey HA App for %s (%s)", conn.base_url,
                 "token auth" if conn.token else "no token")

    session = get_session(hass, conn)

    # Create coordinator for polling data
    coordinator = EkeyDataUpdateCoordinator(hass, session, conn)
    
    # Try initial refresh, but allow setup to complete even if it fails
    # (coordinator will keep retrying in the background)
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        _LOGGER.warning("Initial refresh failed (will retry): %s", err)
        # Don't raise - allow setup to complete so entities are created
    
    # Register device in device registry
    device_registry = dr.async_get(hass)
    device_info = coordinator.data.get("device", {}) if coordinator.data else {}
    
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{host}:{port}")},
        name=f"ekey Scanner ({host}:{port})",
        manufacturer="ekey",
        model=f"dLine {device_info.get('dev_typ', 'Unknown')}",
        sw_version=device_info.get("sw_version", "Unknown"),
        hw_version=str(device_info.get("hw_version", "Unknown")),
        serial_number=device_info.get("prod_sn", None),
        configuration_url=conn.base_url,
    )

    #
    # Create SSE listener for async events
    sse_listener = EkeySSEListener(hass, session, conn, entry.entry_id)
    
    # The app layer: the backend owns users, their finger slots, actions and
    # automations. This integration is a front-end for that document, never a
    # second source of truth — so there is a client, a coordinator that watches it,
    # and an enrolment engine, all separate from the scanner coordinator above
    # (which polls /api/v1 with update_interval=None and a fixed data shape that
    # entities and a shipped blueprint depend on).
    app_client = EkeyAppClient(conn, session)
    app_coordinator = EkeyAppCoordinator(hass, app_client, entry.entry_id, config_entry=entry)
    enroll_manager = EnrollManager(hass, entry.entry_id, app_client, app_coordinator)

    # Store coordinator, listener, and pending enrollments in hass.data
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "sse_listener": sse_listener,
        "pending_enrollments": {},  # APID -> {person_id, finger}
        # APIDs owned by the panel's enrolment engine. The legacy enrollment
        # listeners below skip these: they auto-confirm every APID they see, which
        # would race the engine's single deliberate confirmation.
        "panel_enrollments": {},
        "app_client": app_client,
        "app_coordinator": app_coordinator,
        "enroll_manager": enroll_manager,
    }
    enroll_manager.async_attach()

    # First app-layer read. Allowed to fail: a backend with no app layer, or one
    # that is briefly unreachable, must not stop the scanner half from loading.
    try:
        await app_coordinator.async_config_entry_first_refresh()
    except EkeyAuthError as err:
        # A rotated token (a factory reset does that) is a reauth, not an outage.
        _LOGGER.warning("App layer rejected our token: %s", err)
        entry.async_start_reauth(hass)
    except Exception as err:  # noqa: BLE001 — see above; never block setup
        _LOGGER.warning("App layer not available yet (will retry): %s", err)

    # Fold the legacy HA-side person→APID map into the backend, once per scanner.
    # Deliberately after the first successful read and never destructive: the v1
    # map stays under `legacy` in HA storage forever, so a bad reconcile is
    # recoverable by hand.
    if (app_coordinator.data or {}).get("app_api"):
        try:
            report = await person_map.async_reconcile(hass, app_client)
            if report.conflicts:
                _LOGGER.warning(
                    "Some legacy fingerprint mappings need attention on %s: %s",
                    conn.scanner_id, "; ".join(report.conflicts),
                )
        except EkeyApiError as err:
            _LOGGER.warning("Could not reconcile the legacy person map: %s", err)


    # Start SSE listener task
    entry.async_create_background_task(
        hass,
        sse_listener.start(),
        "ekey_sse_listener"
    )
    
    # Set up automatic enrollment confirmation
    # When enrollment reaches wait_for_confirmation state (enstat=35), automatically confirm
    async def handle_enrollment_state(event):
        """Handle enrollment state changes: update progress notification and auto-confirm when ready.

        This single listener replaces the previous two separate listeners that both
        subscribed to EVENT_ENROLLMENT_STATE, which caused every state-change to be
        processed twice (and the auto-confirm path to fire up to 4× when combined
        with the now-removed duplicate fire in sse_listener.py).
        """
        enstat = event.data.get("enstat")
        apid = event.data.get("apid")
        entryc = event.data.get("entryc", 0)
        ennumtpl = event.data.get("ennumtpl", 0)
        enextres = event.data.get("enextres", 0)
        enaccres = event.data.get("enaccres", 0)

        _LOGGER.debug(
            "Enrollment state event: enstat=%s, apid=%s", enstat, _fmt_apid(apid)
        )

        if not apid:
            _LOGGER.warning("Enrollment state event: no APID in event data")
            return

        # Enrollments started from the panel are owned by EnrollManager, which
        # confirms once and deliberately. This listener auto-confirms every APID it
        # sees, so it must stay out of the way — two confirmations for one session
        # is a real failure mode, not a harmless duplicate.
        if apid in hass.data[DOMAIN][entry.entry_id].get("panel_enrollments", {}):
            return

        pending = hass.data[DOMAIN][entry.entry_id].get("pending_enrollments", {})

        # ── Part 1: update the real-time progress notification ──────────────────
        if apid in pending:
            person_name = pending[apid].get("person_name", pending[apid].get("person_id", "Unknown"))
            finger = pending[apid].get("finger", "?")

            _LOGGER.debug(">>> Updating notification for %s finger %s", person_name, finger)

            from .const import ENROLLMENT_STATES
            state_name = ENROLLMENT_STATES.get(enstat, f"unknown({enstat})")

            # Build status message based on state
            if enstat == 10:  # wait
                status_msg = "⏳ Waiting for device to be ready..."
            elif enstat == 20:  # acquire
                status_msg = f"👆 Scanning finger... (try {entryc}/{ennumtpl} templates collected)"
            elif enstat == 30:  # step_done
                if enextres == 0:  # ok
                    if enaccres == 0:  # accepted
                        status_msg = f"✓ Template {ennumtpl} accepted! Please scan again..."
                    elif enaccres == 20:  # too_small_displacement
                        status_msg = f"⚠️ Move finger slightly and try again (try {entryc})"
                    elif enaccres == 30:  # no_match_with_center
                        status_msg = f"⚠️ Use the same finger! (try {entryc})"
                    else:
                        status_msg = f"⚠️ Try again (try {entryc})"
                elif enextres == 20:  # fingertip
                    status_msg = "⚠️ Place finger more centered on sensor"
                elif enextres == 30:  # soiled sensor
                    status_msg = "⚠️ Please clean the sensor"
                elif enextres == 40:  # too_small
                    status_msg = "⚠️ Place finger more fully on sensor"
                elif enextres == 50:  # biometric
                    status_msg = "⚠️ Poor quality - try cleaning finger"
                elif enextres == 60:  # cold_dry
                    status_msg = "⚠️ Finger too dry - moisten slightly"
                elif enextres == 70:  # wet
                    status_msg = "⚠️ Finger too wet - dry it first"
                else:
                    status_msg = f"⚠️ Try again (try {entryc})"
            elif enstat == 35:  # wait_for_confirmation
                status_msg = f"✓ Enrollment complete! Confirming... ({ennumtpl} templates collected)"
            elif enstat == 40:  # finished_success
                status_msg = f"✅ SUCCESS! Fingerprint enrolled ({ennumtpl} templates)"
            elif enstat == 50:  # quit by user
                status_msg = "❌ Enrollment cancelled by user"
            elif enstat == 60:  # timeout
                status_msg = "⏱️ Enrollment timed out - please try again"
            elif enstat == 70:  # duplicate
                status_msg = "⚠️ This fingerprint is already enrolled!"
            else:
                status_msg = f"Status: {state_name}"

            warning_msg = ""
            if ennumtpl > 6:
                warning_msg = (
                    "\n\n⚠️ **Too many tries!** The scanner is struggling. Make sure to:\n"
                    "- Place your **entire finger** flat on the sensor\n"
                    "- Use **consistent placement** for each scan\n"
                    "- Clean your finger and the sensor if needed"
                )

            await hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "🔐 Enrolling Fingerprint...",
                    "message": (
                        f"**Person:** {person_name}\n"
                        f"**Finger:** {finger}\n\n"
                        f"**Progress:** {ennumtpl}/4 templates\n"
                        f"**Tries:** {entryc}\n\n"
                        f"{status_msg}{warning_msg}"
                    ),
                    "notification_id": f"ekey_enrollment_{apid}",
                },
            )
            _LOGGER.debug(">>> Enrollment notification updated: %s", status_msg)
        else:
            _LOGGER.warning(
                "Enrollment state event: APID %s not found in pending enrollments",
                _fmt_apid(apid),
            )

        # ── Part 2: auto-confirm when scanner is ready ───────────────────────────
        #
        # This only *sends* the confirmation. The fingerprint is saved to storage
        # and the success notification is shown by ``handle_enrollment_complete``
        # when the scanner reports the real terminal state (enstat=40). Keeping a
        # single owner for the success path avoids the previous double-save and the
        # spurious "APID not found in pending" warning.
        if enstat == ENROLL_STATE_WAIT_FOR_CONFIRMATION:
            _LOGGER.info(
                "Enrollment ready for confirmation (enstat=35), auto-confirming APID=%s",
                _fmt_apid(apid),
            )

            # Wait 1 second to ensure scanner is fully ready
            await asyncio.sleep(1)

            # Re-fetch pending (may have been mutated during the sleep)
            pending = hass.data[DOMAIN][entry.entry_id].get("pending_enrollments", {})

            try:
                result = await coordinator.confirm_enrollment(apid)
                _LOGGER.info("Enrollment confirmation sent, response: %s", result)

                rpc_error = result.get("rpc_error_code", "").upper() if isinstance(result, dict) else ""

                if rpc_error == "OK":
                    # Success is finalized on the enstat=40 SSE event; nothing to
                    # save here. The progress notification stays up until then.
                    _LOGGER.info(
                        "Confirmation accepted (OK) - awaiting finished_success (enstat=40) for APID=%s",
                        _fmt_apid(apid),
                    )

                else:
                    _LOGGER.error(
                        "Enrollment confirmation failed with response: %s",
                        rpc_error or result,
                    )

                    person_name = pending[apid].get("person_name", pending[apid].get("person_id", "Unknown")) if apid in pending else "Unknown"
                    finger = pending[apid]["finger"] if apid in pending else "?"

                    await hass.services.async_call(
                        "persistent_notification",
                        "create",
                        {
                            "title": "❌ Fingerprint Enrollment Failed",
                            "message": (
                                f"**Person:** {person_name}\n"
                                f"**Finger:** {finger}\n\n"
                                f"Enrollment confirmation failed.\n\n"
                                f"**Error:** {rpc_error or 'Unknown error'}\n\n"
                                f"Please try enrolling again with better finger placement."
                            ),
                            "notification_id": f"ekey_enrollment_{apid}",
                        },
                    )

                    pending.pop(apid, None)

            except Exception as err:
                # The confirm HTTP call can time out (504 / connection) even though
                # the scanner still finishes enrollment. Don't drop the pending
                # entry or show a failure here — the scanner will emit a terminal
                # SSE state (enstat=40 success, or ≥50 failure) which
                # ``handle_enrollment_complete`` handles, including cleanup.
                _LOGGER.warning(
                    "Confirmation send failed for APID %s (%s) - awaiting terminal SSE state",
                    _fmt_apid(apid), err,
                )

    # Register the single combined enrollment-state listener
    hass.bus.async_listen(EVENT_ENROLLMENT_STATE, handle_enrollment_state)
    
    # Handle enrollment completion events
    async def handle_enrollment_complete(event):
        """Handle enrollment completion from SSE."""
        apid = event.data.get("apid")
        success = event.data.get("success", False)

        # Panel-owned enrollments write to the backend's user document, not to the
        # legacy HA store — see the guard in handle_enrollment_state.
        if apid and apid in hass.data[DOMAIN][entry.entry_id].get("panel_enrollments", {}):
            return


        if success and apid:
            _LOGGER.info("Enrollment complete event received for APID=%s", _fmt_apid(apid))
            
            # Save to permanent storage
            pending = hass.data[DOMAIN][entry.entry_id].get("pending_enrollments", {})
            if apid in pending:
                person_id = pending[apid]["person_id"]
                person_name = pending[apid].get("person_name", person_id)
                finger = pending[apid]["finger"]
                
                # The person-based service path still records into the legacy map,
                # which keeps existing scripts and blueprints working unchanged.
                stored = await person_map.async_load(hass)
                legacy = stored.setdefault("legacy", {})
                if person_id not in legacy:
                    legacy[person_id] = {"fingerprints": {}}
                legacy[person_id].setdefault("fingerprints", {})[str(finger)] = apid
                await person_map.async_save(hass, stored)

                # Then fold it into the backend, which is the source of truth. The
                # reconcile is idempotent and matches by APID first, so forcing it
                # here simply attaches this one fingerprint to the person's user
                # (creating that user if the person has none yet) instead of leaving
                # the two views disagreeing until the next restart.
                try:
                    await person_map.async_reconcile(hass, app_client, force=True)
                except EkeyApiError as reconcile_err:
                    _LOGGER.warning(
                        "Enrolled %s but could not record it on the backend: %s",
                        _fmt_apid(apid), reconcile_err,
                    )

                hass.bus.async_fire("ekey_ha_storage_updated")

                _LOGGER.info("Saved fingerprint mapping: %s finger %d -> %s", person_id, finger, _fmt_apid(apid))
                
                # Show success notification
                await hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "✅ Fingerprint Enrolled Successfully!",
                        "message": (
                            f"**Person:** {person_name}\n"
                            f"**Finger:** {finger}\n\n"
                            f"The fingerprint has been successfully enrolled and saved."
                        ),
                        "notification_id": f"ekey_enrollment_{apid}",
                    },
                )
                
                # Auto-dismiss success notification after 10 seconds
                async def dismiss_notification():
                    await asyncio.sleep(10)
                    await hass.services.async_call(
                        "persistent_notification",
                        "dismiss",
                        {"notification_id": f"ekey_enrollment_{apid}"},
                    )
                hass.async_create_task(dismiss_notification())
                
                # Remove from pending enrollments
                pending.pop(apid, None)
            else:
                _LOGGER.warning("Enrollment completed for APID=%s but not found in pending enrollments", _fmt_apid(apid))
            
            # Wait 3 seconds for scanner to finish processing before querying device info
            await asyncio.sleep(3)
            _LOGGER.debug("Refreshing coordinator data after enrollment completion")
            await coordinator.async_request_refresh()
        else:
            state = event.data.get("state", "unknown")
            _LOGGER.error("Enrollment failed for APID=%s: %s", _fmt_apid(apid), state)
            
            # Remove from pending enrollments on failure
            if apid:
                pending = hass.data[DOMAIN][entry.entry_id].get("pending_enrollments", {})
                if apid in pending:
                    person_id = pending[apid]["person_id"]
                    person_name = pending[apid].get("person_name", person_id)
                    finger = pending[apid]["finger"]
                    _LOGGER.info("Removed failed enrollment from pending: %s finger %d", person_id, finger)
                    
                    # Show error notification
                    await hass.services.async_call(
                        "persistent_notification",
                        "create",
                        {
                            "title": "❌ Fingerprint Enrollment Failed",
                            "message": (
                                f"**Person:** {person_name}\n"
                                f"**Finger:** {finger}\n\n"
                                f"**Reason:** {state}\n\n"
                                f"Please try again."
                            ),
                            "notification_id": f"ekey_enrollment_{apid}",
                        },
                    )
                    
                    pending.pop(apid, None)
    
    hass.bus.async_listen("ekey_enrollment_complete", handle_enrollment_complete)
    
    # Handle green LED on fingerprint match
    async def handle_flash_green_led(event):
        """Turn on green LED when fingerprint matches — only on the scanner that matched."""
        if event.data.get("entry_id") != entry.entry_id:
            return
        async def _set_green_led():
            try:
                # Wait 1 second after match result before sending LED command
                await asyncio.sleep(1)
                _LOGGER.info(">>> Executing setSignalingState(4) - Green LED")
                await coordinator.set_led_state(4)
                _LOGGER.info(">>> Green LED activated successfully")
            except Exception as err:
                _LOGGER.warning(">>> Green LED command failed (daemon busy or timeout): %s", err)
        
        hass.async_create_task(_set_green_led())
    
    hass.bus.async_listen("ekey_flash_green_led", handle_flash_green_led)
    
    # Handle red LED on fingerprint not matched
    async def handle_flash_red_led(event):
        """Turn on red LED when fingerprint doesn't match — only on the scanner that matched."""
        if event.data.get("entry_id") != entry.entry_id:
            return
        async def _set_red_led():
            try:
                # Wait 1 second after match result before sending LED command
                await asyncio.sleep(1)
                _LOGGER.info(">>> Executing setSignalingState(5) - Red LED")
                await coordinator.set_led_state(5)
                _LOGGER.info(">>> Red LED activated successfully")
            except Exception as err:
                _LOGGER.warning(">>> Red LED command failed (daemon busy or timeout): %s", err)
        
        hass.async_create_task(_set_red_led())
    
    hass.bus.async_listen("ekey_flash_red_led", handle_flash_red_led)
    
    # Clear out entities earlier versions created, before the platforms come up.
    _async_remove_retired_entities(hass, entry)

    # ...and then, once the automations are loaded, tell the user if any of them was
    # still using one. Removing the rows silently is what leaves somebody staring at
    # an empty entity picker with no idea why.
    entry.async_on_unload(
        async_at_started(
            hass, lambda _hass: _async_check_retired_references(hass, entry)
        )
    )

    # Forward the setup to platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    # Registered once for the domain, not once per entry — but each handler now
    # resolves the scanner from the EVENT rather than closing over the first entry.
    # That was a real bug: with two scanners, every logbook row was attributed to
    # scanner #1's sensor entity, because these listeners were created inside the
    # "first entry only" block and captured that entry.
    if not hass.data[DOMAIN].get(_SERVICES_REGISTERED):
        await async_setup_services(hass)

        # The panel and its websocket commands. Also once for the domain: there is
        # one sidebar entry and it offers a scanner picker.
        ws_api.async_register(hass)
        await async_register_panel(hass)

        # Handle fingerprint match activity records
        async def handle_fingerprint_matched_activity(event):
            """Resolve person/finger from the backend, then fire the logbook event."""
            apid = event.data.get("apid", "")
            if not apid:
                return

            event_entry_id = event.data.get("entry_id")
            person_name = "Unknown"
            finger = None

            try:
                # The backend's user document is authoritative; person_map falls
                # back to the preserved legacy map when the backend is unreachable,
                # so an outage does not turn every recognition into "Unknown".
                data = await person_map.async_person_map(hass, event_entry_id)

                person_id = None
                for pid, person_data in data.items():
                    for fid, stored_apid in person_data.get("fingerprints", {}).items():
                        if stored_apid == apid:
                            person_id = pid
                            finger = int(fid)
                            break
                    if person_id:
                        break

                if person_id:
                    person_state = hass.states.get(person_id)
                    if person_state:
                        person_name = person_state.attributes.get("friendly_name", person_id)
            except Exception as lookup_err:
                _LOGGER.warning(
                    "Could not resolve person for APID %s: %s", _fmt_apid(apid), lookup_err
                )

            _LOGGER.info(
                "✓ Fingerprint Access GRANTED: Person=%s, Finger=%s, APID=%s",
                person_name,
                finger if finger else "unknown",
                _fmt_apid(apid),
            )

            # Look up the scanner sensor entity_id so the logbook entry is tied to
            # the device and appears in the device activity view. Keyed on the
            # entry the event came FROM, so a second scanner is not attributed to
            # the first.
            entity_reg = er.async_get(hass)
            scanner_entity_id = entity_reg.async_get_entity_id(
                "sensor", DOMAIN, f"{event_entry_id or entry.entry_id}_device_info"
            )

            hass.bus.async_fire(EVENT_ACCESS_GRANTED, {
                "person_name": person_name,
                "finger": finger,
                "entity_id": scanner_entity_id,
                "entry_id": event_entry_id,
            })

        hass.bus.async_listen(EVENT_FINGERPRINT_MATCHED, handle_fingerprint_matched_activity)

        # Handle fingerprint not matched activity records
        async def handle_fingerprint_not_matched_activity(event):
            """Fire EVENT_ACCESS_DENIED with entity_id so it appears in the device activity view."""
            apfar_desc = event.data.get("apfar_desc", "Unknown")
            event_entry_id = event.data.get("entry_id")
            _LOGGER.warning(
                "✗ Fingerprint Access DENIED: Reason=%s, Scanner=%s",
                apfar_desc, event.data.get("scanner_id", f"{host}:{port}"),
            )

            entity_reg = er.async_get(hass)
            scanner_entity_id = entity_reg.async_get_entity_id(
                "sensor", DOMAIN, f"{event_entry_id or entry.entry_id}_device_info"
            )

            hass.bus.async_fire(EVENT_ACCESS_DENIED, {
                "apfar_desc": apfar_desc,
                "entity_id": scanner_entity_id,
                "entry_id": event_entry_id,
            })

        hass.bus.async_listen(EVENT_FINGERPRINT_NOT_MATCHED, handle_fingerprint_not_matched_activity)
        
        # Show setup complete notification only on first installation
        _setup_store = Store(hass, 1, f"{DOMAIN}.setup_notified")
        _setup_data = await _setup_store.async_load()

        if not _setup_data or not _setup_data.get("shown"):
            blueprint_message = (
                "**ekey Integration Setup Complete!** 🎉\n\n"
                "Open **ekey** in the sidebar to manage users and fingerprints: "
                "add a user, enrol a finger with live progress, and assign a "
                "fingerprint that was enrolled on the device itself.\n\n"
                "Actions, automations and the access log live on the scanner or "
                "daemon, not here — they keep working when Home Assistant is "
                "down. Reach them from the panel or from the device's own "
                "**Admin** page.\n\n"
                "**Optional blueprints** (Home Assistant side, for things the "
                "backend cannot do):\n"
                "- *Relay Pulse on Access Granted* — pulse an HA switch\n"
                "- *Welcome Home Notification* — notify a phone\n\n"
                "Install them with `./install_blueprints.sh` "
                "(`.\\install_blueprints.ps1` on Windows), or import the YAML "
                "from `custom_components/ekey_ha_app/blueprints/` under "
                "**Settings** → **Automations & scenes** → **Blueprints**.\n\n"
                "📖 See **QUICKSTART.md** for the step-by-step setup guide."
            )

            await hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "🔐 ekey Integration Setup",
                    "message": blueprint_message,
                    "notification_id": "ekey_setup_complete",
                },
            )

            await _setup_store.async_save({"shown": True})

        hass.data[DOMAIN][_SERVICES_REGISTERED] = True

    # A startup scan for fingerprints that belong to nobody used to live here. It
    # was already dead code — the line that scheduled it had been commented out —
    # and its only output was a persistent notification containing curl commands.
    # The panel lists unassigned fingerprints continuously and can assign one to a
    # user, which is the same information plus a way to act on it.

    _LOGGER.info("ekey HA App integration setup complete for %s:%s", host, port)
    
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    bucket = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if bucket:
        # Stop SSE listener
        await bucket["sse_listener"].stop()
        # Stop listening for enrollment states and drop any watchdog.
        enroll_manager = bucket.get("enroll_manager")
        if enroll_manager is not None:
            enroll_manager.async_detach()

    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    # Services and the panel belong to the domain, not to one entry — tear them
    # down only when the last SCANNER is gone. Counting entry buckets rather than
    # `hass.data[DOMAIN]` matters now that the shared store and the registration
    # flags live in the same dict.
    if not _entry_buckets(hass):
        await async_unload_services(hass)
        async_remove_panel(hass)
        hass.data[DOMAIN][_SERVICES_REGISTERED] = False

    return unload_ok
