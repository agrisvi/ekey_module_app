# Home Assistant ekey module Integration - Design Specification

> ## ⚠ Historical document
>
> This describes the design **before the app layer**, when Home Assistant was the
> only place users and fingerprint mappings existed and the user interface was built
> out of entities. It is kept as a record of that design and of the reasoning behind
> the parts that did survive (config flow, coordinator, SSE listener, events,
> logbook, services).
>
> What has changed since, and where the current description lives:
>
> | This document says | Now |
> | --- | --- |
> | Home Assistant stores the person → fingerprint map | The **backend** owns users and fingerprints; HA keeps only an optional `ha_person` link, stored on the backend user record. The old map is preserved under a `legacy` key and never deleted. |
> | Enrolment is a script blueprint driven by `select.ekey_person_selector` + `select.ekey_finger_selector` | The **ekey panel** in the sidebar, with live progress. Both selects, and `select.ekey_enrolled_fingerprints`, have been removed. |
> | `button.ekey_check_orphaned_fingerprints` finds unmapped fingerprints | The panel's **Unassigned fingerprints** list, which can also assign them. Button removed. |
> | `button.ekey_person_fingerprints` prints the map into a notification | The panel's user list. Button removed. |
> | `button.ekey_led_off` | Never existed in the code. The LED states are 4 = green, 5 = red, 6 = red/green; there is no off. |
> | Six blueprints, including `door_unlock_on_match.yaml` | Two: `toggle_relay_on_granted.yaml` and `welcome_notification.yaml`. `door_unlock_on_match.yaml` was never in the repository. Scanner-side reactions belong on the backend now. |
> | LED feedback via `flash_led_on_*.yaml` blueprints | Built into the integration, and better done as an `led` action on the backend, where it survives a Home Assistant restart. |
>
> For the current design read [`README.md`](README.md) and
> [`custom_components/ekey_ha_app/README.md`](custom_components/ekey_ha_app/README.md);
> for the plan that produced it, the "one app layer" plan in `plans/`.

## Executive Summary

This document describes the design of a Home Assistant custom integration for the **ekey module fingerprint scanner** (OEM version). The integration provides real-time biometric access control, fingerprint management, and automation capabilities by connecting Home Assistant to an ekey module RS485 fingerprint scanner through a local daemon service.

---

## 1. System Architecture

### 1.1 Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    Home Assistant                            │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  ekey HA App Custom Integration                       │  │
│  │  ┌────────────┐ ┌────────────┐ ┌─────────────────┐   │  │
│  │  │ Config     │ │ Coordinator│ │  SSE Listener   │   │  │
│  │  │ Flow       │ │  (REST)    │ │  (Events)       │   │  │
│  │  └────────────┘ └────────────┘ └─────────────────┘   │  │
│  │  ┌────────────┐ ┌────────────┐ ┌─────────────────┐   │  │
│  │  │ Sensors    │ │ Buttons    │ │  Services       │   │  │
│  │  └────────────┘ └────────────┘ └─────────────────┘   │  │
│  └───────────────────────────────────────────────────────┘  │
└──────────────────┬──────────────────────────────────────────┘
                   │ HTTP REST + SSE (localhost:8080)
                   ▼
┌─────────────────────────────────────────────────────────────┐
│              ekey-ha-daemon (C daemon)                       │
│  • REST API server (HTTP/8080)                              │
│  • SSE event stream                                         │
│  • AES-128-GCM encryption/decryption                        │
│  • ECDH key exchange                                        │
└──────────────────┬──────────────────────────────────────────┘
                   │ RS485 Serial (proprietary binary protocol)
                   ▼
