# ekey module App

A **Home Assistant custom component** for the **ekey module fingerprint scanner**
(OEM version): a sidebar panel for managing users and their fingerprints, plus
entities, services and events.

---

## Architecture

```text
ekey module fingerprint scanner
      │  RS485 serial
      ▼
the backend — an add-on, a standalone daemon, or the ESP32 firmware
      │  HTTP REST + SSE  (default localhost:8080)
      ▼
ekey module App  (this repo — HA custom component)
      │
      ▼
ekey panel (sidebar) · 5 entities · 3 services · events · 3 blueprints
```

Users, actions, automations and the access log live and run on the **backend**,
not here. This repo is the Home Assistant front end for them, which is why it has
few entities and no automation logic of its own — and why a recognised finger
still fires its actions while Home Assistant is restarting, updating or down.

---

## Requirements

- **A backend, running and reachable** from the Home Assistant host (default
  `localhost:8080`). Any one of:
  - the **ekey module Daemon add-on** — the easiest route on Home Assistant OS;
  - the **standalone daemon** on a Linux host;
  - an **ESP32** running the ekey firmware, over the network.
- Home Assistant **2024.7** or newer. That floor is not arbitrary: the sidebar
  panel registers its JavaScript through `async_register_static_paths()`, which
  does not exist before it.
- Python — whatever your Home Assistant already runs on. Nothing extra.

---

## Installation

### Option 1: HACS (recommended)

1. Open HACS → ⋮ → **Custom repositories**
2. Add `https://github.com/agrisvi/ekey_module_app` with category
   **Integration**
3. Find **ekey module App** in the list and **Download**
4. **Restart** Home Assistant

### Option 2: Manual

1. Copy the `custom_components/ekey_ha_app/` folder into your Home Assistant
   `config/custom_components/` directory
2. **Restart** Home Assistant — a custom integration is only discovered at
   startup, so a reload will not find it

---

## Configuration

Go to **Settings** → **Devices & Services** → **Add Integration** and search for
**ekey module App**. The first screen asks how Home Assistant reaches the backend,
and that choice decides what it asks next:

| Choice | Use it for | Fields |
| --- | --- | --- |
| **Local — ekey-ha-daemon on this host (HTTP)** | the add-on, or a daemon on the same machine | Host (default `127.0.0.1`), Port (default `8080`), API token (optional) |
| **Remote device — ekey ESP32 (HTTPS + token)** | a scanner reached over the network | Host / IP, Port, API token (required), Verify SSL certificate — leave **off** for the device's self-signed certificate |

**Submit** validates the connection before the entry is saved, so a wrong host or a
rejected token fails here rather than silently later.

Supply the **API token** even on a local install unless you have no use for the panel.
It is optional for a local daemon's `/api/v1`, but required for user management: see
the panel below.

If the backend later rejects the stored token, the integration asks for a new one
through a reauth notice rather than failing silently.

An ESP32 entry also gets a **Configure** button, which can push new Wi-Fi credentials
to the device or reset it back into its setup portal. A local daemon entry has nothing
to configure there and says so.

---

## The ekey panel — users and fingerprints

A sidebar entry named **ekey** (admin only) lists the users the *backend* holds, adds
them, enrols a finger with live progress from the sensor, finds fingerprints the
sensor holds that belong to nobody, and deletes with the sensor's confirmation.

**The backend owns this, not Home Assistant.** User records and which finger slot owns
which fingerprint live on the device or daemon, so a recognition still opens the door
when Home Assistant is stopped. This integration is the front-end.

The panel talks only to Home Assistant, over the websocket connection it already has;
the integration holds the backend token and makes the calls. The token is never sent
to the browser and neither backend needs CORS.

### What it needs from the backend

The panel requires a backend that serves `/app/v1` — any ekey ESP32 device, or an
`ekey-ha-daemon` new enough to have the app layer. Against anything else the panel
says so plainly and the rest of the integration is unaffected. It asks
`GET /app/v1/capabilities` and hides what the backend reports it cannot do, so a
Linux daemon does not offer a GPIO action it could never run.

### Existing person → fingerprint mappings

