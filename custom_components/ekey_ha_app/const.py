"""Constants for the ekey Home Assistant App integration."""
from homeassistant.const import (  # noqa: F401 — re-exported for convenience
    CONF_HOST,
    CONF_PORT,
    CONF_SSL,
    CONF_TOKEN,
    CONF_VERIFY_SSL,
)

DOMAIN = "ekey_ha_app"

# Configuration — use HA's standard CONF_HOST / CONF_PORT keys so the config
# entry data is compatible with HA's built-in helpers (e.g. network helpers).
CONF_DAEMON_HOST = CONF_HOST   # "host"
CONF_DAEMON_PORT = CONF_PORT   # "port"
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 8080
# Self-signed cert on the ESP32 → certificate verification is off by default.
DEFAULT_VERIFY_SSL = False

# Connection modes (config-flow menu options).
MODE_LOCAL = "local"     # ekey-ha-daemon over http:// (+ optional token)
MODE_REMOTE = "remote"   # ESP32 device over https:// (+ required token)

# API Endpoints
API_BASE = "/api/v1"
API_HEALTH = f"{API_BASE}/health"
API_DEVICE = f"{API_BASE}/device"
API_FINGERPRINTS = f"{API_BASE}/fingerprints"
API_FINGERPRINTS_ENROLL = f"{API_BASE}/fingerprints/enroll"
API_FINGERPRINTS_ENROLL_STATE = f"{API_BASE}/fingerprints/enroll/state"
API_FINGERPRINTS_ENROLL_CONFIRM = f"{API_BASE}/fingerprints/enroll/confirm"
API_FINGERPRINTS_ENROLL_QUIT = f"{API_BASE}/fingerprints/enroll/quit"
# Template transfer — how a fingerprint is copied between scanners at all. The
# read is per-APID; the write is NOT, because the APID travels inside the blob's
# own plaintext header and the scanner reads it there (see templates.py). Both
# answer HTTP 200 on a scanner-level refusal, so neither may be judged by status.
API_FINGERPRINT_TEMPLATE = f"{API_BASE}/fingerprints/{{apid}}/template"
API_FINGERPRINTS_TEMPLATE = f"{API_BASE}/fingerprints/template"
API_LED = f"{API_BASE}/led"
API_LED_BRIGHTNESS = f"{API_BASE}/led/brightness"
API_EVENTS = f"{API_BASE}/events"

# --- App layer (/app/v1) -----------------------------------------------------
# The backend owns the app model: users and their finger slots, actions,
# automations, the event log and the MQTT/KNX settings. The ESP32 has served
# these since its app layer landed; the Linux daemon gains them separately.
# This integration is a FRONT-END for them and never a second source of truth.
APP_BASE = "/app/v1"
API_APP_CAPABILITIES = f"{APP_BASE}/capabilities"
API_APP_USERS = f"{APP_BASE}/users"
API_APP_ACTIONS = f"{APP_BASE}/actions"
API_APP_LINKS = f"{APP_BASE}/links"
API_APP_MQTT = f"{APP_BASE}/mqtt"
API_APP_KNX = f"{APP_BASE}/knx"
API_APP_EVENTS = f"{APP_BASE}/events"
API_APP_SERIAL = f"{APP_BASE}/serial"

# ESP32 device-management endpoints (used by the options flow to push Wi-Fi
# credentials / reset the device). Not served by the Linux daemon.
API_CONFIG = "/config"                         # GET current / POST new settings
API_REBOOT = "/reboot"                         # apply staged changes
API_WIFI_RESET = f"{API_DEVICE}/wifi-reset"    # forget Wi-Fi → reboot into setup

# Event types
EVENT_FINGER_TOUCH = "ekey_finger_touch"
EVENT_FINGERPRINT_MATCHED = "ekey_fingerprint_matched"
EVENT_FINGERPRINT_NOT_MATCHED = "ekey_fingerprint_not_matched"
EVENT_ENROLLMENT_STATE = "ekey_enrollment_state"
EVENT_ACCESS_GRANTED = "ekey_access_granted"
EVENT_ACCESS_DENIED = "ekey_access_denied"

