# ekey Home Assistant Automation Examples

This document provides example automations for the **ekey module App** integration.

## Where things belong

Read this first — it decides which of the examples below you actually need.

| Task | Where it lives |
| --- | --- |
| Add a user, enrol or delete a finger, assign an unassigned one | the **ekey** panel in the sidebar |
| React on the scanner: LED, KNX, MQTT, webhook | the backend's **Admin** page → Actions + Automations |
| React in Home Assistant: switches, notifications, lights, media | an automation here |

The middle row matters: rules configured on the backend run on the scanner or
daemon, so they keep firing while Home Assistant is restarting, updating or down.
An automation in Home Assistant cannot. Use Home Assistant for the things only
Home Assistant can reach.

## Blueprints

Three, all in `custom_components/ekey_ha_app/blueprints/`:

1. **toggle_relay_on_granted.yaml** — pulse an HA switch or relay when a known user
   is granted access
2. **welcome_notification.yaml** — push a notification naming the person, to any
   `notify.*` service
3. **access_notification_list.yaml** — add an entry to Home Assistant's own
   notification list (the bell), optionally including refusals

Home Assistant does not load blueprints from `custom_components/`, so each has to be
copied in once: **Settings** → **Automations & scenes** → **Blueprints** → **Import
Blueprint** → paste the YAML. From a clone of the repository,
`scripts/install_blueprints.sh` (`.\scripts\install_blueprints.ps1` on Windows) does
all three; a HACS install does not include that script. See
[`../blueprints/README.md`](../blueprints/README.md).

Everything else in this document is copy-paste YAML you write yourself.

## Which event to trigger on

Most of the examples below use `ekey_access_granted` and `ekey_access_denied`. Those
are the *resolved* events: the integration has already turned the scanner's raw APID
into a user, so `trigger.event.data.person_name` and `.finger` are simply there.

`ekey_fingerprint_matched` / `ekey_fingerprint_not_matched` are the raw scanner events
that precede them. Use those only when you genuinely want the APID or the `apfar`
reason code — a welcome message built on them cannot name anybody.

---

## Managing Fingerprints

Enrolment, deletion and assignment are done in the panel — see
[`QUICKSTART.md`](QUICKSTART.md).
The services below exist for scripted use.

### Manual enrollment (service call)

1. Go to **Developer Tools** → **Actions**
2. Select service: `ekey_ha_app.enroll_fingerprint`
3. Fill in the parameters:

   ```yaml
   person_id: person.john_doe
   finger: 1  # Finger 1-10
   ```

4. Press **Perform Action**
5. Place your finger on the scanner when prompted
6. The enrollment completes automatically via SSE events

### How to delete a fingerprint manually

1. Go to **Developer Tools** → **Actions**
2. Select service: `ekey_ha_app.delete_fingerprint`
3. Fill in the parameters:

   ```yaml
   person_id: person.john_doe
   finger: 1
   ```

4. Press **Perform Action**

### Dashboard management card

There is no longer a card for this, and building one is not worth your time: the
**ekey** panel in the sidebar is the management UI, and it is one click away from
any dashboard.