Earlier versions kept the person↔fingerprint map in Home Assistant. On upgrade it is
folded into the backend once per scanner: matched **by APID first**, then by name. An
app user is linked to a `person` entity through a `ha_person` field on the user record
itself, so the link survives a Home Assistant reinstall.

The old v1 map is copied verbatim under a `legacy` key in
`.storage/ekey_ha_app.person_fingerprints` and **never deleted**, so a bad reconcile is
always recoverable by hand. Anything ambiguous — two candidates for one person, a
finger slot already occupied — is never guessed: it is left alone and reported as a
repair issue.

---

## Entities

All entities are created automatically under the **ekey Scanner** device.
Actual entity ID prefixes depend on the instance name chosen during setup
(e.g. `door_ekey_` instead of `ekey_`). Verify your exact IDs via
**Settings** → **Devices & Services** → **Devices** → **ekey Scanner**.

| Entity | Type | Description |
| --- | --- | --- |
| `sensor.ekey_scanner_info` | Sensor | Serial number, API and software version |
| `sensor.ekey_enrolled_fingerprints` | Sensor | Count of fingerprints enrolled on the scanner |
| `sensor.ekey_last_access` | Sensor | Most recent access result, for the Logbook and Activity tab |
| `button.ekey_led_green` | Button | Turn scanner LED green |
| `button.ekey_led_red` | Button | Turn scanner LED red |

