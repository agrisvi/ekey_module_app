# Quick Start Guide — ekey module App

**Tested with:** Home Assistant Container | Core 2026.4.4 | Frontend 20260325.8

Everything below happens in the **ekey** panel in the sidebar. There is no
dashboard to build and no script to create first — that was the old way, and the
helper entities it needed are gone.

---

## Step 1: Integration setup

**Settings** → **Devices & Services** → **Add Integration**, and search for
**ekey module App** — that is the name the integration registers, so searching for
"ekey" is the reliable way to find it.

The first screen asks *how* Home Assistant reaches the backend, and the answer
changes what it asks next:

| Choice | Use it for | Fields |
| --- | --- | --- |
| **Local — ekey-ha-daemon on this host (HTTP)** | the add-on, or a daemon on the same machine | Host (default `127.0.0.1`), Port (default `8080`), API token — **optional** here |
| **Remote device — ekey ESP32 (HTTPS + token)** | a scanner on the network | Host / IP, Port, API token — **required**, and *Verify SSL certificate*, which you leave **off** for the device's self-signed certificate |

The connection is validated before the entry is saved, so a wrong host or a rejected
token fails here rather than silently later.

A token is optional for a local daemon's `/api/v1`, but the **panel needs one** —
without it the user list cannot be read. Supply it even on a local install unless you
have no use for the panel.

Then two things appear:

**ekey** in the sidebar — the panel, visible to admin users.

Five entities under the **ekey Scanner** device:

- `sensor.ekey_scanner_info` — serial number, API and software version
- `sensor.ekey_enrolled_fingerprints` — count of fingerprints on the scanner
- `sensor.ekey_last_access` — most recent access result, for the Logbook
- `button.ekey_led_green` / `button.ekey_led_red` — make the scanner LED signal

Actual entity IDs depend on the instance name chosen during setup. The `ekey_`
prefix is the default. Verify your exact IDs via
**Settings** → **Devices & Services** → **Devices** → **ekey Scanner**.

### Where is the token?

- **Daemon / add-on:** `sudo cat /etc/ekey/app/token`
- **ESP32:** the device's own **Admin** page shows it after you log in

---

## Step 2: Add a user

1. Open **ekey** in the sidebar
2. If you have more than one scanner, pick it in the **Scanner** dropdown
3. Under **Add user**, enter a **Name**
4. Optionally choose a **Linked person** — a Home Assistant `person` entity. This
   is only an annotation: it is what lets an automation say *"Jane came home"*, and
   it is stored on the backend with the user, so it survives a Home Assistant
   reinstall. Leave it empty if you do not need it.
5. Click **Add user**

Users live on the scanner or daemon, not in Home Assistant. That is why they are
still there after you remove and re-add the integration.

---

## Step 3: Enrol a finger

1. Click **Enroll fingerprint…**
2. In the dialog, pick the **User** and the **Finger**
3. Click **Enroll fingerprint**
4. Place the finger on the scanner when the dialog asks, and keep placing the
   **same** finger until it completes — usually four placements, 30–60 seconds

Progress appears live in the dialog. **Cancel enrollment** stops a session that is
going badly; the user list refreshes by itself when one succeeds.

---

## Step 4: A fingerprint enrolled on the device itself

Anyone can enrol at the scanner's own Admin page, or at the device. Those
fingerprints belong to nobody as far as the user list is concerned, and they show
up under **Unassigned fingerprints**.

1. Click **Check again** if the list looks stale
2. Pick the user and finger, then click **Assign**

The fingerprint keeps working either way — assigning it is what makes the access
log say a name instead of "Unknown", and what lets
`toggle_relay_on_granted.yaml` accept it.

---

## Step 5: View and delete

The user list shows every user, their fingers, and whether each finger is actually
**on scanner**. Two mismatches are called out explicitly:

- **missing on scanner** — the user document has a finger the scanner does not.
  Enrol it again.
- **not linked to a person** — no `person` entity attached. Harmless, unless you
  want the name in automations.

**Edit** renames a user or changes the linked person. **Delete fingerprint** removes
one finger; **Delete user** removes the user and all of their fingers.

**Refresh** re-reads the backend immediately — useful after someone has been
working on the device's own Admin page.

---

## Step 6: Reactions to access

Decide *where* each reaction belongs. This is the one design choice worth a minute:

| Reaction | Configure it | Why |
| --- | --- | --- |
| Scanner LED, KNX, MQTT, webhook | the backend's **Admin** page → Actions + Automations | Runs on the scanner or daemon, so it works while Home Assistant is restarting, updating or down |
| A Home Assistant switch or relay | `toggle_relay_on_granted.yaml` blueprint here | Only Home Assistant can operate an HA entity |
| A phone notification | `welcome_notification.yaml` blueprint here | Same |
| An entry in HA's notification list (the bell) | `access_notification_list.yaml` blueprint here | Same — and it can record refusals too. That list is cleared on restart, so for a durable record use the logbook or the backend's own access log |

The three blueprints ship inside the integration, at
`config/custom_components/ekey_ha_app/blueprints/`, but Home Assistant does not load
them from there. Copy each one in through **Settings** → **Automations & scenes** →
**Blueprints** → **Import Blueprint** → paste the YAML → **Preview** → **Import**.
(From a clone of the repository, `scripts/install_blueprints.sh` — or `.ps1` on
Windows — does all three at once; a HACS install does not include that script.)

Then create automations from them under **Settings** → **Automations & scenes** →
**Blueprints**. Full descriptions and inputs are in
[`../blueprints/README.md`](../blueprints/README.md).

Note that green-on-match and red-on-mismatch LED feedback is **already built in**;
you only need a backend `led` action if you want it to keep working with Home
Assistant down.

---

## Troubleshooting

### The panel is not in the sidebar

It is admin-only. If you are an admin and it is still missing, restart Home
Assistant — the panel is registered once per Home Assistant start.

### The panel shows an old version of itself

The header shows `Integration <version>`. If that number is not the one in
`manifest.json`, the browser or Home Assistant is serving a cached copy: restart
Home Assistant, then hard-reload the page.

### Enrolment keeps failing

- Place your **entire finger** flat on the sensor — not just the tip
- Use **consistent placement** for every scan in the same session
- Clean the sensor surface and your finger before starting
- Hold still during each scan
- If it fails repeatedly, close the dialog and start a fresh session

### Home Assistant asks for the token again

A repair notice titled *"ekey token no longer accepted"* means the backend rejected
the stored token — normally because it was regenerated, or the backend was factory
reset. Click through it and enter the current token; nothing else about the entry
changes and no re-setup is needed.

### An automation shows *"Unknown entity selected"*

It refers to one of the entities removed in the app-layer version — usually
`select.ekey_enrolled_fingerprints`, from an automation built on the old relay-pulse
blueprint. The integration raises a repair notice naming both the entity and the
automation. Re-import the current blueprint and recreate the automation; updating the
integration does **not** update a blueprint you already imported. See
[`../blueprints/README.md`](../blueprints/README.md).

### A person is missing from **Linked person**

Only `person` entities are listed. Add the person under **Settings** → **People**;
they appear immediately.

### Entity IDs do not match an example

Your IDs depend on the instance name entered during setup. Check them at
**Settings** → **Devices & Services** → **Devices** → **ekey Scanner**.

---

## Advanced: using services directly

The panel is not the only route. From **Developer Tools** → **Actions**:

1. Select `ekey_ha_app.enroll_fingerprint`
2. Enter:

   ```yaml
   person_id: person.john_doe
   finger: 1
   ```

3. Click **Perform Action**
4. Place finger on scanner when prompted; progress arrives as notifications

`ekey_ha_app.delete_fingerprint` and `ekey_ha_app.set_led_brightness` work the same
way. See [`../README.md`](../README.md) for their parameters. Those three are the
only services this integration registers.

---

## Advanced: device options (ESP32 only)

The **Configure** button on the integration entry (**Settings** → **Devices &
Services** → *ekey module App* → **Configure**) offers two things:

- **Push Wi-Fi credentials to the device** — set the SSID, optionally the password,
  the mDNS host name and the HTTPS port, and optionally reboot to apply. The current
  values are pre-filled from the device. Leave the password blank to keep the stored
  one. A wrong password is caught on the device and rolled back automatically, but
  changing the network or the port reboots the device and may change its address.
- **Reset the device's Wi-Fi (return to setup mode)** — clears the stored credentials
  and reboots into the setup portal for re-provisioning. The scanner pairing and the
  API token are kept.

On a **local daemon** entry, **Configure** reports that there is nothing to configure:
the daemon has no `/config` API and its settings are its own.

---

## Next steps

- [`AUTOMATION_EXAMPLES.md`](AUTOMATION_EXAMPLES.md) — copy-paste automations built on
  the ekey events
- [`../blueprints/README.md`](../blueprints/README.md) — the three included blueprints
  and what belongs on the backend instead
- [`../README.md`](../README.md) — the complete entity, service and event reference