┌─────────────────────────────────────────────────────────────┐
│          ekey module Fingerprint Scanner                     │
│          (Hardware Device)                                   │
└─────────────────────────────────────────────────────────────┘
```

### 1.2 Component Responsibilities

| Component | Responsibility |
|-----------|---------------|
| **Config Flow** | User onboarding, daemon connection validation |
| **Coordinator** | Data fetching via REST API, device/fingerprint state management |
| **SSE Listener** | Real-time event stream processing (finger touch, matches, enrollment) |
| **Services** | Fingerprint enrollment/deletion, LED control operations |
| **Sensors** | Device info display, enrolled fingerprint count |
| **Buttons** | Quick LED controls, orphaned fingerprint checking |
| **Event Bus** | Propagate scanner events to automations |
| **Storage** | Persistent person-to-fingerprint mappings |

---

## 2. Core Functionality

### 2.1 Integration Setup & Configuration

#### Configuration Flow
- **Step 1**: User adds integration via UI (Settings → Integrations → Add Integration)
- **Step 2**: User provides daemon connection details:
  - **Host**: IP/hostname of daemon (default: `localhost`)
  - **Port**: Daemon HTTP port (default: `8080`)
- **Step 3**: Integration validates connection via `/api/v1/health` endpoint
- **Step 4**: Unique ID assigned as `{host}:{port}` (prevents duplicates)
- **Step 5**: Entry created, platforms loaded

#### Automatic Helper Creation
On first setup, the integration automatically creates:
- `select.ekey_person_selector` - Dropdown of all person entities
- `select.ekey_finger_selector` - Dropdown of fingers 1-10

These helpers simplify script/automation creation without manual typing.

#### Blueprint Installation
Integration includes 6 pre-built blueprints:
- **Script Blueprints**: `enroll_fingerprint.yaml`, `delete_fingerprint.yaml`
- **Automation Blueprints**: `flash_led_on_match.yaml`, `flash_led_on_fail.yaml`, `welcome_notification.yaml`, `door_unlock_on_match.yaml`

User notified via persistent notification with installation instructions.

---

### 2.2 Fingerprint Management

#### 2.2.1 Enrollment Process

**Service Call**: `ekey_ha_app.enroll_fingerprint`

**Parameters**:
- `person_id`: Home Assistant person entity (e.g., `person.john_doe`)
- `finger`: Integer 1-10 (finger number)

**Flow**:
1. Service generates unique APID (UUID v4) for the fingerprint
2. Maps APID to person and finger in temporary "pending enrollments" storage
3. Sends `POST /api/v1/fingerprints/enroll` with APID to daemon
4. Daemon initiates enrollment on scanner hardware
5. User places finger on scanner multiple times
6. SSE events stream enrollment state updates:
   - `wait` - Scanner ready, waiting for finger
   - `acquire` - Capturing fingerprint image
   - `step_done` - One capture complete, place finger again
   - `wait_for_confirmation` - All captures complete
   - `finished_success` - Enrollment successful
   - `finished_quitbyuser` - User cancelled
   - `finished_timeout` - Timeout exceeded
   - `finished_duplicate` - Fingerprint already enrolled
7. On success:
   - Move mapping from pending to permanent storage
   - Update persistent notification
   - Fire `ekey_enrollment_completed` event
8. On failure:
   - Remove from pending enrollments
   - Update notification with error

**Persistent Notification**:
- Real-time updates show enrollment progress
- Step-by-step instructions for user
- Final status (success/failure)

#### 2.2.2 Deletion Process

**Service Call**: `ekey_ha_app.delete_fingerprint`

**Parameters**:
- `person_id`: Person entity
- `finger`: Integer 1-10

**Flow**:
1. Service looks up APID from persistent storage
2. Sends `DELETE /api/v1/fingerprints/{apid}` to daemon
3. Daemon deletes from scanner hardware
4. Remove mapping from persistent storage on success
5. Refresh coordinator data

#### 2.2.3 Orphaned Fingerprint Detection

**Button Entity**: `button.ekey_check_orphaned_fingerprints`

**Purpose**: Find fingerprints enrolled directly on scanner (bypassing Home Assistant)

**Flow**:
1. Fetch all enrolled fingerprints from scanner via `/api/v1/fingerprints`
2. Compare scanner APIDs with stored person mappings
3. Identify APIDs with no associated person
4. Create persistent notification listing orphaned fingerprints
5. Provide guidance on deletion

#### 2.2.4 Person-to-Fingerprint Storage

**Storage Key**: `ekey_ha_app.person_fingerprints`
**Storage Version**: 1
**Format**:
```json
{
  "person.john_doe": {
    "fingerprints": {
      "1": "uuid-apid-finger-1",
      "3": "uuid-apid-finger-3"
    }
  },
  "person.jane_doe": {
    "fingerprints": {
      "2": "uuid-apid-finger-2"
    }
  }
}
```

---

### 2.3 Real-Time Event Processing

#### 2.3.1 SSE Event Stream

**Endpoint**: `GET /api/v1/events`
**Connection**: Long-lived HTTP connection with Server-Sent Events

**Event Types**:

| Event Command | HA Event Name | Description |
|---------------|---------------|-------------|
| `NOTIFY_FINGER_TOUCH` | `ekey_finger_touch` | Finger placed on scanner |
| `NOTIFY_MATCH` | `ekey_fingerprint_matched` | Fingerprint recognized |
| `NOTIFY_MATCH` (no match) | `ekey_fingerprint_not_matched` | Fingerprint not recognized |
| `NOTIFY_ENROLLMENT_STATE` | `ekey_enrollment_state` | Enrollment progress update |

#### 2.3.2 Match Event Processing

**Match Success Event** (`ekey_fingerprint_matched`):
```yaml
event_data:
  cmd: "NOTIFY_MATCH"
  result: 0  # MATCH_OK
  apid: "uuid-of-matched-fingerprint"
  finger: 3  # Mapped from person storage
  person_id: "person.john_doe"
  person_name: "John Doe"