# Events that were fired as bare strings before but are part of the contract the
# panel subscribes to, so they belong here rather than scattered through the code.
EVENT_ENROLLMENT_STARTED = "ekey_enrollment_started"
EVENT_ENROLLMENT_COMPLETE = "ekey_enrollment_complete"
EVENT_FINGERPRINT_DELETED = "ekey_fingerprint_deleted"
EVENT_CONNECTION_LOST = "ekey_connection_lost"
EVENT_STORAGE_UPDATED = "ekey_ha_storage_updated"
# The fingerprint database (see vault.py). Deliberately NOT reusing
# EVENT_STORAGE_UPDATED above: that one belongs to the person-link store and
# carries no data, and sharing it would make each store's consumers refresh for
# the other's reasons.
EVENT_VAULT_JOB = "ekey_storage_job"
EVENT_VAULT_CHANGED = "ekey_storage_changed"
# Fired whenever the backend's user document changes, so every open panel and the
# enrolled-fingerprint selector refresh without polling.
EVENT_USERS_CHANGED = "ekey_users_changed"

# --- Panel -------------------------------------------------------------------
# One sidebar entry for the whole integration (not one per scanner): the panel
# lets you pick the scanner, mirroring how the services take a `scanner` field.
PANEL_URL_PATH = "ekey"
PANEL_TITLE = "ekey"
PANEL_ICON = "mdi:fingerprint"
PANEL_COMPONENT_NAME = "ekey-panel"
PANEL_JS_URL = "/ekey_ha_app_static/ekey-panel.js"
PANEL_STATIC_PATH = "/ekey_ha_app_static"

# Enrollment states
ENROLL_STATE_WAIT = 10
ENROLL_STATE_ACQUIRE = 20
ENROLL_STATE_STEP_DONE = 30
ENROLL_STATE_WAIT_FOR_CONFIRMATION = 35
ENROLL_STATE_FINISHED_SUCCESS = 40
ENROLL_STATE_FINISHED_QUITBYUSER = 50
ENROLL_STATE_FINISHED_TIMEOUT = 60
ENROLL_STATE_FINISHED_DUPLICATE = 70

ENROLLMENT_STATES = {
    10: "wait",
    20: "acquire",
    30: "step_done",
    35: "wait_for_confirmation",
    40: "finished_success",
    50: "finished_quitbyuser",
    60: "finished_timeout",
    70: "finished_duplicate",
}

# Match results
MATCH_OK = 0
MATCH_NOT_OK = 10
MATCH_FAR = 20
MATCH_NO_MATCH_BAD_QUALITY = 30

MATCH_RESULTS = {
    0: "Match OK",
    10: "Match not OK",
    20: "FAR Match",
    30: "No Match, bad quality",
}

# Storage keys
#
# v2 moved authority for users to the backend. The store no longer holds the
# person→APID map as truth: the v1 map is preserved verbatim under a "legacy" key
# (and never deleted, so a bad reconcile is always recoverable by hand), plus a
# per-scanner record of whether it has been folded into the backend yet.
#
# Everything must go through person_map.async_get_store(); constructing a bare
# Store(hass, STORAGE_VERSION, STORAGE_KEY) here would hit the migration hook that
# only the EkeyPersonStore subclass implements.
STORAGE_KEY = f"{DOMAIN}.person_fingerprints"
STORAGE_VERSION = 2

# Person fingerprint data structure
# {
#   "person_id": {
#     "fingerprints": {
#       "1": "uuid-apid-1",  # finger 1
#       "2": "uuid-apid-2",  # finger 2
#       ...
#     }
#   }
# }

# --- The fingerprint database ------------------------------------------------
#
# A SECOND store, not another key inside the person store, because the two have
# nothing in common but a domain: the person map is a few hundred bytes and is
# read on every recognition, while this one holds ~14.6 kB of template hex per
# finger and is touched only when a fingerprint is adopted, restored or enrolled.
# Everything must go through vault.async_get_store() — see the note above.
VAULT_STORAGE_KEY = f"{DOMAIN}.fingerprint_vault"
VAULT_STORAGE_VERSION = 1

# The backend's APP_HTTP_BODY_MAX. It caps the WHOLE users.json document on a
# PUT, which is reached at roughly 30 users x 10 fingers — so a fan-out that adds
# a finger to every scanner has to measure the document before sending it rather
# than discover the limit as a rejected write.
APP_HTTP_BODY_MAX = 24576

# Refused before a single byte of an uploaded backup is read. A real backup of a
# hundred fingerprints is ~1.5 MB; anything past this is not one.
MAX_RESTORE_BYTES = 32 * 1024 * 1024

# Chunk size for backup download and restore upload, in raw bytes before base64.
# 256 KiB becomes ~350 kB of base64 per websocket message, well inside the 4 MiB
# inbound limit aiohttp applies to what the browser sends us.
VAULT_CHUNK_BYTES = 256 * 1024
