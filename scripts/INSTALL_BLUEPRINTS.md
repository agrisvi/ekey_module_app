# Blueprint Installation Scripts

Two scripts copy the shipped blueprints into your Home Assistant config directory.

**They only work from a clone of this repository.** HACS installs
`custom_components/ekey_ha_app/` and nothing else, so a HACS user never receives
these scripts — the blueprints themselves *are* installed (they live inside the
integration folder), but getting them into `config/blueprints/` is a manual step.
See [Manual installation](#manual-installation) below, which is the normal route.

## For Linux / Home Assistant OS

```bash
./scripts/install_blueprints.sh
```

## For Windows

```powershell
.\scripts\install_blueprints.ps1
```

Either can be run from anywhere — each resolves the blueprint source relative to
its own location, not the working directory. Both stop with an error if a
blueprint file is missing, rather than reporting success for a half-finished
install.

Both also list any *other* ekey blueprint they find elsewhere under
`config/blueprints/automation/`. That is not noise: Home Assistant identifies a
blueprint by its path, so an automation created from a stale copy keeps using that
copy no matter how many times you overwrite this folder.

## What gets installed

Only **automation** blueprints. Users and fingerprints are managed in the **ekey**
panel in the sidebar, so the enrol and delete script blueprints are gone — and with
them the `config/blueprints/script/ekey/` folder, which nothing uses now.

Source: `custom_components/ekey_ha_app/blueprints/`
Destination: `config/blueprints/automation/ekey/`

| File | What it does |
| --- | --- |
| `toggle_relay_on_granted.yaml` | Pulse an HA switch or relay when a known user is granted access |
| `welcome_notification.yaml` | Push a notification naming the person, to any `notify.*` service |
| `access_notification_list.yaml` | Add an entry to Home Assistant's own notification list (the bell), optionally including refusals |

That list is kept in step with the actual files by
`tests/ha_component/test_blueprints.py`, which reads the blueprint names back out of
both scripts and fails if either one has drifted.

## Manual installation

This is the route for a HACS install, and it works for a clone too.

### 1. Create the destination folder

In your Home Assistant config directory:

```text
config/
└── blueprints/
    └── automation/
        └── ekey/          ← Create this folder
```

### 2. Copy all three files into it

From `custom_components/ekey_ha_app/blueprints/` in your HA config directory:
`toggle_relay_on_granted.yaml`, `welcome_notification.yaml` and
`access_notification_list.yaml`.

Pasting the YAML works just as well: **Settings** → **Automations & scenes** →
**Blueprints** → **Import Blueprint**, paste the file contents, **Preview**,
**Import**. Note that the GitHub-URL field in that dialog cannot reach a blueprint
stored under `custom_components/`, so paste the contents rather than a link.

### 3. Reload

**Option A:** Developer Tools → YAML → Reload Automations

**Option B:** Restart Home Assistant

### 4. Create an automation

1. Go to **Settings** → **Automations & scenes** → **Blueprints**
2. Click **Create automation** on an **ekey** blueprint
3. Configure the inputs and save

See [`../custom_components/ekey_ha_app/blueprints/README.md`](../custom_components/ekey_ha_app/blueprints/README.md)
for what each blueprint does, its inputs, and why scanner-side reactions (LED, KNX,
MQTT, webhooks) belong on the backend instead.