```

**Match Failure Event** (`ekey_fingerprint_not_matched`):
```yaml
event_data:
  cmd: "NOTIFY_MATCH"
  result: 10  # MATCH_NOT_OK / MATCH_FAR / MATCH_NO_MATCH_BAD_QUALITY
  apid: null
```

**Match Results Mapping**:
- `0`: Match OK
- `10`: Match not OK
- `20`: FAR Match (False Acceptance Rate threshold)
- `30`: No Match, bad quality

#### 2.3.3 Connection Resilience

- **Auto-reconnect**: 5-second backoff on connection loss
- **Connection lost event**: `ekey_connection_lost` fired once per outage
- **Keep-alive**: SSE comments maintain connection health
- **Error recovery**: Automatic JSON cleaning for malformed responses

---

### 2.4 LED Control

#### 2.4.1 Button Entities

| Entity | Action | LED State |
|--------|--------|-----------|
| `button.ekey_led_green` | Press | Set LED green |
| `button.ekey_led_red` | Press | Set LED red |
| `button.ekey_led_off` | Press | Turn LED off |

**API Calls**:
- `POST /api/v1/led` with `{"State": 4}` (green) or `{"State": 5}` (red)
- `DELETE /api/v1/led` (off)

#### 2.4.2 LED Brightness Service

**Service Call**: `ekey_ha_app.set_led_brightness`

**Parameters**:
- `brightness`: Integer 0-100

**API Call**: `POST /api/v1/led/brightness` with `{"Brightness": 75}`

#### 2.4.3 LED Automation Use Cases

- **Visual feedback**: Flash green on successful match
- **Visual feedback**: Flash red on failed match
- **Status indication**: Red LED during enrollment
- **Night mode**: Reduce brightness at night

---

### 2.5 Sensor Entities

#### 2.5.1 Device Info Sensor

**Entity**: `sensor.ekey_scanner_info`

**State**: Software version (e.g., `v1.2.3`)

**Attributes**:
- `fw_api_version`: Firmware API version
- `sw_version`: Software version
- `prod_sn`: Product serial number
- `prod_sn_pcb`: PCB serial number
- `hw_version`: Hardware version
- `dev_typ`: Device type
- `dev_sub_typ`: Device sub-type
- `dev_line`: Device line (e.g., "dLine")
- `dev_variant`: Device variant
- `dev_sub_variant`: Device sub-variant

**Data Source**: `GET /api/v1/device`

#### 2.5.2 Enrolled Fingerprints Sensor

**Entity**: `sensor.ekey_enrolled_fingerprints`

**State**: Number of enrolled fingerprints (integer)

**Unit**: `fingerprints`

**Attributes**:
- `num_aps`: Total count
- `aps`: List of enrolled APIDs

**Data Source**: `GET /api/v1/fingerprints`

**Update Strategy**: On-demand refresh (no polling), triggered by:
- Integration reload
- Manual refresh
- Post-enrollment/deletion

---

## 3. Automation & Integration Features

### 3.1 Event-Driven Automations

#### 3.1.1 Door Unlock on Match

**Trigger**: `ekey_fingerprint_matched` event
**Condition**: Check person_id matches expected users
**Action**: Unlock smart lock

**Example**:
```yaml
trigger:
  platform: event
  event_type: ekey_fingerprint_matched
