# ekey Home Assistant App

Home Assistant custom integration for the ekey module fingerprint scanner (OEM version).

Users and fingerprints live on the **backend** — the scanner itself on an ESP32
install, or the daemon on a Linux install. This integration is the Home Assistant
**front end** for them: a sidebar panel, a few entities, three services and the
events your automations trigger on.

## Features

- **ekey panel in the sidebar** — add users, enrol a finger with live progress,
  assign a fingerprint that was enrolled on the device, delete one
- **Real-time events** — scanner events delivered over Server-Sent Events (SSE)
- **Logbook activity** — every access grant and denial recorded automatically
- **LED control** — make the scanner signal from a Home Assistant automation
- **Device sensors** — scanner status, enrolled count and last access

## Installation

1. Install through HACS, or copy the `ekey_ha_app` folder to `custom_components/` in
   your HA config directory
2. Restart Home Assistant — a custom integration is only discovered at startup
3. Go to **Settings** → **Devices & Services** → **Add Integration**
4. Search for **ekey module App**
5. Choose how Home Assistant reaches the backend:

| Choice | Use it for | Fields |
| --- | --- | --- |
| **Local — ekey-ha-daemon on this host (HTTP)** | the add-on, or a daemon on the same machine | Host (default `127.0.0.1`), Port (default `8080`), API token (optional) |
| **Remote device — ekey ESP32 (HTTPS + token)** | a scanner reached over the network | Host / IP, Port, API token (required), Verify SSL certificate (leave **off** for the device's self-signed certificate) |

The connection is validated before the entry is saved.

A token is optional for a local daemon's `/api/v1` but **required for the panel** —
without one the user list cannot be read. On a daemon install it is in
`/etc/ekey/app/token`; on an ESP32 install the device's Admin page shows it.

If the backend later rejects the stored token — it was regenerated, or the backend was
factory reset — the integration raises a **reauth** notice titled *"ekey token no
longer accepted"*. Entering the current token there is the whole fix; the entry is
otherwise untouched.

## Device options (ESP32 only)

**Configure** on the integration entry offers two things, both talking to the device's
`/config` API:

| Option | What it does |
| --- | --- |
| **Push Wi-Fi credentials to the device** | Sets SSID, password, mDNS host name and HTTPS port, optionally rebooting to apply. Current values are pre-filled from the device; a blank password keeps the stored one. A wrong password is verified on the device and rolled back automatically. Changing the network or the port reboots the device and may change its address |
| **Reset the device's Wi-Fi (return to setup mode)** | Clears the stored credentials and reboots into the setup portal for re-provisioning. The scanner pairing and API token are kept |

A **local daemon** entry aborts with *"Wi-Fi configuration is only available for remote
ekey devices"* — the daemon has no `/config` API.

## The ekey panel

**ekey** appears in the sidebar after setup (admin users only). It manages what
used to need a dashboard full of helper entities:

| Task | Where |
| --- | --- |
| List users and their fingers | panel → user list |
| Add a user | panel → **Add user** |
| Enrol a finger | panel → **Enroll fingerprint** (modal, live progress) |
| Assign a fingerprint enrolled on the device | panel → **Unassigned fingerprints** |
| Delete a fingerprint | panel → user list → **Delete** |
| Change which serial port the scanner is on | panel → **Scanner connection** — *standalone daemon only, see below* |
| Actions, automations, access log, MQTT, KNX | the backend's own **Admin** page |

The backend token never reaches the browser: the panel talks to Home Assistant
over the websocket connection it already has, and the integration holds the token.

Rules that must survive a Home Assistant restart — LED feedback, KNX, MQTT,
webhooks — belong on the backend, not here. That is why they are configured on the
Admin page rather than in this integration.

### Scanner connection

The panel shows which serial port the scanner is on, and lets you change it — but only
where that is genuinely this page's to change. The section appears at all only when the
backend says the port is a setting, and the control inside it only when the backend says it
is editable:

| Backend | What you see |
| --- | --- |
| **Standalone `ekey-ha-daemon`** | The port, a list of every serial port the machine has, and a Save button. A change takes effect within 30 seconds if no scanner is connected yet; otherwise it needs a daemon restart, and the panel says which |
| **`ekey-ha-addon`** | The port, read-only. It is an add-on configuration setting — change it in the Supervisor's Configuration tab and restart the add-on |
| **ESP32 device** | Nothing. The sensor is on fixed UART pins; there is no port to choose |

Ports flagged **system console** can be chosen but ask for confirmation first: opening one
can switch an on-board UART into RS485 mode, which is a lasting change to a port something
else is using.

## Entities

All entities appear under the **ekey Scanner** device. Actual entity ID prefixes
depend on the instance name chosen during setup. Find your exact IDs via
**Settings** → **Devices & Services** → **Devices** → **ekey Scanner**.

| Entity | Type | Description |
| --- | --- | --- |
| `sensor.ekey_scanner_info` | Sensor | Serial number, API and software version |
| `sensor.ekey_enrolled_fingerprints` | Sensor | Count of fingerprints on the scanner |
| `sensor.ekey_last_access` | Sensor | Most recent access result, e.g. `Granted: Jane (finger 3)` |
| `button.ekey_led_green` | Button | Turn the scanner LED green |
| `button.ekey_led_red` | Button | Turn the scanner LED red |

The two LED buttons are here because nothing else can reach the scanner's LED from
Home Assistant. Note that the integration **already** flashes green on a match and
red on a mismatch by itself; if you also configure an `led` action on the backend
for the same trigger, the scanner receives two commands.

### `sensor.ekey_last_access`

A string, not structured data — it exists so the access history shows up in the
Logbook and the device Activity tab. **Automations should trigger on the
`ekey_access_granted` / `ekey_access_denied` events instead**, which carry the
resolved name and the scanner as fields and need no string parsing.

## Events

| Event | Key data fields | Description |
| --- | --- | --- |
| `ekey_finger_touch` | `entry_id` | Finger placed on the scanner |
| `ekey_fingerprint_matched` | `apid`, `apfar`, `apfar_desc`, `entry_id`, `scanner_id` | Fingerprint matched |
| `ekey_fingerprint_not_matched` | `apfar`, `apfar_desc`, `entry_id`, `scanner_id` | Fingerprint not matched |
| `ekey_access_granted` | `person_name`, `finger`, `entity_id`, `entry_id` | Match, with the user resolved (`Unknown` when the fingerprint belongs to nobody) |
| `ekey_access_denied` | `apfar_desc`, `entity_id`, `entry_id` | Refusal, with the reason |
| `ekey_enrollment_state` | `apid`, `enstat`, `entryc`, `ennumtpl` | Raw enrolment progress from the scanner |
| `ekey_users_changed` | `entry_id`, `reason` | The user document changed |
| `ekey_connection_lost` | — | Connection to the backend lost |

`entry_id` identifies which scanner an event came from — check it in any
automation on a multi-scanner install. Match and no-match events are recorded in
the HA **Logbook** automatically.

A single touch fires several of these in order — `ekey_finger_touch`, then
`ekey_fingerprint_matched` / `_not_matched`, then the resolved `ekey_access_granted` /
`ekey_access_denied`. Triggering the same reaction on more than one of them runs it
twice.

Three further events are fired around enrolment and deletion. They exist mainly for
the panel, but nothing stops an automation using them:

| Event | Key data fields | Description |
| --- | --- | --- |
| `ekey_enrollment_started` | `person_id`, `person_name`, `finger`, `apid`, `status` | The `enroll_fingerprint` service accepted a request |
| `ekey_enrollment_complete` | `apid`, `success` — plus `entryc`/`ennumtpl` when it succeeded, `state`/`enstat` when it failed | Enrolment ended. Trigger on this rather than filtering `ekey_enrollment_state` by code |
| `ekey_fingerprint_deleted` | `person_id`, `finger`, `apid` | The `delete_fingerprint` service removed one |

`ekey_ha_storage_updated`, `ekey_flash_green_led` and `ekey_flash_red_led` are also on
the bus, but they are internal plumbing between this integration's own modules — treat
them as private and do not build on them.

## Services

### `ekey_ha_app.enroll_fingerprint`

```yaml
service: ekey_ha_app.enroll_fingerprint
data:
  person_id: person.john_doe
  finger: 1  # 1–10
```

Starts enrolment and reports progress through notifications. The panel's Enroll
dialog does the same thing with live progress and is the easier route; this service
remains for scripted use.

### `ekey_ha_app.delete_fingerprint`

```yaml
service: ekey_ha_app.delete_fingerprint
data:
  person_id: person.john_doe
  finger: 1
```

### `ekey_ha_app.set_led_brightness`

```yaml
service: ekey_ha_app.set_led_brightness
data:
  brightness: 50  # 0–100
```

## Blueprints

Three automation blueprints, in the `blueprints/` subdirectory. All three do something
the backend cannot: operate a Home Assistant entity.

| Blueprint | Description |
| --- | --- |
| `toggle_relay_on_granted.yaml` | Pulse an HA switch or relay when a known user is granted access |
| `welcome_notification.yaml` | Push a notification naming the person, to a phone or any `notify.*` service |
| `access_notification_list.yaml` | Add an entry to Home Assistant's own notification list (the bell), optionally including refusals. Note that list is cleared on restart — the logbook and the backend's own access log are the durable records |

Home Assistant does not load blueprints from `custom_components/`, so each has to be
copied into `config/blueprints/automation/ekey/` once. The reliable route on any
install is **Settings** → **Automations & scenes** → **Blueprints** → **Import
Blueprint** → paste the YAML. From a clone of the repository,
`scripts/install_blueprints.sh` (`.ps1` on Windows) does all three at once — that
script is not part of a HACS install. See
[`blueprints/README.md`](blueprints/README.md).

## Removed in the app-layer version

These existed to build a user interface out of entities, before there was one.
Their registry entries are deleted automatically on upgrade, so no "unavailable"
entities are left behind.

If an **automation or script of yours still refers to one**, deleting the row does not
delete the reference: the integration raises a repair notice at startup naming both the
entity and the automation, because the alternative symptom is an entity picker that
shows *"Unknown entity selected"* over an empty dropdown with nothing explaining why.
The usual cause is an automation built from the old relay-pulse blueprint — see
[blueprints/README.md](blueprints/README.md), and note that updating the integration
does **not** update a blueprint you already imported.

| Removed | Use instead |
| --- | --- |
| `select.ekey_person_selector`, `select.ekey_finger_selector` | the panel's Enroll dialog |
| `select.ekey_enrolled_fingerprints` | the panel's user list; `sensor.ekey_enrolled_fingerprints` for a count |
| `button.ekey_check_orphaned_fingerprints` | the panel's **Unassigned fingerprints** list, which can also assign them |
| `button.ekey_person_fingerprints` | the panel's user list |
| `enroll_fingerprint.yaml`, `delete_fingerprint.yaml` script blueprints | the panel, or the services above |
| `flash_led_on_match.yaml`, `flash_led_on_fail.yaml` | already built in (see the LED note above), or an `led` action on the backend |
| `lovelace_activity_panel.yaml` | the sidebar panel |

`toggle_relay_on_granted.yaml` was rewritten to trigger on `ekey_access_granted`
instead of parsing the last-access sensor's text. **Re-import it** if you were
using the old version.

## Requirements

- Home Assistant 2024.7 or newer (`async_register_static_paths`, used by the panel)
- A backend reachable over HTTP: the add-on, the standalone daemon, or an ESP32
  running the ekey firmware
- An ekey module RS485 fingerprint scanner (OEM version)

## Architecture

```text
┌─────────────────┐         ┌──────────────────┐         ┌─────────────────┐
│ Home Assistant  │  HTTP   │  ekey backend    │ RS485   │ ekey Scanner    │
│ panel · entities│◄───────►│  daemon or ESP32 ├────────►│   (Hardware)    │
│ services · events│ :8080  │  /app/v1 + SSE   │ Serial  │                 │
└─────────────────┘         └──────────────────┘         └─────────────────┘
```

Users, actions, automations and the access log are stored and executed on the
backend, so they keep working while Home Assistant is down.

## Further documentation

- [`docs/QUICKSTART.md`](docs/QUICKSTART.md) — step-by-step setup, from adding the
  integration to enrolling the first finger
- [`docs/AUTOMATION_EXAMPLES.md`](docs/AUTOMATION_EXAMPLES.md) — copy-paste
  automations built on the events above
- [`blueprints/README.md`](blueprints/README.md) — what each blueprint does, its
  inputs, and how to import it

## Support

For issues and feature requests, please visit:
<https://github.com/agrisvi/ekey_module_app/issues>

## License

MIT License
