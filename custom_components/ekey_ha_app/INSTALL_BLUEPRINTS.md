# Blueprint Installation Scripts

Two scripts copy the blueprints into your Home Assistant config directory:

## For Linux/Home Assistant OS

```bash
./install_blueprints.sh
```

## For Windows

```powershell
.\install_blueprints.ps1
```

Both stop with an error if a blueprint file is missing, rather than reporting
success for a half-finished install.

## What gets installed

Only **automation** blueprints. Users and fingerprints are managed in the **ekey**
panel in the sidebar, so the enrol and delete script blueprints are gone — and with
them the `config/blueprints/script/ekey/` folder, which nothing uses now.

Source: `custom_components/ekey_ha_app/blueprints/`

| File | What it does |
| --- | --- |
| `toggle_relay_on_granted.yaml` | Pulse an HA switch or relay when a known user is granted access |
| `welcome_notification.yaml` | Send a notification on access |

## Manual Installation

### 1. Create the destination folder

In your Home Assistant config directory:

```text
config/
└── blueprints/
    └── automation/
        └── ekey/          ← Create this folder
```

### 2. Copy both files into it

`toggle_relay_on_granted.yaml` and `welcome_notification.yaml`.

### 3. Reload

**Option A:** Developer Tools → YAML → Reload Automations

**Option B:** Restart Home Assistant

### 4. Create an automation

1. Go to **Settings** → **Automations & scenes** → **Blueprints**
2. Click **Create automation** on an **ekey** blueprint
3. Configure the inputs and save

See `blueprints/README.md` for what each blueprint does, its inputs, and why
scanner-side reactions (LED, KNX, MQTT, webhooks) belong on the backend instead.