condition:
  condition: template
  value_template: "{{ trigger.event.data.person_id in ['person.john', 'person.jane'] }}"
action:
  service: lock.unlock
  target:
    entity_id: lock.front_door
```

#### 3.1.2 Welcome Notification

**Trigger**: `ekey_fingerprint_matched` event
**Action**: Send notification with person name and timestamp

#### 3.1.3 LED Feedback on Match/Fail

**Trigger**: `ekey_fingerprint_matched` or `ekey_fingerprint_not_matched`
**Action**: Flash LED green (success) or red (failure) for visual feedback

#### 3.1.4 Presence Detection

**Trigger**: `ekey_fingerprint_matched` event
**Action**: Set person's presence to "home"

---

### 3.2 Blueprint System

#### 3.2.1 Script Blueprints

**Enroll Fingerprint Blueprint** (`enroll_fingerprint.yaml`):
- Input: Person selector helper, finger selector helper
- Output: Calls `ekey_ha_app.enroll_fingerprint` service
- UI-friendly, no YAML editing required

**Delete Fingerprint Blueprint** (`delete_fingerprint.yaml`):
- Input: Person selector helper, finger selector helper
- Output: Calls `ekey_ha_app.delete_fingerprint` service

#### 3.2.2 Automation Blueprints

All blueprints use event triggers for flexible automation creation:
- `flash_led_on_match.yaml` - LED feedback on success
- `flash_led_on_fail.yaml` - LED feedback on failure
- `welcome_notification.yaml` - Notify on access
- `door_unlock_on_match.yaml` - Auto-unlock door

**Blueprint Installation Methods**:
1. Copy files to `config/blueprints/script/ekey/` or `config/blueprints/automation/ekey/`
2. Import via UI (paste YAML)
3. Run install script (`install_blueprints.sh` or `.ps1`)

---

### 3.3 Integration with HA Core Features

#### 3.3.1 Person Integration

- Leverages Home Assistant's `person` domain
- Maps fingerprints to existing person entities
- Person names automatically pulled from friendly names
- Supports multiple fingerprints per person (1-10)

#### 3.3.2 Device Registry

**Device Identifier**: `{host}:{port}` (e.g., `localhost:8080`)
**Device Name**: `ekey Scanner (localhost:8080)`
**Manufacturer**: ekey
**Model**: dLine
**Entities grouped under device**:
- 2 sensors
- 4 buttons

#### 3.3.3 Event Bus

All scanner events propagated to HA event bus for:
- Automation triggers
- Template sensors
- History tracking
- Logbook entries

#### 3.3.4 Persistent Notifications

Used for:
- Setup instructions (blueprint installation)
- Enrollment progress and results
- Orphaned fingerprint reports
- Error messages

---

## 4. Data Flow & State Management

### 4.1 Coordinator Pattern

**Update Interval**: `None` (on-demand only)

**Data Structure**:
```python
{
  "device": {
    "sw_version": "1.2.3",
    "prod_sn": "ABC123",
    # ... other device fields
  },
  "fingerprints": {
    "num_aps": 5,
    "aps": ["uuid1", "uuid2", ...]
  }
}
```

**Refresh Triggers**:
- Integration reload
- Manual refresh call
- Post-enrollment/deletion operations

**Error Handling**:
- Timeout tolerance: 15 seconds per API call
- Retry logic: Automatic SSE reconnection
- Fallback: Return empty data if fingerprints unavailable

### 4.2 Pending Enrollment Tracking

**Storage Location**: `hass.data[DOMAIN][entry_id]["pending_enrollments"]`

**Structure**:
```python
{
  "uuid-apid": {
    "person_id": "person.john_doe",
    "person_name": "John Doe",
    "finger": 3
  }
}
```

**Lifecycle**:
1. Created on `enroll_fingerprint` service call
2. Referenced during enrollment SSE events
3. Moved to permanent storage on success
4. Removed on failure/timeout

### 4.3 Persistent Storage

**File**: `.storage/ekey_ha_app.person_fingerprints`

**Operations**:
- **Load**: On integration startup
- **Save**: After enrollment/deletion
- **Format**: JSON with version key

**Migration Strategy**: `async_migrate_entry()` handles config version upgrades

---

## 5. Security & Privacy

### 5.1 Network Security

- **Local-only communication**: Daemon listens on `127.0.0.1` (loopback)
- **No external exposure**: No internet connectivity required
- **Encrypted scanner communication**: AES-128-GCM between daemon and hardware

### 5.2 Data Privacy

- **No biometric data stored**: Only APIDs (UUIDs) stored in HA
- **Fingerprint templates remain on scanner**: Never transmitted to HA
- **Person mappings local**: Stored in HA's `.storage/` directory
- **No cloud dependency**: Fully local operation

### 5.3 Authentication

- **No authentication on daemon**: Assumes trusted local network
- **Home Assistant authentication**: Standard HA user authentication applies

---

## 6. Error Handling & Resilience

### 6.1 Connection Failures

| Scenario | Behavior |
|----------|----------|
| Daemon offline during setup | Config flow shows "cannot_connect" error |
| Daemon offline after setup | SSE auto-reconnects every 5 seconds |
| Network timeout | 15-second timeout, graceful failure |
| Malformed JSON | Automatic control character cleaning, fallback parsing |

### 6.2 Enrollment Failures

| Failure Type | Handling |
|--------------|----------|
| Duplicate fingerprint | Notification with error, remove from pending |
| Timeout (60s) | Notification with timeout error, remove from pending |
| User quit | Notification acknowledging cancellation |
| Scanner busy | Retry guidance in notification |

### 6.3 API Error Handling

- **404 responses**: Log error, return empty data
- **504 Gateway Timeout**: Treat as scanner busy, non-fatal
- **JSON parse errors**: Clean control characters, retry parse
- **Unexpected exceptions**: Log with full traceback, surface to user

### 6.4 Logging Strategy

- **Debug logs**: API requests/responses, SSE events
- **Info logs**: Enrollment start/success, state changes
- **Warning logs**: Connection failures, timeouts
- **Error logs**: Critical failures, parse errors

---

## 7. Installation & Deployment

### 7.1 Prerequisites

**Hardware**:
- ekey module fingerprint scanner (OEM version)
- RS485 serial adapter (e.g., USB-RS485)
- Host machine (Raspberry Pi, x86 Linux, etc.)

**Software**:
- Home Assistant OS / Supervised / Container / Core
- ekey-ha-daemon running on same host or accessible network
- Python 3.11+ (provided by HA)

### 7.2 Installation Steps

1. **Install daemon**:
   ```bash
   cd ekey-ha-daemon/src
   make
   sudo make install
   sudo systemctl enable --now ekey-ha-daemon
   ```

2. **Install HA integration**:
   - Copy `ekey_ha_app/` to `config/custom_components/`
   - Restart Home Assistant

3. **Configure integration**:
   - Settings → Integrations → Add Integration
   - Search "ekey Home Assistant App"
   - Enter daemon host (default: `localhost`) and port (default: `8080`)

4. **Import blueprints** (optional):
   - Run `install_blueprints.sh` or `.ps1`
   - Or manually copy from `blueprints/` to `config/blueprints/`

### 7.3 Configuration Files

| File | Purpose |
|------|---------|
| `manifest.json` | Integration metadata (version, requirements, domain) |
| `config_flow.py` | UI configuration flow |
| `strings.json` | UI text translations |
| `services.yaml` | Service definitions for UI |
| `translations/en.json` | English translations |

---

## 8. API Reference (Daemon Interaction)

### 8.1 REST Endpoints Used

| Endpoint | Method | Purpose | Response |
|----------|--------|---------|----------|
| `/api/v1/health` | GET | Health check | `{"status": "ok"}` |
| `/api/v1/device` | GET | Get device info | Device details JSON |
| `/api/v1/fingerprints` | GET | List enrolled | `{"num_aps": 5, "aps": [...]}` |
| `/api/v1/fingerprints/enroll` | POST | Start enrollment | `{"status": "started"}` |
| `/api/v1/fingerprints/enroll/confirm` | POST | Confirm enrollment | `{"status": "confirmed"}` |
| `/api/v1/fingerprints/enroll/quit` | POST | Abort enrollment | `{"status": "quit"}` |
| `/api/v1/fingerprints/{apid}` | DELETE | Delete fingerprint | `{"status": "deleted"}` |
| `/api/v1/led` | POST | Set LED state | `{"status": "ok"}` |
| `/api/v1/led` | DELETE | End LED state | `{"status": "ok"}` |
| `/api/v1/led/brightness` | POST | Set brightness | `{"status": "ok"}` |
| `/api/v1/events` | GET | SSE stream | Event stream |

### 8.2 SSE Event Format

**Event Structure**:
```
data: {"cmd": "NOTIFY_MATCH", "result": 0, "apid": "uuid-here"}\n\n
```

**Keep-alive**:
```
: keep-alive comment\n\n
```

**Event Commands**:
- `NOTIFY_FINGER_TOUCH`
- `NOTIFY_MATCH`
- `NOTIFY_ENROLLMENT_STATE`

---

## 9. Testing Strategy

### 9.1 Unit Tests

**Test Files**:
- `tests/ha_component/test_coordinator.py` - Coordinator API calls

**Frameworks**:
- `pytest` for test execution
- `pytest-homeassistant-custom-component` for HA fixtures
- `aioresponses` for mocking HTTP responses

### 9.2 Integration Tests

**Scenarios**:
- Full enrollment flow (mock SSE events)
- Match event processing
- Connection failure recovery
- Config flow validation

### 9.3 Manual Testing Checklist

- [ ] Installation via UI
- [ ] Fingerprint enrollment (1-10 fingers)
- [ ] Fingerprint deletion
- [ ] LED control buttons
- [ ] Event triggers in automations
- [ ] Blueprint script execution
- [ ] Daemon connection loss/recovery
- [ ] Multiple persons with multiple fingers
- [ ] Orphaned fingerprint detection

---

## 10. Future Enhancements (Out of Scope)

### 10.1 Planned Features
- Multi-scanner support (multiple daemon instances)
- Advanced LED patterns (pulse, fade)
- Fingerprint template export/import
- Scanner log access UI
- Device firmware update via HA
- Enrollment quality metrics

### 10.2 Considered but Not Implemented
- Biometric authentication for HA users (privacy concerns)
- Cloud backup of fingerprints (security risk)
- Fingerprint matching in HA (requires template storage)

---

## 11. Technical Constraints & Limitations

### 11.1 Hardware Limitations
- **Max 99 fingerprints**: scanner hardware limit
- **Enrollment time**: ~15-30 seconds per fingerprint
- **Single concurrent operation**: Cannot enroll while matching

### 11.2 Software Limitations
- **Single daemon instance**: One scanner per daemon
- **Local network only**: No remote access built-in
- **No template backup**: Templates stored only on scanner hardware
- **Person entity dependency**: Requires pre-existing person entities

### 11.3 Performance Characteristics
- **SSE reconnection delay**: 5 seconds
- **API timeout**: 15 seconds per request
- **Enrollment timeout**: 60 seconds (scanner hardware limit)
- **Match latency**: <1 second (hardware dependent)

---

## 12. Glossary

| Term | Definition |
|------|------------|
| **APID** | Application ID - unique UUID identifying a fingerprint |
| **SSE** | Server-Sent Events - HTTP streaming protocol for real-time events |
| **dLine** | the value the scanner reports in its `dev_line` field. It is device-reported data, not a claim about which retail product this is — the module here is an OEM version |
| **Coordinator** | HA pattern for managing API data fetching |
| **Config Flow** | HA's UI-based integration configuration system |
| **Blueprint** | Reusable automation/script template in HA |
| **FAR** | False Acceptance Rate - biometric security threshold |
| **Orphaned Fingerprint** | Fingerprint enrolled without HA person mapping |

---

## 13. References

### 13.1 Documentation
- `README.md` - Integration overview and installation
- `QUICKSTART.md` - Step-by-step user guide
- `AUTOMATION_EXAMPLES.md` - Example automations and scripts
- `INSTALL_BLUEPRINTS.md` - Blueprint installation guide
- `API_DOCUMENTATION.md` - Daemon API reference (in daemon repo)

### 13.2 Code Files
- `__init__.py` - Integration entry point, setup logic
- `config_flow.py` - Configuration UI flow
- `coordinator.py` - Data update coordinator (REST API client)
- `sse_listener.py` - Server-Sent Events listener
- `services.py` - Service handlers (enroll, delete, LED)
- `sensor.py` - Sensor entity definitions
- `button.py` - Button entity definitions
- `const.py` - Constants and configuration keys
- `util.py` - Utility functions (JSON cleaning)

### 13.3 External Dependencies
- `aiohttp>=3.8.0` - Async HTTP client
- `homeassistant.core` - HA core framework
- `homeassistant.helpers` - HA helper utilities

---

## Appendix A: Complete Event Flow Examples

### A.1 Successful Fingerprint Match

```
1. User places finger on scanner
   └─> Hardware detects finger
   
