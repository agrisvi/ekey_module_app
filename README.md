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

The one exception is the [fingerprint database](#fingerprint-storage--the-central-database):
a copy of every fingerprint *template*, held in Home Assistant because a template cannot
be re-derived and nothing else in the system keeps one. It never takes part in opening a
door — it exists so a sensor can be repaired, replaced or added without asking everyone
to enroll again.

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

**Install the backend first.** This repository is the Home Assistant half, and the
config flow validates the connection before it will save an entry — so with no
backend running you get as far as the last screen and are turned away. See
[Requirements](#requirements) above; on Home Assistant OS the easiest route is the
[ekey module Daemon add-on](https://github.com/agrisvi/ekey_module_addon).

### Option 1: HACS — one click

[![Open your Home Assistant instance and open a repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=agrisvi&repository=ekey_module_app&category=integration)

That opens HACS on **your own** Home Assistant with this repository already filled in.
Press **Download**, then restart Home Assistant.

The same thing by hand, if the link cannot reach your instance:

1. **HACS** → **⋮** (top right) → **Custom repositories**
2. Repository `https://github.com/agrisvi/ekey_module_app`, type **Integration**
3. **Add**, then find **ekey module App** in the list and **Download**
4. Restart Home Assistant

HACS is not part of Home Assistant and has to be installed once first. If there is no
**HACS** entry in your sidebar you do not have it — see
[hacs.xyz](https://hacs.xyz/docs/use/download/download/), or skip it and use Option 2,
which installs exactly the same files.

### Option 2: Manual — no HACS

Copy `custom_components/ekey_ha_app/` from this repository into your Home Assistant
configuration directory, so that `config/custom_components/ekey_ha_app/manifest.json`
exists. From a shell on the Home Assistant host:

```bash
cd /tmp
wget -O app.tar.gz https://github.com/agrisvi/ekey_module_app/archive/refs/heads/main.tar.gz
tar xzf app.tar.gz
mkdir -p /config/custom_components
cp -r ekey_module_app-main/custom_components/ekey_ha_app /config/custom_components/
```

### Either way: restart, do not reload

A custom integration is discovered only at Home Assistant **startup**. Reloading YAML
or refreshing the page will not find it — and the symptom is that **ekey module App**
simply does not appear under **Add Integration**, which looks exactly like a failed
download. Restart first, then go looking.

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

### Configure — the connection settings

Every entry gets a **Configure** button, and what it offers is decided by the backend
rather than by the connection mode:

| Entry | What it offers |
| --- | --- |
| **Scanner connection (serial port)** | which serial port the scanner is wired to, on any backend where that is a setting — a daemon or add-on on a Linux host. A device with the sensor on fixed UART pins does not have it |
| **Push Wi-Fi credentials** / **Reset the device's Wi-Fi** | ESP32 entries only — only a device owns its own network settings |

The port picker lists every port the backend enumerated, flagging the machine's own
`[system console]` (choosing one asks for confirmation, because it can switch that UART
into RS485 mode) and any port `[in use]` by another process. After saving, the dialog
says whether the scanner is connected on the new port within 30 seconds or whether the
backend has to restart first — read from the reply, because a backend already bound to
a port cannot be re-pointed while it runs.

Where the port is set outside Home Assistant — the add-on's own configuration, or the
daemon's `-d` option — the dialog reports which port is in use and where to change it,
rather than offering a control that would be refused. An entry with nothing to
configure at all says so.

> The port used to be a card on the sidebar panel. It moved here because it is a
> connection setting, next to the host and token that reach the same backend.

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

## Fingerprint storage — the central database

The panel's scanner dropdown has one extra entry, **Fingerprint storage**, under a
*Home Assistant* group. It is not a scanner: it is Home Assistant's own copy of every
fingerprint *template*, and it exists because a template cannot be re-derived from
anything. Replace a sensor or factory-reset one, and without a copy every person on it
has to come back and present a finger again.

What makes a central copy possible is that **the APID lives inside the template blob,
in plaintext** — so a template written to a second scanner keeps its identity. One
physical finger has one APID across the whole fleet.

> **The database never opens a door.** A scanner does that. Deleting everything here
> takes nobody's finger off any sensor; what it costs is the ability to repair a
> scanner, add one, or recover from a factory reset.

### The presence matrix

Each finger row carries one chip per configured scanner, so the question an
administrator actually has — *is this finger on all my doors?* — is answered at a
glance:

| Chip | Meaning |
| --- | --- |
| `ok` | that sensor holds it |
| `missing` | the database has it, that sensor does not — fixable with **Push** |
| `extra` | that sensor has it, the database does not — fixable with **Adopt** |
| `?` | that scanner's list could not be read. **Not** the same as missing; nothing is assumed |
| `n/a` | it can never be copied there — a different device variant, or a backend with no template routes |

Past four scanners the healthy chips collapse into one (`ok on 7`) so that deviations
stand out; the collapsed names stay in the tooltip.

### What each button does

| Action | Effect |
| --- | --- |
| **Sync from a scanner…** | reads that scanner's templates into the database. Writes nothing to it, and nobody presents a finger |
| **Sync to storage…** (on a scanner's card) | the same thing, previewed first, from the scanner you are looking at |
| **Adopt into database** | pulls in one fingerprint the database was missing, keeping its APID |
| **Push…** | writes stored templates to the scanners that lack them, *and* names the owner in each scanner's own user list, so that sensor keeps working — with the right person — when Home Assistant is down |
| **Create backup…** | an encrypted file, downloaded to your computer |
| **Restore backup…** | reads a file back. **Writes to no scanner**: afterwards the matrix shows what is missing where, and you choose what to push |
| **Clean storage…** | deletes every record. Typed confirmation; the scanners are untouched |

**Nothing is ever pushed automatically.** Drift is detected continuously and displayed,
but a write to a door controller only ever happens when you click. Transfers are slow —
a few seconds per fingerprint, because the sensor spends ~1.9 s registering each one —
so bulk actions run as a background job with live per-item progress and a **Stop** that
takes effect between fingerprints, never inside one.

A write is only reported as successful when the scanner confirms it **kept** the
template. The endpoint answers HTTP 200 even when it accepted a transfer and discarded
it, so success is read from the `verified` field and nothing else.

### Backups contain biometric data

A backup file holds working fingerprint templates. Anyone who copies it can write those
fingerprints into any scanner of the same device variant — which is to say, mint working
credentials for the building. So:

- backups are **passphrase-encrypted by default** (scrypt + AES-256-GCM, built on Home
  Assistant's side; the file's own header is authenticated, so an edited header will not
  open). An unencrypted export exists behind an explicit checkbox and says what it is;
- a lost passphrase is a lost backup — there is no recovery path;
- the database itself lives in `.storage/ekey_ha_app.fingerprint_vault` as plain JSON,
  which means **it is included in Home Assistant's own backups**. Encrypting it with a
  key stored beside it would be theatre, so it is not done; use HA's encrypted backups
  and keep the config directory private;
- if Home Assistant is served over plain HTTP, templates and the backup passphrase
  cross your network in the clear. That is a reason to put HA behind HTTPS, and it is
  not something this feature can fix.

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

The panel is plain ES modules with no build step, so its dialogs can be rendered and
asserted on in Node with a handful of DOM stubs. That covers the states only reachable
mid-request — enrolment's "starting" and "live", and every step of the storage view's
upload and job dialogs — which are the ones that otherwise rot unnoticed.

The fingerprint database has its own suites, and they are written around the failures
rather than the happy paths: `test_templates.py` (a blob that is really a saved error
reply, a truncated one, one belonging to another finger), `test_backup.py` (a wrong
passphrase, an edited header, a foreign file), `test_jobs.py` (`verified: false` never
counted as stored, a variant mismatch skipped rather than retried, the 24 kB user-document
cap) and `test_storage_ws.py` (an unreadable scanner list reported as unknown, a restore
that touches no scanner).

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
