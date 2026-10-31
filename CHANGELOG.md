# Changelog

## 1.3.4

- **Fixed: a panel enrollment the scanner had abandoned kept saying "Reading the finger…"
  for two minutes.** When the backend loses a frame, it re-establishes the scanner session,
  and that ends the enrollment on the scanner with no closing state. Backends now send
  `NOTIFY_SESSION_RESET`. The SSE listener fires it on the bus as `ekey_session_reset` (with
  `entry_id`), and `EnrollManager` ends that scanner's running enrollment at once. If it had
  been confirmed, it checks the scanner's list first. (`const.py`, `sse_listener.py`,
  `enroll.py`, test in `test_capabilities_and_enroll.py`)

- **The copy dialog's title counts what it will copy.** It said "Copying 30 fingerprint(s)"
  when 20 of the 30 in the database were already on the scanner and the job wrote 10, so the
  title and the "Writing 8 of 10" line underneath disagreed. The title now says 10. When the
  job also assigns fingerprints that are already on the scanner, it says how many. (`jobs.py`)

- **Fixed: a copied fingerprint whose check failed was reported as "skipped" and never
  offered a retry.** After writing a template the backend reads the scanner's list back and
  answers `verified`. When that read-back itself failed (a finger on the sensor at that moment
  is enough) the reply had no `verified` at all. That "unknown" was treated like "the scanner
  refused it" — a permanent skip. A missing `verified` is now a retryable failure
  (`not_verified`), so **Retry the N that failed** picks it up. `verified: false` with the
  device's own verdict is still a skip. (`jobs.py`)
- **Fixed: a template on the scanner with nobody named for it was never assigned.** When the
  check after a write failed, the template was on the sensor but not in its user list. A push
  or a retry then left it out, because it was "already on the scanner". Deleting that user on
  the device later deleted only the fingerprints the list named, and this one stayed behind
  under "Unassigned fingerprints", still opening the door. A push now assigns such a
  fingerprint to its owner from the database in the same single user-list write, and does not
  write the template again. (`jobs.py`)

## 1.3.3

- **Copying fingerprints to a scanner is faster.** A push used to update the scanner's
  user list after every single template — read it, write it back, then re-read the list
  and the scanner's fingerprint list for Home Assistant's snapshot (the last one a full
  RS-485 round trip). Now every template for a scanner goes first, each still verified on
  its own, and that scanner's user list is read and written once, with one refresh: for
  N fingerprints N+4 requests instead of 5·N. If that one user-list write fails, every
  fingerprint of the batch is reported as "template only" (on the sensor, nobody named),
  as a single one was before; a cancelled push still assigns the templates it wrote.
  (`jobs.py`)
- **The ekey panel opens on "Fingerprint storage".** Whatever view was left last time, the
  tab now starts on the storage overview; the last scanner stays remembered for the
  scanner page. (`www/ekey-panel.js`)
- **Delete a user on every scanner.** The storage view's user rows have **"Delete on every
  scanner…"**: on each scanner every fingerprint of that person is deleted and the list
  re-read, and the user entry goes only once nothing of theirs is left there; the
  database records are removed last, and only when every scanner confirmed. A scanner
  whose list cannot be read is reported, never assumed empty. (`jobs.py`
  `async_purge_user`, websocket `ekey_ha_app/storage/purge_user`)
- **The storage view reads like the scanner page.** Users fold their finger rows (arrow
  per user, **Show all / Hide all**), the users card has the same button bar — Enroll
  fingerprint…, Refresh, Sync from a scanner…, Push missing — and **"Back to the scanner
  page"** returns to the last scanner. (`www/ekey-panel.js`)

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