Five entities, not fifteen. Managing users and fingerprints is the panel's job; the
three selects and two notification buttons earlier versions created for that purpose
are gone, and their registry rows are deleted automatically on upgrade so nothing is
left showing as "unavailable". See
[the component README](custom_components/ekey_ha_app/README.md#removed-in-the-app-layer-version)
for the full list and what replaces each one.

The LED buttons stay because nothing else in Home Assistant can reach the scanner's
LED. Note the integration already flashes green on a match and red on a mismatch by
itself — if you also add an `led` action on the backend for the same trigger, the
scanner gets two commands.

---

## Services

| Service | Parameters | Description |
| --- | --- | --- |
| `ekey_ha_app.enroll_fingerprint` | `person_id` (entity_id), `finger` (1–10) | Start fingerprint enrollment |
| `ekey_ha_app.delete_fingerprint` | `person_id` (entity_id), `finger` (1–10) | Delete an enrolled fingerprint |
| `ekey_ha_app.set_led_brightness` | `brightness` (0–100) | Set scanner LED brightness |

---

## Events

| Event | Key data fields | Description |
| --- | --- | --- |
| `ekey_finger_touch` | `entry_id` | Finger placed on scanner |
| `ekey_fingerprint_matched` | `apid`, `apfar`, `apfar_desc`, `entry_id`, `scanner_id` | Fingerprint matched successfully |
| `ekey_fingerprint_not_matched` | `apfar`, `apfar_desc`, `entry_id`, `scanner_id` | Fingerprint did not match |
| `ekey_access_granted` | `person_name`, `finger`, `entity_id`, `entry_id` | Match with the user resolved — **the one to build automations on** |
| `ekey_access_denied` | `apfar_desc`, `entity_id`, `entry_id` | Refusal, with the reason |
| `ekey_enrollment_state` | `apid`, `enstat`, `entryc`, `ennumtpl` | Enrollment progress update |
| `ekey_users_changed` | `entry_id`, `reason` | The user document changed |
| `ekey_connection_lost` | — | Connection to daemon lost |

`entry_id` says which scanner an event came from — check it in any automation on a
multi-scanner install. Match and no-match events are automatically recorded in the
HA **Logbook** as `ekey Access — Access GRANTED: <name> (finger <n>)` and
`ekey Access — Access DENIED: <reason>`.

Prefer `ekey_access_granted` over watching `sensor.ekey_last_access`: the event
carries the resolved name as a field, where the sensor only has it inside a string.
A single touch fires several of these in order — touch, then matched/not-matched, then
the resolved granted/denied — so triggering one reaction on more than one of them runs
it twice.

Three further events fire around enrolment and deletion — `ekey_enrollment_started`,
`ekey_enrollment_complete` and `ekey_fingerprint_deleted`. See
[the component README](custom_components/ekey_ha_app/README.md#events) for their
fields.

---

## Blueprints

Three automation blueprints, in
[`custom_components/ekey_ha_app/blueprints/`](custom_components/ekey_ha_app/blueprints/):

| Blueprint | Description |
| --- | --- |
| `toggle_relay_on_granted.yaml` | Pulse an HA switch or relay when a known user is granted access |
| `welcome_notification.yaml` | Push a notification naming the person, to any `notify.*` service |
| `access_notification_list.yaml` | Add an entry to Home Assistant's own notification list (the bell), optionally including refusals |

All three operate a **Home Assistant** entity, which is the one thing the backend
cannot do. Everything that only involves the scanner — LED, KNX, MQTT, a webhook — should
be an action on the backend instead, configured in the **Actions** and
**Automations** tabs of its Admin page. Those rules run on the scanner or daemon, so
they keep firing while Home Assistant is restarting, updating or down.

The enrol and delete *script* blueprints are gone: that is what the panel does now.

Home Assistant does not load blueprints from `custom_components/`, so each has to be
copied into `config/blueprints/automation/ekey/` once — paste the YAML under
**Settings** → **Automations & scenes** → **Blueprints** → **Import Blueprint**. From
a clone of this repository, [`scripts/install_blueprints.sh`](scripts/install_blueprints.sh)
(or `.ps1` on Windows) does all three at once; that script sits outside
`custom_components/`, so it is not part of a HACS install. See
[`custom_components/ekey_ha_app/blueprints/README.md`](custom_components/ekey_ha_app/blueprints/README.md)
and [`scripts/INSTALL_BLUEPRINTS.md`](scripts/INSTALL_BLUEPRINTS.md).

---

## Quick start

See [`custom_components/ekey_ha_app/docs/QUICKSTART.md`](custom_components/ekey_ha_app/docs/QUICKSTART.md)
for a step-by-step setup guide.

---

## Automation Examples

See [`custom_components/ekey_ha_app/docs/AUTOMATION_EXAMPLES.md`](custom_components/ekey_ha_app/docs/AUTOMATION_EXAMPLES.md)
for a comprehensive set of copy-paste automation examples covering:

- Fingerprint match / no-match handling
- LED control
- Door unlock
- Enrollment workflow
- Person-based welcome messages
- Connection monitoring

---

## Development

### Repository layout

```text
custom_components/ekey_ha_app/   ← everything HACS installs, and nothing else
├── blueprints/                  three automation blueprints + their README
├── docs/                        QUICKSTART.md, AUTOMATION_EXAMPLES.md
├── translations/, www/          strings and the sidebar panel's JS
└── *.py                         the integration
scripts/                         install_blueprints.sh / .ps1 — clone-only tooling
tests/                           pytest suite + the Node panel render test
```

The split matters: HACS downloads the contents of `custom_components/ekey_ha_app/`
only, so anything a user needs at runtime has to live inside it, and anything that is
maintainer tooling should not. That is why the blueprints ship with the integration
but the scripts that copy them do not.

### Running tests locally

```bash
# Install dependencies
pip install pytest pytest-asyncio pytest-cov homeassistant aiohttp

# Run tests
pytest tests/ha_component/ -v

# With coverage
pytest tests/ha_component/ --cov=custom_components/ekey_ha_app --cov-report=term-missing

# The sidebar panel's render states (needs only node — no bundler, no browser)
node tests/panel_render_test.mjs
```

The panel is plain ES modules with no build step, so its enrolment dialog can be
rendered and asserted on in Node with a handful of DOM stubs. That covers the two
states only reachable mid-request — "starting" and "live" — which are the ones that
otherwise rot unnoticed.

CI runs hassfest validation, pytest on Python 3.13 (Home Assistant 2026.x needs it)
and the panel render tests — see
[`.github/workflows/ha-component.yml`](.github/workflows/ha-component.yml).

---

## Related

- **[ekey module Daemon add-on](https://github.com/agrisvi/ekey_module_addon)** —
  the backend packaged for Home Assistant OS. Add that repository in
  **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, install, point it at
  your RS485 converter, and this component talks to it on `localhost:8080`.
- **The standalone daemon and the ESP32 firmware** — the same backend for a plain
  Linux host and for the scanner-side product. Both speak the identical
  `/app/v1` API, so this component works against any of the three unchanged.

---

## License

See [LICENSE](LICENSE) for details.
