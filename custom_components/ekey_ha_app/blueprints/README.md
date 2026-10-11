# ekey Home Assistant Blueprints

Five automation blueprints. All of them exist because they operate something that only
**Home Assistant** can operate — a switch entity, a notification service, its own
notification list, a Homematic CCU it is connected to — which is the one thing the
backend cannot do for you.

Anything that only involves the scanner (LED feedback, KNX, MQTT, a webhook) should
be an action on the backend instead, configured in the **Actions** and
**Automations** tabs of the device's Admin page. Those rules run on the scanner or
daemon, so they keep working while Home Assistant is restarting, updating or down.

There are no script blueprints any more: enrolling and deleting fingerprints is
what the **ekey** panel in the sidebar is for.

---

## toggle_relay_on_granted.yaml

Pulses a switch or relay ON for a configurable duration when the scanner grants
access to a **known** user. A fingerprint that maps to nobody is ignored.

Triggers on the `ekey_access_granted` event, which carries the resolved user name
and the scanner identity as fields.

Configurable:

- Switch / relay entity (e.g. `switch.iono_pi_relay_1`)
- ON duration in seconds (default: 3)
- Scanner (optional) — leave empty to react to every scanner; with more than one,
  pick that scanner's "ekey Scanner Info" sensor so the relay follows only its own
  door

> **Upgrading:** the previous version triggered on `sensor.ekey_last_access` and
> parsed its text, cross-checking against `select.ekey_enrolled_fingerprints`. That
> select no longer exists. Delete the old automation, re-import this blueprint and
> pick your relay again.
>
> **Updating the integration does not update your blueprint.** Home Assistant copies
> a blueprint into `config/blueprints/automation/…` when you import it and identifies
> it by that path from then on — nothing reads `custom_components/` again, and there is
> no auto-refresh for blueprints shipped by a custom integration (`async_populate()`
> seeds only Home Assistant's own examples, and only when the folder does not exist
> yet). So an old copy stays exactly as it was until you replace the file.
>
> The symptom if you don't: the automation editor shows the old blueprint's inputs,
> **ekey Enrolled Fingerprints** says *"Unknown entity selected"*, and its dropdown is
> empty — there is no such entity any more, so there is nothing to offer. The
> integration now raises a repair notice naming the automation when it sees this, but
> the fix is still manual, below.
>
> To fix it:
>
> 1. Note which relay and duration the old automation used, then delete it.
> 2. Re-import the current blueprint over the old one (paste the YAML, or re-run
>    `scripts/install_blueprints.sh` / `.ps1` from a repository clone — those overwrite
>    `config/blueprints/automation/ekey/` **and list any other ekey blueprint they find
>    elsewhere**, which is the case that makes this confusing: an automation created
>    from one of those is still using that file, not the one just installed).
> 3. Delete any stale copies — anything matching
>    `config/blueprints/automation/**/*.yaml` that mentions ekey but is not in the
>    `ekey/` folder.
> 4. **Developer Tools → YAML → Reload Automations**, then create the automation again
>    from the ekey blueprint and pick your relay.

## welcome_notification.yaml

Pushes a notification naming the person when the scanner grants access.

Configurable:

- Notification service (e.g. `notify.mobile_app_phone`)
- Message — a template; `{{ person }}` and `{{ finger }}` are in scope
- Scanner (optional)

Triggers on `ekey_access_granted`, and ignores a fingerprint that maps to nobody.

> **It used to be unable to name anyone.** The old version triggered on
> `ekey_fingerprint_matched`, which carries the raw APID and nothing else, so its
> message was the fixed string "Welcome home!" for every person, every time.
> `ekey_access_granted` is the same recognition after the integration has resolved the
> APID to a user, so the name is simply there. The `notify_service` input is unchanged
> and the two new inputs have defaults, so an automation created from the old version
> keeps working and starts saying something useful — **no re-import needed** for this
> one.

## access_notification_list.yaml

Adds an entry to **Home Assistant's own notification list** — the bell in the sidebar —
on every access. There is no notification-service field: the bell is the
`persistent_notification` integration, not a notify target, so it is called directly.

Configurable:

- **Keep a single entry** — ON: one notification rewritten on every access, so the bell
  shows the latest and nothing accumulates. OFF: a new entry per access, which builds a
  scrollable list you then have to dismiss one at a time.
- **Also record refusals** — adds an entry when a finger is rejected. Those carry no
  name (an unrecognised finger belongs to nobody), only the scanner's reason.
- Scanner (optional)

> **The notification list is cleared when Home Assistant restarts.** It is held in
> memory with no storage behind it, so it is a good inbox and a poor record. Two places
> keep a durable one: Home Assistant's **logbook** — these events carry the scanner's
> entity, so they appear in the device's activity view — and the **access log on the
> backend's own Admin page**, which is written by the daemon or the ESP32 and therefore
> survives Home Assistant being down entirely. The ekey panel in the sidebar does not
> show that log; it manages users and fingerprints only.

`notify.persistent_notification` in the *welcome* blueprint above also reaches the same
list, and is fine if that is all you want. Use this blueprint instead when you want one
row that updates, or refusals recorded, or both.

## ekey_to_homematic.yaml and ekey_to_homematicip_local.yaml

Forward an access to a **Homematic CCU** (CCU3, RaspberryMatic, Homegear) through the
Home Assistant integration you already use for it. Two files, one per integration —
import the one that matches your setup:

| File | For |
| --- | --- |
| `ekey_to_homematic.yaml` | the core [Homematic](https://www.home-assistant.io/integrations/homematic/) integration (`homematic`, YAML) |
| `ekey_to_homematicip_local.yaml` | **Homematic(IP) Local** (`homematicip_local`, HACS) |

Three things either can do on the CCU:

| Action | Core `homematic` | Homematic(IP) Local | Use it for |
| --- | --- | --- | --- |
| **Press a virtual key** (recommended) | `homematic.virtualkey` | `homematicip_local.set_device_value`, `PRESS_SHORT`/`PRESS_LONG` on the key's channel | A CCU program "when virtual key *N* is pressed" — immediate, and the door logic stays on the CCU |
| **Set a system variable** | `homematic.set_variable_value` | `homematicip_local.set_variable_value` | CCU programs that need to know *who* it was (write the person's name) or just *that* it happened |
| **Set a device value** | `homematic.set_device_value` | `homematicip_local.set_device_value` | Switching a Homematic actuator straight from Home Assistant, e.g. `STATE` = `true` on a door opener |

Configurable:

- **When:** access granted (known user), denied, or either; optional comma-separated
  person names; optional scanner
- **Homematic:** which action and — core integration only — the interface name from
  your `homematic:` configuration (e.g. `ip` for HmIP)
- **Virtual key:** remote address (`BidCoS-RF`, keys 1–50; `HmIP-RCV-1`, keys 1–48),
  key number, short or long press
- **System variable:** the CCU hub entity (required with Homematic(IP) Local, optional
  with one CCU on the core integration), the variable's name as on the CCU, and what to
  write — the person, `granted`/`denied`, `true`, `false` or custom text
- **Device value:** address, channel, parameter, value and value type

Only the section for the chosen action is used. Runs queued, so several fingers in
quick succession are each forwarded in order.

> **Prepare the CCU first.** A virtual key does nothing until a CCU program uses it:
> in the CCU WebUI create a program with the condition *"Virtual remote key N pressed
> short"* (*Programme und Verknüpfungen → Programme*). A system variable has to exist
> on the CCU, with the type matching what you write — a *character string* for the
> person's name, a *logic value* for `true`/`false`.
>
> **A refusal carries no name.** An unrecognised finger belongs to nobody, so for
> *denied* the person filter is skipped and "the person's name" writes an empty string.
>
> **This runs in Home Assistant**, so it stops while Home Assistant is down. For a door
> that must open regardless, use an action on the scanner itself (Admin page →
> **Actions** / **Automations**).

---

## How to install

Home Assistant does not load a blueprint from `custom_components/`. It only reads
`config/blueprints/automation/`, so the files you want have to be *copied* there once,
whichever method you use below.

### Method 1: copy and paste the YAML (works on every install)

1. Open the blueprint file — on your HA machine it is at
   `config/custom_components/ekey_ha_app/blueprints/<name>.yaml` — and copy all of it
2. **Settings** → **Automations & scenes** → **Blueprints**
3. **Import Blueprint** → paste → **Preview** → **Import**

The GitHub-URL field in that dialog cannot reach a blueprint stored under
`custom_components/`, so paste the contents rather than a link.

### Method 2: manual file copy

Copy the `.yaml` files into `config/blueprints/automation/ekey/`, creating the
folder if needed, then **Developer Tools** → **YAML** → **Reload Automations**. There
is no `config/blueprints/script/ekey/` any more — nothing goes there.

### Method 3: the installer script (repository clone only)

```bash
./scripts/install_blueprints.sh          # Linux, Home Assistant OS
.\scripts\install_blueprints.ps1         # Windows
```

The scripts live in `scripts/` at the root of the repository, **not inside the
integration** — HACS installs `custom_components/ekey_ha_app/` and nothing else, so a
HACS user does not have them and should use Method 1 or 2.

Where they are available they copy every blueprint file into
`config/blueprints/automation/ekey/`, stop with an error if any is missing — no silent
half-installs — and list any other ekey blueprint they find elsewhere, because an
automation created from one of those still uses that copy rather than the one just
installed.

Then: **Developer Tools** → **YAML** → **Reload Automations**.

---

## After importing

1. **Settings** → **Automations & scenes** → **Blueprints**
2. Click **Create automation** on the ekey blueprint
3. Fill in the inputs and save

---

## A complete door setup

1. In the panel, add your users and enrol their fingers.
2. On the backend's Admin page → **Actions**, add an `led` action; under
   **Automations**, link it to `match_ok` and a red one to `match_nok`. This is the
   scanner's own feedback and needs no Home Assistant.
3. Here, create an automation from **toggle_relay_on_granted.yaml** pointing at
   your door relay.
4. Optionally add **welcome_notification.yaml** for a phone notification, and
   **access_notification_list.yaml** if you want each access visible on the bell —
   with *Keep a single entry* ON unless you really want a list to dismiss by hand.

The door opens even if Home Assistant is down only if the relay is wired to the
backend (a `gpio`, `knx` or `mqtt` action). A relay that is a Home Assistant entity
depends on Home Assistant — that is a wiring decision, not a configuration one.

---

## Support

For issues or questions, see [the integration README](../README.md) or open an issue
on GitHub:
<https://github.com/agrisvi/ekey_module_app/issues>
