# Changelog

## 1.3.2

- **One version number with the backends.** The integration now carries the same number
  as the ekey ESP32 device, the Linux daemon and the Home Assistant add-on (it was 1.2.6),
  so "1.3.2" means one release everywhere. It is an alignment, not a skipped range of
  integration releases.
- **The add-on is set up without copying its token.** The ekey module Daemon add-on (1.3.2
  or later) announces itself and its API token through the Supervisor. With no entry for
  it yet, Settings → Devices & services shows **"ekey add-on found"** — press Submit, there
  is no token field. An existing entry for it — `127.0.0.1` or `localhost` — takes the
  token over and reloads, also whenever the token is regenerated on the add-on's System
  tab. ESP32 entries are never touched.
- **The ekey panel shows the selected scanner's own page.** Choosing a scanner in
  "Scanner or storage" now opens that scanner's pages — access log, users, actions,
  settings — inside the panel:
  - the **add-on** through ingress, signed in by Home Assistant; this replaces the add-on's
    separate "ekey daemon" sidebar entry;
  - an **ESP32 device** or a **daemon on another machine** by its own address, signed in
    with the token stored in the entry (it travels in the URL fragment, which never
    reaches a server log). Needs firmware/daemon 1.3.2 for the automatic sign-in; older
    ones show their login form.

  **"Users & persons in Home Assistant"** next to the picker switches to the previous
  view, where users are linked to Home Assistant persons. When a page cannot be shown —
  the add-on is stopped, there is no Supervisor, Home Assistant is on HTTPS and the
  scanner on plain HTTP, or a daemon at 127.0.0.1 that is not the add-on — a card says why
  and how to fix it. "Open in a new tab" stays available for a device whose certificate
  the browser has not accepted yet.
- **Fixed: a second scanner could fail to set up at Home Assistant start** with
  `ValueError: Overwriting panel ekey owned by custom`. Both entries are set up at the
  same time and both registered the shared panel; the registration is now claimed before
  the first await. The same race could register the logbook listeners twice.
- **Fingerprint reads always ask the scanner.** ekey 1.3.2 devices and daemons keep the
  fingerprint list in memory briefly to speed up their own pages; the integration sends
  `Cache-Control: no-cache` so the storage matrix, "missing on scanner" and the check
  after a transfer always see what the scanner holds.
- **Blueprints for Homematic.** *ekey - Homematic on access* forwards granted (or denied)
  access to a Homematic CCU — press a virtual key, set a system variable or set a device
  value — in two variants: the core Homematic integration and Homematic(IP) Local (HACS).
  See `blueprints/README.md`.

Tests: 340 Python, 248 panel.
