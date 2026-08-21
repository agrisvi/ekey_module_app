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

1. Copy the `ekey_ha_app` folder to `custom_components/` in your HA config directory
2. Restart Home Assistant
3. Go to **Settings** → **Devices & Services** → **Add Integration**
4. Search for **ekey Home Assistant App**
5. Enter the daemon host and port (default: `localhost`, `8080`)

A token is required for the panel. On a daemon install it is in
`/etc/ekey/app/token`; on an ESP32 install the device's Admin page shows it.

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

Two automation blueprints, in the `blueprints/` subdirectory. Both do something
the backend cannot: operate a Home Assistant entity.

| Blueprint | Description |
| --- | --- |
| `toggle_relay_on_granted.yaml` | Pulse an HA switch or relay when a known user is granted access |
| `welcome_notification.yaml` | Push a notification naming the person, to a phone or any `notify.*` service |
| `access_notification_list.yaml` | Add an entry to Home Assistant's own notification list (the bell), optionally including refusals. Note that list is cleared on restart — the logbook and the panel's Event log are the durable records |

Install with `./install_blueprints.sh` (`.\install_blueprints.ps1` on Windows), or
import the YAML under **Settings** → **Automations & scenes** → **Blueprints**.
See `blueprints/README.md`.

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

## Support

For issues and feature requests, please visit:
<https://github.com/agrisvi/ekey_module_app/issues>

## License

MIT License