2. SSE Event: NOTIFY_FINGER_TOUCH
   └─> HA fires `ekey_finger_touch` event
   
3. Scanner captures and matches fingerprint
   
4. SSE Event: NOTIFY_MATCH (result=0, apid="uuid-123")
   └─> Integration looks up APID in storage
   └─> Finds person.john_doe, finger 3
   └─> HA fires `ekey_fingerprint_matched` with person details
   
5. Automations trigger:
   └─> Flash LED green
   └─> Unlock front door
   └─> Send notification "Welcome home, John!"
```

### A.2 Failed Fingerprint Match

```
1. User places finger on scanner
   └─> SSE Event: NOTIFY_FINGER_TOUCH
   
2. Scanner captures fingerprint
   
3. SSE Event: NOTIFY_MATCH (result=10, apid=null)
   └─> HA fires `ekey_fingerprint_not_matched`
   
4. Automation triggers:
   └─> Flash LED red for 2 seconds
```

### A.3 Complete Enrollment Flow

```
1. User calls service: ekey_ha_app.enroll_fingerprint
   Data: {person_id: person.john_doe, finger: 3}
   
2. Integration generates APID: "uuid-abc-123"
   └─> Stores in pending_enrollments
   └─> POST /api/v1/fingerprints/enroll
   
