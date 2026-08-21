# ekey Home Assistant Automation Examples

This document provides example automations for the ekey Home Assistant App integration.

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

Two, both in `custom_components/ekey_ha_app/blueprints/`:

1. **toggle_relay_on_granted.yaml** — pulse an HA switch or relay when a known user
   is granted access
2. **welcome_notification.yaml** — send a notification on access

Install them with `./install_blueprints.sh` (`.\install_blueprints.ps1` on
Windows), or paste the YAML under **Settings** → **Automations & scenes** →
**Blueprints** → **Import Blueprint**. See `blueprints/README.md`.

Everything else in this document is copy-paste YAML you write yourself.

---

## Managing Fingerprints

Enrolment, deletion and assignment are done in the panel — see `QUICKSTART.md`.
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
        event_type: ekey_fingerprint_matched
    action:
      - service: notify.mobile_app_phone
        data:
          message: "Welcome home!"
          title: "Door Access"
```

### Door access denied alert

```yaml
automation:
  - alias: "ekey: Access Denied Alert"
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

```yaml
automation:
  - alias: "ekey: Log Access Attempts"
    trigger:
      - platform: event
        event_type: ekey_fingerprint_matched
      - platform: event
        event_type: ekey_fingerprint_not_matched
    action:
      - service: logbook.log
        data:
          name: ekey Scanner
          message: >
            {% if trigger.event.event_type == 'ekey_fingerprint_matched' %}
              Fingerprint matched: {{ trigger.event.data.apid }}
            {% else %}
              Fingerprint not matched: {{ trigger.event.data.apfar_desc }}
            {% endif %}

automation:
  - alias: "ekey: Turn on lights on successful scan"
    trigger:
      - platform: event
        event_type: ekey_fingerprint_matched
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

### Automatic enrollment confirmation

```yaml
automation:
  - alias: "ekey: Auto-confirm Enrollment"
    trigger:
      - platform: event
        event_type: ekey_enrollment_state
    condition:
      - condition: template
        value_template: "{{ trigger.event.data.enstat == 35 }}"  # wait_for_confirmation
    action:
      - service: ekey_ha_app.confirm_enrollment
        data:
          apid: "{{ trigger.event.data.apid }}"
          finger: "{{ trigger.event.data.id }}"
```

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

```yaml
automation:
  - alias: "ekey: Person-Specific Welcome"
    trigger:
      - platform: event
        event_type: ekey_fingerprint_matched
    action:
      - service: script.welcome_home
        data:
          apid: "{{ trigger.event.data.apid }}"

script:
  welcome_home:
    sequence:
      - variables:
          person_name: >
            {% set apid = apid %}
            {% set ns = namespace(found='Guest') %}
            {% for person in states.person %}
              {# Check if this person has this fingerprint registered #}
              {% if state_attr('sensor.ekey_enrolled_fingerprints', 'fingerprints') %}
                {% for fp in state_attr('sensor.ekey_enrolled_fingerprints', 'fingerprints') %}
                  {% if fp.apid == apid %}
                    {% set ns.found = person.name %}
                  {% endif %}
                {% endfor %}
              {% endif %}
            {% endfor %}
            {{ ns.found }}
      - service: tts.google_translate_say
        target:
          entity_id: media_player.home_speaker
        data:
          message: "Welcome home, {{ person_name }}!"
```

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

      # Wait for match result (max 5 seconds)
      - wait_for_trigger:
          - platform: event
            event_type: ekey_fingerprint_matched
          - platform: event
            event_type: ekey_fingerprint_not_matched
        timeout: 5

      # Handle result
      - choose:
          # Match successful - open door
          - conditions:
              - condition: template
                value_template: "{{ wait.trigger.event.event_type == 'ekey_fingerprint_matched' }}"
            sequence:
              - service: lock.unlock
                target:
                  entity_id: lock.front_door
              - service: light.turn_on
                target:
                  entity_id: light.entrance
              - service: notify.mobile_app_phone
                data:
                  message: "Door unlocked by {{ wait.trigger.event.data.apid }}"

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
        event_type: ekey_enrollment_state
      - platform: event
        event_type: ekey_connection_lost
    action:
      - service: persistent_notification.create
        data:
          title: "ekey Event: {{ trigger.event.event_type }}"
          message: "{{ trigger.event.data | tojson }}"
```