A card built out of entities is what this integration used to offer, with a
read-only dropdown, two buttons that printed information into notifications, and two
scripts. All five are gone. If you want the panel closer to hand, add a
[navigation button](https://www.home-assistant.io/dashboards/actions/#navigate) to
your dashboard:

```yaml
type: button
name: ekey users
icon: mdi:fingerprint
tap_action:
  action: navigate
  navigation_path: /ekey
```

For status on a dashboard, use the sensors — see *Status card* under
**Lovelace Dashboard Examples** below.

---

## Fingerprint Recognition Events

### Welcome home notification

```yaml
automation:
  - alias: "ekey: Welcome Home"
    trigger:
      - platform: event
        event_type: ekey_access_granted
    action:
      - service: notify.mobile_app_phone
        data:
          message: "Welcome home, {{ trigger.event.data.person_name }}!"
          title: "Door Access"
```

`welcome_notification.yaml` is this automation as a blueprint, with the message and
the scanner as configurable inputs — use that unless you want to hand-edit the YAML.

### Door access denied alert

```yaml
automation:
  - alias: "ekey: Access Denied Alert"
    trigger:
      - platform: event
        event_type: ekey_access_denied
    action:
      - service: notify.mobile_app_phone
        data:
          message: "Door access denied — {{ trigger.event.data.apfar_desc }}"
          title: "Security Alert"
```

To narrow it to one reason, trigger on the raw event instead — only that one carries
the numeric code:

```yaml
automation:
  - alias: "ekey: Poor Quality Scan"
    trigger:
      - platform: event
        event_type: ekey_fingerprint_not_matched
    condition:
      - condition: template
        value_template: "{{ trigger.event.data.apfar == 30 }}"  # Bad quality
    action:
      - service: notify.mobile_app_phone
        data:
          message: "Door access denied - poor fingerprint quality"
          title: "Security Alert"
```

### Log all access attempts

Both access events are **already** written to the Home Assistant logbook by the
integration, as `ekey Access — Access GRANTED: <name> (finger <n>)` and
`ekey Access — Access DENIED: <reason>`. You only need an automation like this one to
log something different:

```yaml
automation:
  - alias: "ekey: Log Access Attempts"
    trigger:
      - platform: event
        event_type: ekey_access_granted
      - platform: event
        event_type: ekey_access_denied
    action:
      - service: logbook.log
        data:
          name: ekey Scanner
          message: >
            {% if trigger.event.event_type == 'ekey_access_granted' %}
              Access granted: {{ trigger.event.data.person_name }}
              (finger {{ trigger.event.data.finger }})
            {% else %}
              Access denied: {{ trigger.event.data.apfar_desc }}
            {% endif %}
```

### Turn on lights on a successful scan

```yaml
automation:
  - alias: "ekey: Turn on lights on successful scan"
    trigger:
      - platform: event
        event_type: ekey_access_granted
    action:
      - service: light.turn_on
        target:
          entity_id: light.entrance
        data:
          brightness_pct: 100
          transition: 1
```

---

## Enrollment Workflow

Enrolment is driven from the panel's **Enroll fingerprint** dialog, which shows the
progress below live and needs no automation. The events are here for the cases the
dialog cannot cover — announcing a result on a speaker, or logging every attempt.

There is **no** `confirm_enrollment` service: `enstat == 35`
(*wait_for_confirmation*) is confirmed by the panel session that started the
enrolment, or by the backend itself. The three services this integration registers are
`enroll_fingerprint`, `delete_fingerprint` and `set_led_brightness`, and nothing else.

The examples below filter `ekey_enrollment_state` by its numeric `enstat`, which is
the raw progress stream. If you only care whether it worked, `ekey_enrollment_complete`
fires once at the end with a plain `success` boolean and is the easier trigger.

### Enrollment success notification

```yaml
automation:
  - alias: "ekey: Enrollment Success"
    trigger:
      - platform: event
        event_type: ekey_enrollment_state
    condition:
      - condition: template
        value_template: "{{ trigger.event.data.enstat == 40 }}"  # finished_success
    action:
      - service: notify.mobile_app_phone
        data:
          message: "Fingerprint enrolled successfully!"
          title: "ekey Enrollment"
```

### Enrollment failed alert

```yaml
automation:
  - alias: "ekey: Enrollment Failed"
    trigger:
      - platform: event
        event_type: ekey_enrollment_state
    condition:
      - condition: template
        value_template: >
          {{ trigger.event.data.enstat in [50, 60, 70] }}  # quit/timeout/duplicate
    action:
      - service: notify.mobile_app_phone
        data:
          message: >
            Enrollment failed:
            {% if trigger.event.data.enstat == 50 %}Cancelled by user
            {% elif trigger.event.data.enstat == 60 %}Timeout
            {% elif trigger.event.data.enstat == 70 %}Duplicate fingerprint
            {% endif %}
          title: "ekey Enrollment Failed"
```

---

## LED Control

Two buttons exist, `button.ekey_led_green` and `button.ekey_led_red`. **There is no
"LED off" button** — the scanner returns to its own signalling on the next event, so
these are momentary signals, not a state you have to clear.

### Do not write "flash on match" automations

The integration **already** turns the LED green on a match and red on a mismatch,
by itself, with no configuration. Adding an automation for it means the scanner
receives the command twice.

If you want that feedback to survive a Home Assistant restart or update, configure
it on the backend instead: Admin page → **Actions** → add an `led` action, then
**Automations** → link it to `match_ok` (and a red one to `match_nok`). Then it runs
on the scanner and needs nothing from Home Assistant. Do not do both.

### Signal something the scanner does not know about

This is what the buttons are actually for — an event the scanner has no way to
detect:

```yaml
automation:
  - alias: "ekey: Red LED while the alarm is armed"
    trigger:
      - platform: state
        entity_id: alarm_control_panel.home
        to: "armed_away"
    action:
      - service: button.press
        target:
          entity_id: button.ekey_led_red

  - alias: "ekey: Green LED when the doorbell is answered"
    trigger:
      - platform: state
        entity_id: binary_sensor.doorbell_answered
        to: "on"
    action:
      - service: button.press
        target:
          entity_id: button.ekey_led_green
```

### Adjust LED brightness by time of day

```yaml
automation:
  - alias: "ekey: Bright LED during day"
    trigger:
      - platform: sun
        event: sunrise
    action:
      - service: ekey_ha_app.set_led_brightness
        data:
          brightness: 100

  - alias: "ekey: Dim LED at night"
    trigger:
      - platform: sun
        event: sunset
    action:
      - service: ekey_ha_app.set_led_brightness
        data:
          brightness: 30
```

---

## Person-Based Actions

### Person-specific welcome

The integration resolves the fingerprint to a user before firing
`ekey_access_granted`, so the name needs no lookup — it is a field on the event. An
unrecognised fingerprint arrives as `Unknown`.

```yaml
automation:
  - alias: "ekey: Person-Specific Welcome"
    trigger:
      - platform: event
        event_type: ekey_access_granted
    condition:
      - condition: template
        value_template: "{{ trigger.event.data.person_name != 'Unknown' }}"
    action:
      - service: tts.google_translate_say
        target:
          entity_id: media_player.home_speaker
        data:
          message: "Welcome home, {{ trigger.event.data.person_name }}!"
```

### Do something different per person

```yaml
automation:
  - alias: "ekey: Per-Person Scene"
    trigger:
      - platform: event
        event_type: ekey_access_granted
    action:
      - choose:
          - conditions:
              - condition: template
                value_template: "{{ trigger.event.data.person_name == 'Jane' }}"
            sequence:
              - service: scene.turn_on
                target:
                  entity_id: scene.jane_evening
          - conditions:
              - condition: template
                value_template: "{{ trigger.event.data.person_name == 'John' }}"
            sequence:
              - service: scene.turn_on
                target:
                  entity_id: scene.john_evening
```

The name is the **user name on the backend**, set in the panel. Linking a user to a
Home Assistant `person` entity does not change it — that link is what lets you reach
the person's own attributes if you need them.

---

## Connection Monitoring

### Connection lost alert

```yaml
automation:
  - alias: "ekey: Connection Lost"
    trigger:
      - platform: event
        event_type: ekey_connection_lost
    action:
      - service: persistent_notification.create
        data:
          title: "ekey Scanner Offline"
          message: "Connection to ekey daemon lost. Check service status."
          notification_id: ekey_connection_lost
```

---

## Complete Door Access Scenario

```yaml
automation:
  - alias: "ekey: Complete Door Access Flow"
    mode: restart
    trigger:
      - platform: event
        event_type: ekey_finger_touch
    action:
      # Flash LED to acknowledge touch
      - service: button.press
        target:
          entity_id: button.ekey_led_green

      # Wait for the resolved result (max 5 seconds)
      - wait_for_trigger:
          - platform: event
            event_type: ekey_access_granted
          - platform: event
            event_type: ekey_access_denied
        timeout: 5

      # Handle result
      - choose:
          # Match successful - open door
          - conditions:
              - condition: template
                value_template: "{{ wait.trigger.event.event_type == 'ekey_access_granted' }}"
            sequence:
              - service: lock.unlock
                target:
                  entity_id: lock.front_door
              - service: light.turn_on
                target:
                  entity_id: light.entrance
              - service: notify.mobile_app_phone
                data:
                  message: "Door unlocked by {{ wait.trigger.event.data.person_name }}"

        # Match failed - deny access
        default:
          - service: button.press
            target:
              entity_id: button.ekey_led_red
          - service: notify.mobile_app_phone
            data:
              message: "Door access denied"
              data:
                priority: high

```

> The red LED above is the one case worth keeping in Home Assistant only if you
> have not configured a backend `match_nok` action — otherwise the scanner gets the
> command twice. There is no "LED off" step: the scanner clears its own signalling.

---

## Service Call Examples

### Enroll fingerprint for person

```yaml
service: ekey_ha_app.enroll_fingerprint
data:
  person_id: person.john_doe
  finger: 1
```

### Delete fingerprint

```yaml
service: ekey_ha_app.delete_fingerprint
data:
  person_id: person.john_doe
  finger: 1
```

### Set LED brightness

```yaml
service: ekey_ha_app.set_led_brightness
data:
  brightness: 75
```

---

## Lovelace Dashboard Examples

### Status card, with a link to the panel

```yaml
type: vertical-stack
cards:
  - type: entities
    title: ekey Scanner
    entities:
      - sensor.ekey_scanner_info
      - sensor.ekey_enrolled_fingerprints
      - sensor.ekey_last_access

  - type: horizontal-stack
    cards:
      - type: button
        name: Manage users
        icon: mdi:fingerprint
        tap_action:
          action: navigate
          navigation_path: /ekey
      - type: button
        tap_action:
          action: call-service
          service: button.press
          target:
            entity_id: button.ekey_led_green
        name: Green
        icon: mdi:led-on
      - type: button
        tap_action:
          action: call-service
          service: button.press
          target:
            entity_id: button.ekey_led_red
        name: Red
        icon: mdi:led-on
```

### LED brightness slider (requires a manual `input_number` helper)

The integration does not create an `input_number` entity for LED brightness.
If you want a brightness slider, create a helper manually:

1. Go to **Settings** → **Devices & Services** → **Helpers** → **Create Helper**
2. Choose **Number** and configure it (min: 0, max: 100, step: 10)
3. Note the entity ID (e.g., `input_number.ekey_led_brightness`)

Then use this card and automation:

```yaml
# Dashboard card
type: entities
title: LED Brightness
entities:
  - type: custom:slider-entity-row
    entity: input_number.ekey_led_brightness
    name: Brightness
    min: 0
    max: 100
    step: 10
```

```yaml
# Automation to apply the slider value
automation:
  - alias: "ekey: Update LED Brightness"
    trigger:
      - platform: state
        entity_id: input_number.ekey_led_brightness
    action:
      - service: ekey_ha_app.set_led_brightness
        data:
          brightness: "{{ states('input_number.ekey_led_brightness') | int }}"
```

---

## Debug Automation

```yaml
automation:
  - alias: "ekey: Debug All Events"
    trigger:
      - platform: event
        event_type: ekey_finger_touch
      - platform: event
        event_type: ekey_fingerprint_matched
      - platform: event
        event_type: ekey_fingerprint_not_matched
      - platform: event
        event_type: ekey_access_granted
      - platform: event
        event_type: ekey_access_denied
      - platform: event
        event_type: ekey_enrollment_state
      - platform: event
        event_type: ekey_enrollment_started
      - platform: event
        event_type: ekey_enrollment_complete
      - platform: event
        event_type: ekey_fingerprint_deleted
      - platform: event
        event_type: ekey_users_changed
      - platform: event
        event_type: ekey_connection_lost
    action:
      - service: persistent_notification.create
        data:
          title: "ekey Event: {{ trigger.event.event_type }}"
          message: "{{ trigger.event.data | tojson }}"
```

A single touch produces several of these in order: `ekey_finger_touch`, then
`ekey_fingerprint_matched` or `_not_matched`, then the resolved
`ekey_access_granted` / `ekey_access_denied`. Triggering on more than one of them for
the same reaction runs it twice.