3. SSE Event: NOTIFY_ENROLLMENT_STATE (state=10 "wait")
   └─> Notification: "Place finger on scanner..."
   
4. User places finger (1st time)
   └─> SSE Event: state=20 "acquire"
   └─> Notification: "Capturing..."
   
5. SSE Event: state=30 "step_done"
   └─> Notification: "Step 1/3 complete, place again..."
   
6. User places finger (2nd time)
   └─> SSE Event: state=20 "acquire"
   └─> SSE Event: state=30 "step_done"
   └─> Notification: "Step 2/3 complete..."
   
7. User places finger (3rd time)
   └─> SSE Event: state=20 "acquire"
   └─> SSE Event: state=30 "step_done"
   
8. SSE Event: state=35 "wait_for_confirmation"
   └─> Integration sends POST /api/v1/fingerprints/enroll/confirm
   
9. SSE Event: state=40 "finished_success"
   └─> Move APID from pending to permanent storage
   └─> Notification: "✅ Enrollment complete!"
   └─> Fire ekey_enrollment_completed event
```

---

## Document Information

**Version**: 1.0  
**Date**: 2026-06-02  
**Codebase Version**: v1.0.0  
**Integration Domain**: `ekey_ha_app`  
**Supported HA Version**: see `hacs.json` — 2024.7+  

> **This document is a snapshot, not the contract.** It was written before the app
> layer moved to the backend, so parts of it describe entities and script blueprints
> that no longer exist. `README.md` and `custom_components/ekey_ha_app/README.md` are
> the current descriptions; this is kept for the architectural reasoning behind them.

---

## Change Log

| Date | Version | Changes |
|------|---------|---------|
| 2026-06-02 | 1.0 | Initial design document created from code analysis |

