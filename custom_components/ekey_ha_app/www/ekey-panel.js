/*
 * ekey-panel — the ekey sidebar panel.
 *
 * Deliberately plain: a vanilla custom element with shadow DOM, no build step and
 * no import of Home Assistant's bundled `lit`. The frontend's internal module
 * paths are not a stable public interface, and a panel that breaks on a frontend
 * update is worse than a plainer one that keeps working. Everything it needs from
 * Home Assistant arrives through two documented surfaces: the `hass` property HA
 * sets on every custom panel, and `hass.callWS` / `hass.connection.subscribeMessage`.
 *
 * It holds no backend token and talks to no backend directly — every call goes to
 * the integration's websocket commands, which hold the token server-side. See
 * ws_api.py for why.
 *
 * The screens mirror the device's own admin page on purpose. Someone who enrols a
 * finger here and someone who does it on the device must get the same result and
 * recognise the same wording, so the awkward states (an occupied slot, a template
 * the sensor holds that no user claims, a delete the sensor would not confirm) are
 * surfaced the same way rather than hidden.
 */

const FINGER_COUNT = 10;

/* The virtual "Fingerprint storage" entry in the scanner dropdown. It is NOT an
   entry_id and never leaves the browser: `_mode` records which view is showing and
   `_entryId` always names a real scanner. Sending this to the backend would make
   `ws_subscribe` drop every event (its filter compares against real entry ids) and
   `users/get` answer "that scanner is not set up" — both silently. */
const STORAGE_ID = "__storage__";

/* Below this many scanners, never collapse the healthy chips: with three columns
   the whole row fits and collapsing hides information for no gain. At four and up,
   the healthy ones fold into one chip so that deviations stand out. */
const MATRIX_COLLAPSE_FROM = 4;

/* Untrusted lists — a restored backup's user list, a job's item log — are clipped
   before rendering. A file can claim anything. */
const MAX_PREVIEW_ROWS = 50;
const MAX_TEXT = 120;

/* Raw bytes per chunk, matching the backend's VAULT_CHUNK_BYTES. */
const CHUNK_BYTES = 256 * 1024;
const MAX_RESTORE_BYTES = 32 * 1024 * 1024;

const STYLE = `
  :host { display: block; }
  .wrap { max-width: 1000px; margin: 0 auto; padding: 16px; }
  .head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
  h1 { font-size: 1.5rem; margin: 0 0 4px; color: var(--primary-text-color); font-weight: 500; }
  .sub { color: var(--secondary-text-color); font-size: 0.9rem; margin: 0; }
  .card {
    background: var(--card-background-color, #fff);
    border-radius: var(--ha-card-border-radius, 12px);
    box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,.08));
    padding: 16px; margin: 16px 0; color: var(--primary-text-color);
  }
  h2 { font-size: 1.05rem; margin: 0 0 12px; font-weight: 500; }
  label { display: block; font-size: 0.8rem; color: var(--secondary-text-color); margin: 12px 0 4px; }
  input[type=text], select {
    width: 100%; box-sizing: border-box; padding: 8px 10px; font-size: 0.95rem;
    color: var(--primary-text-color); background: var(--secondary-background-color, #f5f5f5);
    border: 1px solid var(--divider-color, #e0e0e0); border-radius: 6px;
  }
  .row { display: flex; gap: 12px; flex-wrap: wrap; }
  .row > * { flex: 1 1 200px; }
  .bar { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; align-items: center; }
  button {
    font: inherit; font-size: 0.9rem; padding: 8px 16px; border-radius: 6px; cursor: pointer;
    border: none; background: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff);
  }
  button.ghost { background: transparent; color: var(--primary-color, #03a9f4); border: 1px solid var(--divider-color, #e0e0e0); }
  button.danger { background: var(--error-color, #db4437); color: #fff; }
  button.sm { padding: 4px 10px; font-size: 0.8rem; }
  button[disabled] { opacity: 0.5; cursor: not-allowed; }
  .item { border: 1px solid var(--divider-color, #e0e0e0); border-radius: 8px; padding: 12px; margin-bottom: 10px; }
  .item .top { display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; }
  .name { font-weight: 500; }
  .person { color: var(--secondary-text-color); font-size: 0.8rem; }
  .finger { display: flex; justify-content: space-between; align-items: center; gap: 8px;
            padding: 6px 0; border-top: 1px solid var(--divider-color, #e0e0e0); margin-top: 8px; flex-wrap: wrap; }
  .apid { color: var(--secondary-text-color); font-size: 0.72rem; font-family: monospace; word-break: break-all; }
  .badge { font-size: 0.7rem; padding: 2px 8px; border-radius: 999px; margin-left: 8px; white-space: nowrap; }
  .badge.ok { background: rgba(76,175,80,.16); color: var(--success-color, #4caf50); }
  .badge.warn { background: rgba(255,152,0,.16); color: var(--warning-color, #ff9800); }
  .hint { color: var(--secondary-text-color); font-size: 0.8rem; margin: 8px 0 0; line-height: 1.45; }
  .msg { margin-top: 12px; font-size: 0.88rem; padding: 10px 12px; border-radius: 6px; display: none; }
  .msg.show { display: block; }
  .msg.err { background: rgba(219,68,55,.12); color: var(--error-color, #db4437); }
  .msg.ok { background: rgba(76,175,80,.12); color: var(--success-color, #4caf50); }
  .msg.warn { background: rgba(255,152,0,.12); color: var(--warning-color, #ff9800); }
  .progress { font-size: 0.95rem; margin: 8px 0; }
  .muted { color: var(--secondary-text-color); }
  .empty { color: var(--secondary-text-color); font-size: 0.88rem; }

  /* Modal, matching the device's own admin page. Fixed rather than inline because the
     choice being made is about the scanner, not about the list behind it: an inline
     block pushes the page around and reads as one more section to scroll past.
     position:fixed inside a shadow root still resolves against the viewport — nothing
     up the tree sets transform/filter/perspective, which are what would turn an
     ancestor into the containing block instead. */
  .modal { position: fixed; inset: 0; background: rgba(0,0,0,.5); z-index: 10;
           display: flex; align-items: center; justify-content: center; padding: 16px; }
  .modal-box {
    background: var(--card-background-color, #fff);
    border-radius: var(--ha-card-border-radius, 12px);
    box-shadow: 0 18px 48px rgba(0,0,0,.45);
    padding: 20px; width: 100%; max-width: 420px; max-height: 90vh; overflow: auto;
    color: var(--primary-text-color);
  }
  .modal-box h2 { margin-top: 0; }
  /* The card the modal grew out of keeps its own margins; inside the box the first
     label would otherwise push the title away from it. */
  .modal-box label:first-of-type { margin-top: 0; }
  /* A per-item job report does not fit in a 420px box. */
  .modal-box.wide { max-width: 640px; }

  /* Inputs the panel never had before the storage view: a passphrase, a typed
     confirmation, a file to restore. */
  input[type=password] {
    width: 100%; box-sizing: border-box; padding: 8px 10px; font-size: 0.95rem;
    color: var(--primary-text-color); background: var(--secondary-background-color, #f5f5f5);
    border: 1px solid var(--divider-color, #e0e0e0); border-radius: 6px;
  }
  input[type=file] { width: 100%; box-sizing: border-box; font: inherit; font-size: 0.9rem; }
  .check { display: flex; gap: 8px; align-items: center; font-size: 0.85rem; margin: 12px 0 0; }

  /* The presence matrix. One chip per configured scanner: the row is the finger,
     the chips are the sensors. Colour is never the only signal — every chip carries
     a word — and the two states that must not be mistaken for each other (unknown,
     blocked) also differ in border. */
  .fname { flex: 1 1 200px; min-width: 0; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; justify-content: flex-end; }
  .chip { display: inline-flex; align-items: center; gap: 6px; max-width: 100%;
          font-size: 0.7rem; padding: 2px 8px; border-radius: 999px; white-space: nowrap;
          border: 1px solid transparent; }
  .chip-n { overflow: hidden; text-overflow: ellipsis; max-width: 11ch; }
  .chip-s { font-weight: 600; }
  .chip.ok      { background: rgba(76,175,80,.16); color: var(--success-color, #4caf50); }
  .chip.missing { background: rgba(255,152,0,.16); color: var(--warning-color, #ff9800); }
  .chip.extra   { background: rgba(3,169,244,.16); color: var(--info-color, #03a9f4); }
  .chip.unknown { background: var(--secondary-background-color, #f5f5f5);
                  color: var(--secondary-text-color); border-color: var(--divider-color, #e0e0e0); }
  .chip.blocked { background: rgba(219,68,55,.10); color: var(--error-color, #db4437);
                  border-color: rgba(219,68,55,.35); border-style: dashed; }
  .legend .chip { margin: 0 2px; }
  @media (max-width: 600px) { .chips { justify-content: flex-start; } }

  /* Storage mode is tinted, so a page showing the DATABASE cannot be mistaken at a
     glance for one showing a scanner. That confusion is this feature's main
     usability risk, so it is answered in four places at once — see _renderHead. */
  .storage .card { border-left: 4px solid var(--info-color, #03a9f4); }
  .banner { background: rgba(3,169,244,.08); }
  .tag { font-size: 0.7rem; vertical-align: middle; padding: 2px 8px; border-radius: 999px;
         background: rgba(3,169,244,.16); color: var(--info-color, #03a9f4); font-weight: 500; }

  .joblist { max-height: 40vh; overflow: auto; margin-top: 8px; }
  .track { height: 6px; border-radius: 999px; background: var(--divider-color, #e0e0e0);
           overflow: hidden; margin: 8px 0; }
  .track-fill { height: 100%; background: var(--primary-color, #03a9f4); transition: width .2s; }

  /* A live region has to persist to be announced, and _render() replaces the whole
     body every time — so this one lives OUTSIDE it, as a sibling. */
  .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden;
             clip: rect(0 0 0 0); white-space: nowrap; }
`;

function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* An APID is a UUID nobody reads in full. Show enough to match against the
   device's own page, which prints it the same way. */
function shortApid(apid) {
  const text = String(apid || "");
  return text.length > 16 ? `${text.slice(0, 8)}…${text.slice(-4)}` : text;
}

/* Clip BEFORE escaping, never after. Cutting an escaped string can slice
   "&quot;" into "&quot" — which a browser still resolves to a quote inside an
   attribute value, which is an injection. A restored backup file is untrusted
   input and its usernames land in title= and aria-label=, so this matters. */
function clip(value, max = MAX_TEXT) {
  const text = String(value == null ? "" : value);
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

function safe(value, max = MAX_TEXT) {
  return esc(clip(value, max));
}

/* Base64 in chunks: btoa(String.fromCharCode(...bytes)) blows the argument limit
   somewhere around 100k, and a template blob alone is 14 kB. */
function bytesToB64(bytes) {
  let out = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    out += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(out);
}

function b64ToBytes(text) {
  const raw = atob(text);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return bytes;
}

function humanBytes(count) {
  const n = Number(count) || 0;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} kB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

class EkeyPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._entryId = null;
    this._scanners = [];
    this._persons = [];
    this._data = null;         // users/get result for the selected scanner
    this._enroll = null;       // live enrollment status
    this._enrollOpen = false;  // the picker dialog is showing
    this._enrollUser = null;   // user_id chosen in the picker
    this._enrollStarting = false;  // the start request is in flight
    this._editing = null;      // user_id being edited
    this._message = null;      // { text, kind }
    this._loading = false;
    this._unsub = null;
    this._booted = false;

    /* Which view is showing. `_entryId` keeps naming a real scanner in both modes —
       see STORAGE_ID for why the sentinel must not go anywhere near the backend. */
    this._mode = "scanner";       // "scanner" | "storage"
    /* storage/get's reply. Kept OUT of `_data`: every existing render function and
       every existing test reads `_data` with the users/get shape, and putting a
       different shape there would silently reinterpret `missing` and `unassigned`. */
    this._storage = null;
    this._dialog = null;          // { kind, …, busy } — at most one open
    this._job = null;             // live or last-finished job status
    this._stale = false;          // a change arrived while a dialog was open
    this._formCache = {};         // id -> { value, start, end }
    this._lastDialogKey = null;   // so focus is taken once, not every render
    this._live = null;            // the persistent aria-live node
  }

  /* HA assigns this on every render of the panel, so it must stay cheap: only
     the first assignment bootstraps. */
  set hass(hass) {
    this._hass = hass;
    if (!this._booted && hass) {
      this._booted = true;
      this._boot();
    }
  }

  get hass() {
    return this._hass;
  }

  connectedCallback() {
    this._renderShell();
    /* On document, not on the panel: a modal that has taken over the screen should
       answer Escape wherever the focus happens to be, and a shadow root only receives
       the key event when something inside it is focused. Registered once here and
       removed on disconnect, so navigating away from the panel does not leave a
       listener behind that still holds this element alive. */
    document.addEventListener("keydown", this._onKeyDown);
  }

  disconnectedCallback() {
    document.removeEventListener("keydown", this._onKeyDown);
    this._teardown();
  }

  /* Escape closes the picker, and deliberately does NOT abandon a live enrolment: the
     scanner is mid-session and waiting for a finger, so leaving it needs the button that
     says so. Bound in the constructor because add/removeEventListener must be handed the
     same function object. */
  _onKeyDown = (ev) => {
    if (ev.key !== "Escape") return;
    /* One document-level listener for the whole panel — adding a second for the
       storage dialogs is how a panel ends up outliving its page. */
    if (this._dialog || this._job) {
      this._closeDialog();
      return;
    }
    this._closeEnroll();
  };

  /* Ignored while a start request is in flight or an enrolment is live: the scanner is
     mid-session and waiting for a finger, so leaving it needs the button that says so. */
  _closeEnroll() {
    if (!this._enrollOpen || this._enrollStarting) return;
    if (this._enroll && !this._enroll.done) return;
    this._enrollOpen = false;
    this._render();
  }

  _teardown() {
    if (this._unsub) {
      const unsub = this._unsub;
      this._unsub = null;
      Promise.resolve(unsub).then((fn) => { if (typeof fn === "function") fn(); }).catch(() => {});
    }
  }

  // ------------------------------------------------------------------ plumbing

  async _ws(message) {
    return this._hass.callWS(message);
  }

  _say(text, kind) {
    this._message = text ? { text, kind: kind || "ok" } : null;
    this._render();
  }

  async _boot() {
    this._renderShell();
    try {
      const [scanners, persons] = await Promise.all([
        this._ws({ type: "ekey_ha_app/scanners" }),
        this._ws({ type: "ekey_ha_app/persons" }),
      ]);
      this._scanners = (scanners && scanners.scanners) || [];
      this._persons = (persons && persons.persons) || [];
      const loaded = this._scanners.find((s) => s.loaded);
      this._entryId = (loaded || this._scanners[0] || {}).entry_id || null;
    } catch (err) {
      this._say(`Could not read the scanner list: ${err.message || err}`, "err");
      return;
    }
    await this._subscribe();
    await this._load();
  }

  async _subscribe() {
    this._teardown();
    if (this._mode !== "storage" && !this._entryId) return;
    try {
      /* Storage mode subscribes with NO entry_id: any scanner's user list is a
         column of the matrix, so a change on any of them is a change to this view.
         Scanner mode stays scoped, as before. Job events carry entry_id: null,
         which the server-side filter lets through either way. */
      const message = { type: "ekey_ha_app/subscribe" };
      if (this._mode !== "storage") message.entry_id = this._entryId;
      this._unsub = this._hass.connection.subscribeMessage(
        (msg) => this._onEvent(msg),
        message,
      );
    } catch (err) {
      // Live updates are a convenience; the panel still works by reloading.
      this._say(
        "Live updates are unavailable — the list will refresh when you act on it.",
        "warn",
      );
    }
  }

  _onEvent(msg) {
    const type = msg && msg.event_type;
    const data = (msg && msg.data) || {};
    if (type === "ekey_enroll_progress") {
      this._enroll = data;
      if (data.done) {
        this._say(data.message, data.ok ? "ok" : "err");
        this._load();
        return;
      }
      this._render();
      return;
    }
    if (type === "ekey_storage_job") {
      this._job = data;
      if (data.done) {
        this._say(data.message, data.ok ? "ok" : "warn");
        this._announce(data.message);
        /* The database changed, so the matrix has to be re-read. The job dialog
           stays open on its report — closing it is the operator's move. */
        this._load();
        return;
      }
      this._announce(data.message);
      this._render();
      return;
    }
    if (type === "ekey_users_changed" || type === "ekey_storage_changed") {
      /* Deferred, not dropped. A reload re-renders, and a re-render replaces the
         whole body — which would wipe a passphrase somebody is halfway through
         typing because another tab (or the five-minute poll) fired an event. */
      if (this._dialog || (this._job && !this._job.done)) {
        this._stale = true;
        return;
      }
      this._load();
      return;
    }
    if (type === "ekey_connection_lost") {
      this._say("The scanner connection was lost. Retrying…", "warn");
    }
  }

  /* Announced to a screen reader without touching the page. The node lives outside
     `_body` because a live region has to persist to be read out at all. */
  _announce(text) {
    if (this._live && text) this._live.textContent = String(text);
  }

  /* A two-line dispatcher, so every existing caller — _onEvent, the Refresh
     button, every action — keeps working unchanged and picks up the right source. */
  async _load() {
    return this._mode === "storage" ? this._loadStorage() : this._loadScanner();
  }

  async _loadScanner() {
    if (!this._entryId) {
      this._data = null;
      this._render();
      return;
    }
    this._loading = true;
    this._render();
    try {
      this._data = await this._ws({
        type: "ekey_ha_app/users/get",
        entry_id: this._entryId,
      });
      this._enroll = this._data.enroll || null;
    } catch (err) {
      this._data = null;
      this._say(`Could not read users: ${err.message || err}`, "err");
    }
    this._loading = false;
    this._render();
  }

  async _loadStorage() {
    this._loading = true;
    this._render();
    try {
      this._storage = await this._ws({ type: "ekey_ha_app/storage/get" });
      /* A job that was already running when this page loaded — adopt it rather
         than leaving a minutes-long operation invisible. */
      if (this._storage.job && !this._job) this._job = this._storage.job;
    } catch (err) {
      this._storage = null;
      this._say(`Could not read the fingerprint database: ${err.message || err}`, "err");
    }
    this._loading = false;
    this._render();
  }

  // -------------------------------------------------------------------- render

  _renderShell() {
    if (this.shadowRoot.childElementCount) return;
    const style = document.createElement("style");
    style.textContent = STYLE;
    const body = document.createElement("div");
    body.className = "wrap";
    /* A sibling of `body`, not a child: _render() replaces body.innerHTML wholesale
       and a live region that gets replaced is never announced. */
    const live = document.createElement("div");
    live.className = "sr-only";
    if (live.setAttribute) {
      live.setAttribute("role", "status");
      live.setAttribute("aria-live", "polite");
    }
    this.shadowRoot.append(style, body, live);
    this._body = body;
    this._live = live;
    this._render();
  }

  _render() {
    if (!this._body) return;
    const parts = [this._renderHead()];

    const scanner = this._scanners.find((s) => s.entry_id === this._entryId);
    let modal = "";
    if (!this._scanners.length) {
      parts.push(`<div class="card"><p class="empty">No ekey scanner is configured yet.
        Add one under Settings → Devices &amp; services.</p></div>`);
    } else if (this._mode === "storage") {
      /* Before the not-loaded and no-app-layer branches on purpose: those describe
         the last-selected SCANNER, and letting either of them win would replace the
         database view with a card about something else entirely. */
      if (this._storage) {
        parts.push(this._renderStorageBanner());
        parts.push(this._renderStorageTools());
        parts.push(this._renderStorageUsers());
        parts.push(this._renderStorageExtras());
      } else if (this._loading) {
        parts.push('<div class="card"><p class="empty">Reading the database…</p></div>');
      }
      modal = this._renderStorageModal();
    } else if (scanner && !scanner.loaded) {
      /* The serial port is named here on purpose: a wrong port is one of the two reasons
         a backend is unreachable (the other is the host/token), and this card is where
         someone lands when it is. It says where the setting lives, since it is no longer
         on this page. */
      parts.push(`<div class="card"><p class="empty">This scanner is not loaded — it may be
        starting up or unreachable. Check its address, token and serial port under
        Settings → Devices &amp; services → ekey module App → <b>Configure</b>.</p></div>`);
    } else if (this._data && this._data.app_api === false) {
      parts.push(this._renderNoAppApi(scanner));
    } else if (this._data) {
      parts.push(this._renderAddUser());
      parts.push(this._renderUsers());
      parts.push(this._renderUnassigned());
      modal = this._renderEnroll() || this._renderScannerModal();
    } else if (this._loading) {
      parts.push('<div class="card"><p class="empty">Loading…</p></div>');
    }

    /* A running job belongs to the integration, not to one view: it stays on screen
       when the dropdown changes, because losing sight of a minutes-long transfer is
       how someone starts a second one. */
    if (!modal) modal = this._renderJob();

    /* The modal carries its own copy of the message, so showing it here as well would
       print the same sentence twice, one of them behind the overlay. */
    parts.push(modal ? "" : this._renderMessage());
    /* Last, so the overlay paints over the page rather than relying on z-index alone
       to beat a card that comes after it. */
    parts.push(modal);
    /* One class carries the whole storage tint — see the .storage rules in STYLE. */
    this._body.className = this._mode === "storage" ? "wrap storage" : "wrap";
    this._snapshotForm();
    this._body.innerHTML = parts.join("");
    this._wire();
    this._restoreForm();
    this._focusDialog();
  }

  /* Text the operator is typing survives a re-render. Deliberately a short, named
     list rather than "every input": every SELECT is re-rendered with the right
     option already chosen, and restoring those would fight the render instead of
     helping it. */
  _snapshotForm() {
    const root = this.shadowRoot;
    this._formCache = {};
    for (const id of ["bk-pass", "bk-pass2", "rs-pass", "cl-word"]) {
      const node = root.getElementById && root.getElementById(id);
      if (node && node.value) {
        this._formCache[id] = {
          value: node.value,
          start: node.selectionStart,
          end: node.selectionEnd,
        };
      }
    }
  }

  _restoreForm() {
    const root = this.shadowRoot;
    for (const [id, saved] of Object.entries(this._formCache || {})) {
      const node = root.getElementById && root.getElementById(id);
      if (!node) continue;
      node.value = saved.value;
      /* Put the caret back too, or a re-render mid-word jumps it to the end. */
      if (saved.start != null && node.setSelectionRange) {
        try {
          node.setSelectionRange(saved.start, saved.end);
        } catch (err) {
          /* Some input types refuse selection ranges; not worth failing over. */
        }
      }
    }
  }

  /* Focus is taken when a dialog APPEARS, not on every render — a streaming job
     re-renders every few seconds, and re-focusing each time would take the caret
     away from anyone using a keyboard. */
  _focusDialog() {
    const key = `${(this._dialog || {}).kind || ""}|${(this._job || {}).job_id || ""}|${
      (this._job || {}).done ? "1" : "0"}`;
    if (key === this._lastDialogKey) return;
    this._lastDialogKey = key;
    if (!this._dialog && !this._job) return;
    const root = this.shadowRoot;
    const box = root.getElementById && (root.getElementById("dlg") || root.getElementById("job-modal"));
    if (box && box.querySelector) {
      const first = box.querySelector("input, select, button");
      if (first && first.focus) first.focus();
    }
  }

  _renderHead() {
    const storage = this._mode === "storage";
    const busy = this._jobRunning();
    /* Shown from ONE scanner upwards now, not two. The list is scanners + the
       database, so it always has at least two entries — and a single-scanner
       install is precisely the one that most needs the database, since that
       scanner is then the only copy of everyone's fingerprints. */
    const picker = this._scanners.length >= 1
      ? `<div style="min-width:220px">
           <label for="scanner">Scanner or storage</label>
           <select id="scanner"${busy ? " disabled" : ""}>
             <optgroup label="Scanners">${this._scanners.map((s) => `
               <option value="${esc(s.entry_id)}"${
                 !storage && s.entry_id === this._entryId ? " selected" : ""}>
                 ${safe(s.title)}${s.loaded ? "" : " (not loaded)"}
               </option>`).join("")}
             </optgroup>
             <optgroup label="Home Assistant">
               <option value="${STORAGE_ID}"${storage ? " selected" : ""}>Fingerprint storage</option>
             </optgroup>
           </select>
         </div>`
      : "";
    /* Which build is actually loaded. The integration is installed by copying files
       onto a Home Assistant host, so a stale copy behaves exactly like a fix that was
       never made — this makes the difference visible without opening devtools. */
    const version = (this.panel && this.panel.config && this.panel.config.version) || "";
    /* The title is the first of four signals that say which side you are looking at
       — the others are the tag, the banner card, and the tint on every card. A page
       that looks the same in both modes is this feature's main usability risk. */
    const heading = storage
      ? `<h1>Fingerprint storage <span class="tag">Home Assistant</span></h1>
         <p class="sub">Home Assistant's own copy of every fingerprint template. Nothing here
           opens a door by itself — a scanner does that. Use it to repair a scanner, add a new
           one, or recover after a factory reset.</p>`
      : `<h1>ekey users &amp; fingerprints</h1>
         <p class="sub">Managed on the scanner itself — Home Assistant is the front-end.
           Recognitions keep working when Home Assistant is down.</p>`;
    return `<div class="head">
        <div>
          ${heading}
          ${version ? `<p class="sub muted">Integration ${esc(version)}</p>` : ""}
        </div>
        ${picker}
      </div>`;
  }

  _jobRunning() {
    return !!(this._job && !this._job.done);
  }

  _renderNoAppApi(scanner) {
    const caps = (this._data && this._data.capabilities) || {};
    const why = caps.source === "unavailable"
      ? "The scanner could not be reached just now, so its capabilities are unknown."
      : "This backend does not serve the app layer (<code>/app/v1</code>).";
    return `<div class="card">
        <h2>User management is not available for this scanner</h2>
        <p class="hint">${why}</p>
        <p class="hint">User records, actions and automations live on the backend. An ekey ESP32
          device provides them; the Linux daemon provides them once it has been updated to a
          version with the app layer. Until then the scanner half of this integration works
          normally — entities, events and the enrolment service are unaffected.</p>
        <div class="bar"><button class="ghost" id="reload">Check again</button></div>
      </div>`;
  }

  _renderAddUser() {
    if (this._enroll && !this._enroll.done) return "";
    return `<div class="card">
        <h2>Add user</h2>
        <div class="row">
          <div>
            <label for="new-name">Name</label>
            <input type="text" id="new-name" placeholder="e.g. Jane" autocomplete="off">
          </div>
          <div>
            <label for="new-person">Link to a Home Assistant person (optional)</label>
            ${this._personSelect("new-person", null)}
          </div>
        </div>
        <p class="hint">Linking a person is what lets automations and the logbook say who opened
          the door. One person can be linked to one user per scanner.</p>
        <div class="bar"><button id="add-user">Add user</button></div>
      </div>`;
  }

  _personSelect(id, selected) {
    const options = [`<option value="">— not linked —</option>`].concat(
      this._persons.map((p) => `<option value="${esc(p.entity_id)}"${
        p.entity_id === selected ? " selected" : ""}>${esc(p.name)}</option>`),
    );
    return `<select id="${id}">${options.join("")}</select>`;
  }

  _renderUsers() {
    const users = (this._data.users || []).slice();
    users.sort((a, b) => String(a.username || "").localeCompare(String(b.username || "")));
    const missing = new Set(this._data.missing || []);
    const busy = this._enroll && !this._enroll.done;

    const rows = users.map((user) => {
      if (this._editing === user.id) return this._renderUserEditor(user);
      const fingers = (user.fingers || []).slice().sort((a, b) => (a.finger || 0) - (b.finger || 0));
      const person = user.ha_person
        ? `<div class="person">${esc(this._personName(user.ha_person))}</div>`
        : `<div class="person muted">not linked to a person</div>`;
      const fingerHtml = fingers.length
        ? fingers.map((f) => {
            const badge = !this._data.scanner_list_known
              ? ""
              : missing.has(f.apid)
                ? '<span class="badge warn">missing on scanner</span>'
                : '<span class="badge ok">on scanner</span>';
            return `<div class="finger">
                <div>Finger ${esc(f.finger)}${badge}
                  <div class="apid">${esc(shortApid(f.apid))}</div>
                </div>
              </div>`;
          }).join("")
        : '<p class="hint">No fingerprints yet.</p>';
      return `<div class="item">
          <div class="top">
            <div><span class="name">${esc(user.username)}</span>${person}</div>
            <div><button class="sm ghost" data-edit="${esc(user.id)}"${busy ? " disabled" : ""}>Edit</button></div>
          </div>
          ${fingerHtml}
        </div>`;
    });

    return `<div class="card">
        <h2>Users &amp; fingerprints</h2>
        <div class="bar">
          <button id="start-enroll"${busy || !users.length ? " disabled" : ""}>Enroll fingerprint…</button>
          <button class="ghost" id="reload">Refresh</button>
          <button class="ghost" id="sync-to-storage"${
            busy || !this._data.scanner_list_known ? " disabled" : ""}
            title="${this._data.scanner_list_known
              ? "Copy this scanner's fingerprints into Home Assistant's database"
              : "This scanner's fingerprint list could not be read just now, so there is nothing to copy."}"
            >Sync to storage…</button>
          <button class="sm ghost" id="goto-storage">Open fingerprint storage</button>
        </div>
        ${users.length ? "" : '<p class="hint">No users yet — add one above, then enroll a finger.</p>'}
        <p class="hint">The database keeps a copy of each template, so this scanner can be
          repaired or replaced without asking anyone to enroll again.</p>
        ${rows.join("")}
        ${this._data.scanner_list_known ? "" : `<p class="hint">The scanner's stored list could not
          be read just now, so the on-scanner badges are hidden rather than guessed.</p>`}
      </div>`;
  }

  _renderUserEditor(user) {
    const fingers = (user.fingers || []).slice().sort((a, b) => (a.finger || 0) - (b.finger || 0));
    const fingerHtml = fingers.map((f) => `
      <div class="finger">
        <div>Finger ${esc(f.finger)}<div class="apid">${esc(shortApid(f.apid))}</div></div>
        <div><button class="sm danger" data-delfp="${esc(f.apid)}">Delete fingerprint</button></div>
      </div>`).join("") || '<p class="hint">No fingerprints to remove.</p>';

    return `<div class="item">
        <div class="row">
          <div>
            <label for="edit-name">Name</label>
            <input type="text" id="edit-name" value="${esc(user.username)}" autocomplete="off">
          </div>
          <div>
            <label for="edit-person">Linked person</label>
            ${this._personSelect("edit-person", user.ha_person || null)}
          </div>
        </div>
        ${fingerHtml}
        <p class="hint">Deleting acts immediately and is confirmed by the scanner: if the sensor
          still holds the fingerprint it is kept in this list, because it still opens the door.</p>
        <div class="bar">
          <button data-save="${esc(user.id)}">Save</button>
          <button class="ghost" id="cancel-edit">Cancel</button>
          <button class="danger" data-deluser="${esc(user.id)}">Delete user</button>
        </div>
      </div>`;
  }

  _renderUnassigned() {
    const orphans = this._data.unassigned || [];
    if (!orphans.length) return "";
    const users = this._data.users || [];
    const rows = orphans.map((apid) => `
      <div class="finger">
        <div class="apid">${esc(apid)}</div>
        <div class="bar" style="margin:0">
          <select data-assign-user="${esc(apid)}">
            ${users.map((u) => `<option value="${esc(u.id)}">${esc(u.username)}</option>`).join("")}
          </select>
          <select data-assign-finger="${esc(apid)}">
            ${Array.from({ length: FINGER_COUNT }, (_, i) =>
              `<option value="${i + 1}">Finger ${i + 1}</option>`).join("")}
          </select>
          <button class="sm" data-assign="${esc(apid)}"${users.length ? "" : " disabled"}>Assign</button>
          <button class="sm danger" data-delfp="${esc(apid)}">Delete</button>
        </div>
      </div>`).join("");

    return `<div class="card">
        <h2>Unassigned fingerprints</h2>
        <p class="hint">The scanner holds ${orphans.length} fingerprint(s) that belong to no user —
          usually an enrolment that finished after a page gave up, or a delete that failed. They
          already work; all that is missing is who they belong to. <b>Assign</b> them rather than
          re-enrolling: nothing is sent to the sensor and nobody has to present a finger again.</p>
        ${rows}
      </div>`;
  }

  /* No "Scanner connection" card here any more: which serial port the scanner is wired
     to is a connection setting and lives in the config entry's Configure dialog, beside
     the host and token that reach the same backend — Settings → Devices & services →
     ekey module App → Configure. This page is about users and their fingers. */

  // ------------------------------------------------------------ storage view

  _renderStorageBanner() {
    const count = (this._storage.scanners || []).length;
    return `<div class="card banner" role="note">
        <p class="sub"><b>You are looking at the database, not a scanner.</b> These records live
          in Home Assistant. The chips on the right of each finger say what your ${count}
          scanner${count === 1 ? "" : "s"} actually hold — that is the only place on this page
          where a sensor is described.</p>
      </div>`;
  }

  _renderStorageTools() {
    const s = this._storage;
    const busy = this._jobRunning();
    const changed = s.changed
      ? new Date(s.changed * 1000).toLocaleString()
      : "never";
    return `<div class="card">
        <h2>Storage tools</h2>
        <div class="bar">
          <button id="storage-sync"${busy ? " disabled" : ""}>Sync from a scanner…</button>
          <button class="ghost" id="storage-backup"${busy ? " disabled" : ""}>Create backup…</button>
          <button class="ghost" id="storage-restore"${busy ? " disabled" : ""}>Restore backup…</button>
          <button class="ghost" id="reload">Refresh</button>
          <button class="danger" id="storage-clean"${busy || !s.record_count ? " disabled" : ""}>Clean storage…</button>
        </div>
        <p class="hint">${s.record_count} fingerprint(s) for ${s.user_count} user(s),
          ${humanBytes(s.bytes)}. Last change ${esc(changed)}.</p>
      </div>`;
  }

  /* Where a fingerprint stands on one scanner. Five states, and the order of these
     checks is the whole "never guess" rule in one place:

       unknown  the scanner is not loaded, or its list could not be read. NOT missing.
       ok       the sensor holds it.
       blocked  it can never be copied there — a different device variant, or a
                backend with no template routes. Permanent, so never retried.
       missing  the database has it and that sensor does not. Fixable, by a push. */
  _cellState(record, scanner) {
    if (!scanner.loaded || !scanner.list_known) return "unknown";
    if ((scanner.on_scanner || []).indexOf(record.apid) >= 0) return "ok";
    if (scanner.template_api === false) return "blocked";
    if (record.dev_variant != null && scanner.dev_variant != null
        && record.dev_variant !== scanner.dev_variant) return "blocked";
    return "missing";
  }

  _chip(state, name, title) {
    const words = { ok: "ok", missing: "missing", extra: "extra", unknown: "?", blocked: "n/a" };
    return `<span class="chip ${state}" title="${safe(title)}" aria-label="${safe(title)}">
        ${name ? `<span class="chip-n">${safe(name, 40)}</span>` : ""}
        <span class="chip-s">${words[state]}</span>
      </span>`;
  }

  /* Deviations first, then the healthy ones — and past MATRIX_COLLAPSE_FROM the
     healthy ones fold into a single chip. That is what keeps an eight-scanner row
     readable: the eye lands on the problem instead of on a wall of green, and the
     collapsed names survive in the tooltip. */
  _matrixChips(record) {
    const scanners = this._storage.scanners || [];
    const groups = { missing: [], blocked: [], unknown: [], ok: [] };
    for (const scanner of scanners) {
      groups[this._cellState(record, scanner)].push(scanner);
    }

    const reason = {
      missing: (n) => `${n} — the database has this fingerprint, that scanner does not`,
      blocked: (n) => `${n} — a different device variant, or no template support: this ` +
        `fingerprint can never be copied there`,
      unknown: (n) => `${n} — that scanner's list could not be read. Nothing is assumed.`,
      ok: (n) => `${n} — stored on that scanner`,
    };

    const out = [];
    for (const state of ["missing", "blocked", "unknown"]) {
      for (const scanner of groups[state]) {
        out.push(this._chip(state, scanner.title, reason[state](scanner.title)));
      }
    }
    const okNames = groups.ok.map((s) => s.title);
    if (okNames.length >= 2 && scanners.length >= MATRIX_COLLAPSE_FROM) {
      const all = okNames.length === scanners.length;
      out.push(`<span class="chip ok" title="${safe(okNames.join(", "), 400)}"
          aria-label="${all ? `stored on all ${okNames.length} scanners`
            : `ok on ${okNames.length} scanners`}">
          <span class="chip-s">${all ? `on all ${okNames.length} scanners` : `ok on ${okNames.length}`}</span>
        </span>`);
    } else {
      for (const scanner of groups.ok) {
        out.push(this._chip("ok", scanner.title, reason.ok(scanner.title)));
      }
    }
    return { html: out.join(""), missing: groups.missing.length };
  }

  _renderLegend() {
    return `<p class="hint legend">
        ${this._chip("ok", "", "on that scanner")} on that scanner ·
        ${this._chip("missing", "", "in the database, not on that scanner")} in the database,
        not on that scanner ·
        ${this._chip("extra", "", "on that scanner, not in the database")} on that scanner,
        not in the database ·
        ${this._chip("unknown", "", "that scanner's list could not be read")} that scanner's
        list could not be read — <b>not</b> the same as missing ·
        ${this._chip("blocked", "", "a different device variant")} a different device variant —
        a template can never be copied there
      </p>`;
  }

  _renderStorageUsers() {
    const s = this._storage;
    const busy = this._jobRunning();
    if (!s.users.length) {
      return `<div class="card">
          <h2>Fingerprints in the database</h2>
          <p class="empty">The database is empty. Use <b>Sync from a scanner…</b> to copy the
            fingerprints your scanners already hold into Home Assistant — nothing is written to
            any scanner and nobody has to present a finger.</p>
        </div>`;
    }

    let drift = 0;
    const rows = s.users.map((user) => {
      const fingers = user.fingers.map((finger) => {
        const chips = this._matrixChips(finger);
        drift += chips.missing;
        const badge = finger.has_template === false
          ? '<span class="badge warn">no template stored</span>'
          : '<span class="badge ok">on database</span>';
        const superseded = finger.superseded_by
          ? `<div class="person muted">Replaced by a newer enrolment — this template still
             works wherever it is stored.</div>`
          : "";
        return `<div class="finger">
            <div class="fname">Finger ${esc(finger.finger)}${badge}
              <div class="apid">${esc(shortApid(finger.apid))}</div>
              ${superseded}
            </div>
            <div class="chips" role="group"
                 aria-label="${safe(user.username)} finger ${esc(finger.finger)} — presence on each scanner">
              ${chips.html}
            </div>
            <div class="bar" style="margin:0">
              ${chips.missing && finger.has_template !== false
                ? `<button class="sm ghost" data-push="${esc(finger.apid)}"${busy ? " disabled" : ""}
                     >Push to ${chips.missing} scanner${chips.missing === 1 ? "" : "s"}…</button>`
                : ""}
            </div>
          </div>`;
      }).join("");

      return `<div class="item">
          <div class="top">
            <div>
              <div class="name">${safe(user.username)}</div>
              ${user.ha_person
                ? `<div class="person">${safe(user.ha_person)}</div>`
                : '<div class="person muted">not linked to a Home Assistant person</div>'}
            </div>
          </div>
          ${fingers}
        </div>`;
    }).join("");

    const unknown = (s.scanners || []).filter((x) => x.loaded && !x.list_known);
    const extras = (s.extras || []).length;
    let summary;
    if (unknown.length) {
      summary = `<p class="msg show warn">${unknown.map((x) => `“${safe(x.title)}”`).join(", ")}
        could not be read, so ${unknown.length === 1 ? "its column shows" : "their columns show"}
        “?”. Nothing is assumed about ${unknown.length === 1 ? "it" : "them"}: a fingerprint may
        or may not be there.</p>`;
    } else if (drift || extras) {
      summary = `<p class="msg show warn">${drift} fingerprint(s) are in the database but not on
        every scanner${extras ? `, and ${extras} are on a scanner but not in the database` : ""}.
        <b>Nothing is copied automatically</b> — use the buttons.</p>`;
    } else {
      summary = `<p class="msg show ok">Every fingerprint in the database is on all
        ${(s.scanners || []).length} scanner(s).</p>`;
    }

    return `<div class="card">
        <h2>Fingerprints in the database</h2>
        <div class="bar">
          <button class="ghost" id="storage-push"${busy || !drift ? " disabled" : ""}
            >Push ${drift} missing fingerprint(s)…</button>
        </div>
        ${summary}
        ${this._renderLegend()}
        ${rows}
      </div>`;
  }

  _renderStorageExtras() {
    const extras = (this._storage.extras || []).slice(0, MAX_PREVIEW_ROWS);
    if (!extras.length) return "";
    const busy = this._jobRunning();
    const rows = extras.map((extra) => `
        <div class="finger">
          <div class="fname">
            <div class="apid">${esc(shortApid(extra.apid))}</div>
            <div class="person muted">${safe(extra.scanners.join(", "), 80)}${
              extra.user_hint ? ` · “${safe(extra.user_hint)}”` : ""}${
              extra.finger_hint ? ` · finger ${esc(extra.finger_hint)}` : ""}</div>
          </div>
          <div class="chips">${this._chip("extra", extra.scanners[0],
            `${extra.scanners[0]} — on that scanner, not in the database`)}</div>
          <div class="bar" style="margin:0">
            <button class="sm" data-adopt="${esc(extra.apid)}"
              data-adopt-entry="${esc(extra.entry_ids[0])}"${busy ? " disabled" : ""}
              >Adopt into database</button>
          </div>
        </div>`).join("");

    return `<div class="card">
        <h2>On a scanner, not in the database</h2>
        <p class="hint">These fingerprints work today. What is missing is Home Assistant's copy —
          without it they cannot be restored, moved to a new scanner, or repaired.
          <b>Adopt</b> reads the template off the scanner and stores it; nothing is written to the
          scanner and nobody presents a finger.</p>
        ${rows}
      </div>`;
  }

  // ---------------------------------------------------------------- dialogs

  /* One overlay renderer, so the backdrop, the dialog role and the in-box message
     are written once and cannot drift apart between six dialogs. The message has to
     be INSIDE the box: _render() suppresses the page-level message whenever a modal
     is showing, so a dialog that forgot it would print its errors behind its own
     overlay. */
  _renderDialog(body, { title, wide = false } = {}) {
    return `<div class="modal" id="dlg">
        <div class="modal-box${wide ? " wide" : ""}" role="dialog" aria-modal="true"
             aria-labelledby="dlg-title" tabindex="-1">
          <h2 id="dlg-title">${title}</h2>
          ${body}
          ${this._renderMessage()}
        </div>
      </div>`;
  }

  /* True while closing would strand something: a transfer mid-flight, or a job that
     is the only report of a minutes-long operation. */
  _dialogLocked() {
    if (this._job && !this._job.done) return true;
    if (this._dialog && this._dialog.busy) return true;
    return false;
  }

  _closeDialog() {
    if (this._dialogLocked()) return;
    const dialog = this._dialog;
    if (dialog && dialog.uploadId) {
      this._ws({ type: "ekey_ha_app/storage/restore/abort", upload_id: dialog.uploadId })
        .catch(() => {});
    }
    if (dialog && dialog.downloadId) {
      this._ws({ type: "ekey_ha_app/storage/backup/end", download_id: dialog.downloadId })
        .catch(() => {});
    }
    this._dialog = null;
    this._job = null;              // only reachable once done
    this._formCache = {};
    this._message = null;
    /* A change that arrived while this dialog was open was deferred, not dropped. */
    if (this._stale) {
      this._stale = false;
      this._load();
      return;
    }
    this._render();
  }

  _renderStorageModal() {
    if (this._job) return this._renderJob();
    const dialog = this._dialog;
    if (!dialog) return "";
    switch (dialog.kind) {
      case "syncFrom": return this._renderSyncFrom();
      case "backup": return this._renderBackup();
      case "restore": return this._renderRestore();
      case "clean": return this._renderClean();
      default: return "";
    }
  }

  _renderScannerModal() {
    if (this._job) return this._renderJob();
    const dialog = this._dialog;
    if (!dialog || dialog.kind !== "syncTo") return "";
    return this._renderSyncTo();
  }

  _renderSyncFrom() {
    const dialog = this._dialog;
    const busy = dialog.busy;
    const chosen = (this._storage.scanners || []).find((s) => s.entry_id === dialog.entryId)
      || {};
    const options = (this._storage.scanners || []).map((s) => {
      const label = !s.loaded
        ? `${s.title} — not loaded`
        : s.list_known
          ? `${s.title} — ${s.on_scanner_count} fingerprint(s)`
          : `${s.title} — list unavailable`;
      return `<option value="${esc(s.entry_id)}"${s.entry_id === dialog.entryId ? " selected" : ""}${
        s.loaded ? "" : " disabled"}>${safe(label, 80)}</option>`;
    }).join("");

    /* Never a fabricated count: a scanner whose list could not be read gets
       "all", because the number genuinely is not known yet. */
    const count = chosen.list_known ? chosen.on_scanner_count : null;
    const seconds = count ? Math.max(1, Math.round(count * 4 / 60)) : null;
    return this._renderDialog(`
      <p class="hint" style="margin-top:0">Every fingerprint that scanner holds is read and stored
        in Home Assistant. Nothing is written to the scanner and nobody presents a finger. A
        record the database already has is refreshed, not duplicated.</p>
      <label for="sf-scanner">Scanner</label>
      <select id="sf-scanner"${busy ? " disabled" : ""}>${options}</select>
      <p class="hint">${count === null
        ? "That scanner's list could not be read, so the number is not known yet."
        : `Reading one fingerprint takes a few seconds, so ${count} will take roughly
           ${seconds} minute(s). You can stop part-way; whatever has been copied is kept.`}</p>
      <div class="bar">
        <button id="sf-go"${busy ? " disabled" : ""}>${busy ? "Starting…"
          : count === null ? "Copy all fingerprints" : `Copy ${count} fingerprint(s)`}</button>
        <button class="ghost" id="sf-cancel"${busy ? " disabled" : ""}>Cancel</button>
      </div>`, { title: "Copy fingerprints from a scanner into the database" });
  }

  _renderSyncTo() {
    const dialog = this._dialog;
    const preview = dialog.preview;
    const scanner = this._scanners.find((s) => s.entry_id === dialog.entryId) || {};
    const title = `Copy “${safe(scanner.title)}” into the database`;

    if (!preview) {
      return this._renderDialog(`
        <p class="progress">Reading this scanner's fingerprint list…</p>
        <div class="bar"><button class="ghost" id="st-cancel">Cancel</button></div>`, { title });
    }
    if (!preview.list_known) {
      /* No Continue button at all. Offering one over a list we could not read would
         start a minutes-long job against a guess. */
      return this._renderDialog(`
        <p class="hint" style="margin-top:0">This scanner's fingerprint list could not be read
          just now, so there is nothing to preview and nothing has been copied. Try Refresh in a
          moment.</p>
        <div class="bar"><button class="ghost" id="st-cancel">Close</button></div>`, { title });
    }
    if (!preview.items.length) {
      return this._renderDialog(`
        <p class="hint" style="margin-top:0">This scanner holds no fingerprints, so there is
          nothing to copy.</p>
        <div class="bar"><button class="ghost" id="st-cancel">Close</button></div>`, { title });
    }

    const rows = preview.items.slice(0, MAX_PREVIEW_ROWS).map((item) => `
        <div class="finger">
          <div class="fname">${item.user_hint
            ? `${safe(item.user_hint)} · finger ${esc(item.finger_hint)}`
            : "unassigned on this scanner"}
            <div class="apid">${esc(shortApid(item.apid))}</div>
          </div>
          <div class="chips">${item.in_database
            ? this._chip("ok", "", "already in the database — its template will be refreshed")
            : this._chip("extra", "", "not in the database yet")}</div>
        </div>`).join("");

    return this._renderDialog(`
      <p class="hint" style="margin-top:0">These are the fingerprints this scanner holds. Continue
        copies each one into Home Assistant's database. The scanner is not changed.</p>
      ${rows}
      <p class="hint">${preview.items.length} fingerprint(s): ${preview.new_count} new,
        ${preview.known_count} already stored (their templates will be refreshed).</p>
      <div class="bar">
        <button id="st-go"${dialog.busy ? " disabled" : ""}>${dialog.busy ? "Starting…" : "Continue"}</button>
        <button class="ghost" id="st-cancel"${dialog.busy ? " disabled" : ""}>Cancel</button>
      </div>`, { title });
  }

  _renderBackup() {
    const dialog = this._dialog;
    const s = this._storage || {};
    const encrypt = dialog.encrypt !== false;
    return this._renderDialog(`
      <p class="hint" style="margin-top:0">Writes every database record — including the
        fingerprint templates themselves — to a file on this computer. ${s.record_count || 0}
        record(s), about ${humanBytes(s.bytes || 0)}.</p>
      ${encrypt ? `
        <label for="bk-pass">Passphrase</label>
        <input type="password" id="bk-pass" autocomplete="new-password" spellcheck="false">
        <label for="bk-pass2">Repeat the passphrase</label>
        <input type="password" id="bk-pass2" autocomplete="new-password" spellcheck="false">
        <p class="hint">At least 8 characters. The file is encrypted on the Home Assistant side
          before it reaches this browser. There is no way to recover the passphrase — a lost
          passphrase is a lost backup.</p>` : ""}
      <label class="check"><input type="checkbox" id="bk-plain"${encrypt ? "" : " checked"}>
        Save without encryption</label>
      ${encrypt ? "" : `
        <p class="msg show warn">An unencrypted backup contains working fingerprint templates in
          plain text. Anyone who copies the file can write those fingerprints into another
          scanner. Keep it off shared drives and delete it when you are done.</p>`}
      <div class="bar">
        <button id="bk-go"${dialog.busy ? " disabled" : ""}>${
          dialog.busy ? "Preparing…" : "Create backup"}</button>
        <button class="ghost" id="bk-cancel"${dialog.busy ? " disabled" : ""}>Cancel</button>
      </div>`, { title: "Create a backup" });
  }

  _renderRestore() {
    const dialog = this._dialog;
    const title = "Restore a backup";

    /* 1 — nothing chosen. */
    if (!dialog.file) {
      return this._renderDialog(`
        <p class="hint" style="margin-top:0">Choose a backup file. Nothing changes until you have
          seen what it contains and pressed Restore.</p>
        <label for="rs-file">Backup file</label>
        <input type="file" id="rs-file" accept=".ekeybak,.json,application/json,application/octet-stream">
        <div class="bar"><button class="ghost" id="rs-cancel">Cancel</button></div>`, { title });
    }

    const name = `<p class="progress">${safe(dialog.file.name, 60)} — ${humanBytes(dialog.file.size)}</p>`;

    /* 2 — uploading. The file input is GONE from here on: its value cannot be set
       programmatically, so it must never be the only copy across a re-render. */
    if (dialog.uploading) {
      const done = dialog.sent || 0;
      const total = dialog.chunks || 1;
      return this._renderDialog(`
        ${name}
        <div class="track" role="progressbar" aria-valuemin="0" aria-valuemax="${total}"
             aria-valuenow="${done}" aria-label="Uploading part ${done} of ${total}">
          <div class="track-fill" style="width:${Math.round((done / total) * 100)}%"></div>
        </div>
        <p class="hint">Sending the file to Home Assistant…</p>
        <div class="bar"><button class="ghost" id="rs-cancel">Cancel</button></div>`, { title });
    }

    const refile = `<button class="ghost" id="rs-refile">Choose a different file…</button>`;
    const header = dialog.header || {};
    const described = `${header.encryption ? "Encrypted" : "Unencrypted"} backup, created
      ${safe(header.created || "at an unknown time")} by ${safe(header.created_by || "an unknown build")}.`;

    /* 3 — encrypted, still locked. The header is plaintext by design so this can be
       shown before anyone types a secret; the wording stays conditional because an
       unverified header is a claim, not a fact. */
    if (dialog.needsPassphrase) {
      return this._renderDialog(`
        ${name}
        <p class="hint">${described} It says it holds ${esc(header.record_count)} fingerprint(s)
          for ${esc(header.user_count)} user(s). The passphrase is needed before the contents can
          be listed.</p>
        ${dialog.foreign ? `<p class="msg show warn">This backup was made on a different Home
          Assistant. That is fine, but check the device variants match your scanners.</p>` : ""}
        <label for="rs-pass">Passphrase</label>
        <input type="password" id="rs-pass" autocomplete="off" spellcheck="false">
        <div class="bar">
          <button id="rs-unlock"${dialog.busy ? " disabled" : ""}>${
            dialog.busy ? "Opening…" : "Unlock and preview"}</button>
          ${refile}
          <button class="ghost" id="rs-cancel">Cancel</button>
        </div>`, { title });
    }

    /* 4 — previewed. */
    const preview = dialog.preview || {};
    const users = (preview.users || []).slice(0, MAX_PREVIEW_ROWS).map((user) => `
        <div class="finger">
          <div class="fname">${safe(user.username)} — ${esc(user.total)} finger(s)</div>
          <div class="chips">
            ${user.new ? this._chip("extra", "", `${user.new} new`) : ""}
            ${user.known ? this._chip("ok", "", `${user.known} already stored`) : ""}
          </div>
        </div>`).join("");
    const more = (preview.users || []).length > MAX_PREVIEW_ROWS
      ? `<p class="hint">…and ${(preview.users || []).length - MAX_PREVIEW_ROWS} more user(s).</p>`
      : "";
    const replacing = dialog.mode === "replace" && preview.db_only_count > 0;

    return this._renderDialog(`
      ${name}
      <p class="hint">${described} ${esc(preview.record_count)} fingerprint(s) for
        ${(preview.users || []).length} user(s).</p>
      ${dialog.foreign ? `<p class="msg show warn">This backup was made on a different Home
        Assistant.</p>` : ""}
      ${(dialog.problems || []).length ? `<p class="msg show warn">${dialog.problems.length}
        record(s) in this file are damaged and will be skipped: ${
          safe(dialog.problems.slice(0, 3).join("; "), 300)}</p>` : ""}
      <div class="item">${users}</div>
      ${more}
      <p class="hint">Your database holds ${(this._storage || {}).record_count || 0}
        fingerprint(s) today. Restoring adds ${esc(preview.new_count)} and refreshes
        ${esc(preview.refresh_count)}. ${preview.db_only_count} record(s) exist only in your
        database.</p>
      <label for="rs-mode">Records that are only in your database</label>
      <select id="rs-mode">
        <option value="merge"${dialog.mode === "replace" ? "" : " selected"}>Keep them (merge)</option>
        <option value="replace"${dialog.mode === "replace" ? " selected" : ""}
          >Delete them — make the database match the file exactly</option>
      </select>
      ${replacing ? `<label class="check"><input type="checkbox" id="rs-ack">
        I understand ${esc(preview.db_only_count)} record(s) will be deleted from the
        database</label>` : ""}
      <p class="hint">Nothing is written to any scanner by a restore. Afterwards the matrix shows
        which scanners are missing the restored fingerprints, and you choose what to push.</p>
      <div class="bar">
        <button id="rs-go" class="${replacing ? "danger" : ""}"${dialog.busy ? " disabled" : ""}
          >Restore ${esc(preview.record_count)} record(s)</button>
        ${refile}
        <button class="ghost" id="rs-cancel"${dialog.busy ? " disabled" : ""}>Cancel</button>
      </div>`, { title });
  }

  _renderClean() {
    const count = (this._storage || {}).record_count || 0;
    return this._renderDialog(`
      <p class="msg show err">This permanently deletes all ${count} fingerprint record(s) and
        their templates from Home Assistant. It cannot be undone.</p>
      <p class="hint">Your scanners are not touched: every fingerprint that opens a door today
        keeps working. What you lose is Home Assistant's copy — and with it the ability to repair
        a scanner, add a new one, or recover after a factory reset. Create a backup first if you
        have not.</p>
      <label for="cl-word">Type <b>DELETE</b> to confirm</label>
      <input type="text" id="cl-word" autocomplete="off" spellcheck="false">
      <p class="hint">Nothing happens until the word matches exactly.</p>
      <div class="bar">
        <button class="danger" id="cl-go" disabled>Delete all ${count} record(s)</button>
        <button class="ghost" id="cl-cancel">Cancel</button>
      </div>`, { title: "Delete everything in the database" });
  }

  // ------------------------------------------------------------- job report

  _renderJob() {
    const job = this._job;
    if (!job) return "";
    const running = !job.done;
    const total = job.total || 0;
    const counts = job.counts || { ok: 0, skipped: 0, failed: 0 };

    /* Newest first while running: a full re-render resets scrollTop, so appending at
       the bottom of a scrolling list would push each new result out of sight. Once
       done, grouped by outcome — failures first, because that is the order you act
       on them in. */
    const items = (job.items || []).slice();
    const ordered = running
      ? items.reverse()
      : items.sort((a, b) => {
          const rank = { failed: 0, skipped: 1, ok: 2 };
          return (rank[a.state] ?? 3) - (rank[b.state] ?? 3);
        });

    const rows = ordered.slice(0, MAX_PREVIEW_ROWS).map((item) => `
        <div class="finger">
          <div class="fname">${safe(item.label)}
            <div class="apid">${esc(shortApid(item.apid))}${
              item.scanner ? ` → ${safe(item.scanner, 40)}` : ""}</div>
            ${item.detail ? `<div class="person muted">${safe(item.detail, 200)}</div>` : ""}
          </div>
          <div class="chips">${this._chip(
            /* A skip borrows the "blocked" look because that is what it means here:
               permanent, and not something a retry can help. */
            item.state === "ok" ? "ok" : item.state === "skipped" ? "blocked" : "missing",
            item.state, item.reason || item.state)}</div>
        </div>`).join("");

    const bar = total
      ? `<div class="track" role="progressbar" aria-valuemin="0" aria-valuemax="${total}"
             aria-valuenow="${job.index || 0}" aria-label="${job.index || 0} of ${total} done">
           <div class="track-fill" style="width:${Math.round(((job.index || 0) / total) * 100)}%"></div>
         </div>`
      : "";

    if (running) {
      return `<div class="modal" id="job-modal">
          <div class="modal-box wide" role="dialog" aria-modal="true" aria-labelledby="job-title"
               tabindex="-1">
            <h2 id="job-title">${safe(job.title)}</h2>
            <p class="progress" role="status">${safe(job.message, 200)}</p>
            ${bar}
            <p class="hint">${counts.ok} done · ${counts.skipped} skipped · ${counts.failed} failed
              · ${Math.max(0, total - (job.index || 0))} to go. A few seconds each — leaving this
              page does not stop it.</p>
            <div class="joblist" id="job-list" aria-label="Result for each fingerprint">${rows}</div>
            <div class="bar">
              <button class="danger" id="job-stop"${job.cancelling ? " disabled" : ""}>${
                job.cancelling ? "Stopping…" : "Stop"}</button>
              <span class="hint" style="margin:0">${job.cancelling
                ? "Stopping after the current fingerprint — the scanner is mid-transfer."
                : "Fingerprints already copied are kept."}</span>
            </div>
            ${this._renderMessage()}
          </div>
        </div>`;
    }

    const heading = job.cancelled
      ? `Stopped — ${counts.ok} of ${total} done`
      : job.ok ? `Done — ${counts.ok} fingerprint(s)` : `${counts.ok} of ${total} done`;
    const verdict = job.cancelled
      ? `<p class="msg show warn">Stopped at your request. The ${counts.ok} already done are kept;
         the rest were not touched.</p>`
      : job.ok
        ? `<p class="msg show ok">All ${counts.ok} completed and verified on the scanner.</p>`
        : `<p class="msg show warn">${counts.ok} done, ${counts.skipped} skipped,
           ${counts.failed} failed. The ones that did not go through changed nothing — none of
           them was half-written.</p>`;

    return `<div class="modal" id="job-modal">
        <div class="modal-box wide" role="dialog" aria-modal="true" aria-labelledby="job-title"
             tabindex="-1">
          <h2 id="job-title">${esc(heading)}</h2>
          ${verdict}
          <div class="joblist">${rows}</div>
          <div class="bar">
            ${counts.failed ? `<button id="job-retry">Retry the ${counts.failed} that failed</button>` : ""}
            <button class="ghost" id="job-close">Close</button>
          </div>
          ${this._renderMessage()}
        </div>
      </div>`;
  }

  /* The picker and the live progress are ONE dialog, in that order, exactly as on the
     device's page. Keeping progress in the modal matters: the person is looking at this
     dialog and at the scanner, and moving the status back behind an overlay mid-enrolment
     would be the moment they most need to see it. */
  _renderEnroll() {
    const e = this._enroll;
    const busy = e && !e.done;
    if (!busy && !this._enrollOpen) return "";

    const body = busy ? this._renderEnrollProgress(e) : this._renderEnrollPicker();
    /* The message goes INSIDE the box. A failure to start belongs where the person is
       looking, not at the bottom of a page an overlay is covering. */
    return `<div class="modal" id="enroll-modal">
        <div class="modal-box" role="dialog" aria-modal="true" aria-label="Enroll fingerprint">
          ${body}
          ${this._renderMessage()}
        </div>
      </div>`;
  }

  _renderEnrollPicker() {
    const starting = this._enrollStarting;
    const users = this._data.users || [];
    const selected = users.find((u) => u.id === this._enrollUser) || users[0];
    const used = new Set(((selected && selected.fingers) || []).map((f) => f.finger));
    return `<h2>Enroll fingerprint</h2>
        <p class="hint" style="margin-top:0">Pick who this finger belongs to, then press Enroll
          and follow the scanner.</p>
        <label for="en-user">User</label>
        <select id="en-user"${starting ? " disabled" : ""}>${users.map((u) => `<option value="${esc(u.id)}"${
          selected && u.id === selected.id ? " selected" : ""}>${esc(u.username)}</option>`).join("")}</select>
        <label for="en-finger">Finger</label>
        <select id="en-finger"${starting ? " disabled" : ""}>${Array.from({ length: FINGER_COUNT }, (_, i) => {
          const n = i + 1;
          return `<option value="${n}">Finger ${n}${used.has(n) ? " — occupied" : ""}</option>`;
        }).join("")}</select>
        <p class="hint">Choosing an occupied slot does not delete anything: the old fingerprint
          stays on the scanner and becomes unassigned.</p>
        <div class="bar">
          <button id="do-enroll"${starting ? " disabled" : ""}>${starting ? "Starting…" : "Enroll"}</button>
          <button class="ghost" id="close-enroll"${starting ? " disabled" : ""}>Cancel</button>
        </div>`;
  }

  _renderEnrollProgress(e) {
    return `<h2>Enrolling ${esc(e.username)} — finger ${esc(e.finger)}</h2>
        <p class="progress">${esc(e.message || "Working…")}</p>
        <p class="hint">Templates collected: ${esc(e.templates || 0)} · tries: ${esc(e.tries || 0)}</p>
        <div class="bar">
          <button class="danger" data-cancel-enroll="${esc(e.apid)}">Cancel enrollment</button>
        </div>`;
  }

  _renderMessage() {
    if (!this._message) return '<div class="msg"></div>';
    return `<div class="msg show ${esc(this._message.kind)}">${esc(this._message.text)}</div>`;
  }

  _personName(entityId) {
    const found = this._persons.find((p) => p.entity_id === entityId);
    return found ? found.name : entityId;
  }

  // --------------------------------------------------------------------- wiring

  _wire() {
    const root = this.shadowRoot;
    const on = (selector, event, handler) => {
      const node = root.getElementById(selector);
      if (node) node.addEventListener(event, handler);
    };

    on("scanner", "change", async (ev) => {
      const value = ev.target.value;
      this._mode = value === STORAGE_ID ? "storage" : "scanner";
      /* Only a real scanner is ever stored: the sentinel must not reach the
         backend — see STORAGE_ID. Keeping the last scanner selected also means
         switching back needs no extra round trip. */
      if (this._mode === "scanner") this._entryId = value;
      this._editing = null;
      this._enrollOpen = false;
      this._enroll = null;
      this._dialog = null;
      this._message = null;
      this._formCache = {};
      await this._subscribe();
      await this._load();
    });

    // ---- storage view -------------------------------------------------------
    on("goto-storage", "click", async () => {
      this._mode = "storage";
      this._message = null;
      await this._subscribe();
      await this._load();
    });
    on("sync-to-storage", "click", () => this._openSyncTo());
    on("storage-sync", "click", () => this._openSyncFrom());
    on("storage-backup", "click", () => this._openDialog({ kind: "backup", encrypt: true }));
    on("storage-restore", "click", () => this._openDialog({ kind: "restore" }));
    on("storage-clean", "click", () => this._openDialog({ kind: "clean" }));
    on("storage-push", "click", () => this._startPush({}));

    on("dlg", "click", (ev) => {
      /* Backdrop only — a click that began inside the box and drifted out while
         selecting text must not count as "dismiss". */
      if (ev.target === ev.currentTarget) this._closeDialog();
    });

    on("sf-scanner", "change", (ev) => {
      this._dialog.entryId = ev.target.value;
      this._render();
    });
    on("sf-go", "click", () => this._startSyncFrom());
    on("sf-cancel", "click", () => this._closeDialog());

    on("st-go", "click", () => this._startSyncTo());
    on("st-cancel", "click", () => this._closeDialog());

    on("bk-plain", "change", (ev) => {
      this._dialog.encrypt = !ev.target.checked;
      this._render();
    });
    on("bk-go", "click", () => this._createBackup());
    on("bk-cancel", "click", () => this._closeDialog());

    on("rs-file", "change", (ev) => this._pickRestoreFile(ev.target.files && ev.target.files[0]));
    on("rs-refile", "click", () => {
      this._dialog = { kind: "restore" };
      this._render();
    });
    on("rs-unlock", "click", () => this._inspectRestore());
    on("rs-mode", "change", (ev) => {
      this._dialog.mode = ev.target.value;
      this._render();
    });
    on("rs-go", "click", () => this._commitRestore());
    on("rs-cancel", "click", () => this._closeDialog());

    /* Enabled straight on the node, without a re-render: re-rendering on every
       keystroke would fight the caret. The word is checked again on click, and
       again on the server — a check that only happens in the page proves nothing. */
    on("cl-word", "input", (ev) => {
      const button = root.getElementById("cl-go");
      if (button) button.disabled = ev.target.value !== "DELETE";
    });
    on("cl-go", "click", () => this._cleanStorage());
    on("cl-cancel", "click", () => this._closeDialog());

    on("job-stop", "click", () => this._stopJob());
    on("job-close", "click", () => this._closeDialog());
    on("job-retry", "click", () => this._retryFailed());
    on("job-modal", "click", (ev) => {
      if (ev.target === ev.currentTarget) this._closeDialog();
    });

    on("reload", "click", () => { this._message = null; this._load(); });
    on("add-user", "click", () => this._addUser());
    on("start-enroll", "click", () => {
      this._enrollOpen = true;
      this._enrollUser = null;
      this._render();
    });
    on("close-enroll", "click", () => this._closeEnroll());
    on("do-enroll", "click", () => this._doEnroll());
    on("en-user", "change", (ev) => { this._enrollUser = ev.target.value; this._render(); });

    /* A click on the backdrop closes it, but only on the backdrop itself — a click that
       started inside the box and drifted out while selecting text must not count. */
    on("enroll-modal", "click", (ev) => {
      if (ev.target === ev.currentTarget) this._closeEnroll();
    });
    on("cancel-edit", "click", () => { this._editing = null; this._render(); });

    root.querySelectorAll("[data-edit]").forEach((el) =>
      el.addEventListener("click", () => {
        this._editing = el.getAttribute("data-edit");
        this._message = null;
        this._render();
      }));
    root.querySelectorAll("[data-save]").forEach((el) =>
      el.addEventListener("click", () => this._saveUser(el.getAttribute("data-save"))));
    root.querySelectorAll("[data-deluser]").forEach((el) =>
      el.addEventListener("click", () => this._deleteUser(el.getAttribute("data-deluser"))));
    root.querySelectorAll("[data-delfp]").forEach((el) =>
      el.addEventListener("click", () => this._deleteFingerprint(el.getAttribute("data-delfp"))));
    root.querySelectorAll("[data-assign]").forEach((el) =>
      el.addEventListener("click", () => this._assign(el.getAttribute("data-assign"))));
    root.querySelectorAll("[data-cancel-enroll]").forEach((el) =>
      el.addEventListener("click", () => this._cancelEnroll(el.getAttribute("data-cancel-enroll"))));
    root.querySelectorAll("[data-push]").forEach((el) =>
      el.addEventListener("click", () => this._startPush({ apids: [el.getAttribute("data-push")] })));
    root.querySelectorAll("[data-adopt]").forEach((el) =>
      el.addEventListener("click", () => this._adopt(
        el.getAttribute("data-adopt"), el.getAttribute("data-adopt-entry"))));
  }

  // --------------------------------------------------------------------- actions

  async _call(message, okText) {
    try {
      const result = await this._ws(message);
      if (okText) this._say(okText, "ok");
      return result;
    } catch (err) {
      this._say(err.message || String(err), "err");
      return null;
    }
  }

  // ------------------------------------------------------- storage actions

  _openDialog(dialog) {
    this._dialog = dialog;
    this._message = null;
    this._render();
  }

  _openSyncFrom() {
    const first = (this._storage.scanners || []).find((s) => s.loaded) || {};
    this._openDialog({ kind: "syncFrom", entryId: first.entry_id });
  }

  async _startSyncFrom() {
    const dialog = this._dialog;
    if (!dialog.entryId) return;
    dialog.busy = true;
    this._render();
    const result = await this._call({
      type: "ekey_ha_app/storage/sync_from_scanner",
      entry_id: dialog.entryId,
    });
    if (result) {
      this._dialog = null;
      this._job = result;
    } else {
      dialog.busy = false;
    }
    this._render();
  }

  /* From a real scanner's card: the same job, previewed first so nobody approves a
     minutes-long operation over a list they cannot see. */
  async _openSyncTo() {
    this._openDialog({ kind: "syncTo", entryId: this._entryId, preview: null });
    const preview = await this._call({
      type: "ekey_ha_app/storage/scanner_preview",
      entry_id: this._entryId,
    });
    if (!this._dialog || this._dialog.kind !== "syncTo") return;   // closed meanwhile
    this._dialog.preview = preview || { list_known: false, items: [] };
    this._render();
  }

  async _startSyncTo() {
    const dialog = this._dialog;
    dialog.busy = true;
    this._render();
    /* The APIDs that were shown, not "whatever is there now" — what runs is what
       was approved. */
    const apids = (dialog.preview.items || []).map((item) => item.apid);
    const result = await this._call({
      type: "ekey_ha_app/storage/sync_from_scanner",
      entry_id: dialog.entryId,
      apids,
    });
    if (result) {
      this._dialog = null;
      this._job = result;
    } else {
      dialog.busy = false;
    }
    this._render();
  }

  async _adopt(apid, entryId) {
    const result = await this._call({
      type: "ekey_ha_app/storage/sync_from_scanner",
      entry_id: entryId,
      apids: [apid],
    });
    if (result) this._job = result;
    this._render();
  }

  async _startPush(options) {
    const message = { type: "ekey_ha_app/storage/push" };
    if (options.apids) message.apids = options.apids;
    if (options.entry_ids) message.entry_ids = options.entry_ids;
    const result = await this._call(message);
    if (result) {
      this._dialog = null;
      this._job = result;
    }
    this._render();
  }

  async _stopJob() {
    if (!this._job) return;
    this._job.cancelling = true;
    this._render();
    await this._call({
      type: "ekey_ha_app/storage/job/cancel",
      job_id: this._job.job_id,
    });
  }

  /* Only the failures. A skip is permanent — a device variant cannot be changed
     from here — and offering to retry one teaches people to click it forever. */
  async _retryFailed() {
    const failed = (this._job.items || []).filter((item) => item.state === "failed");
    if (!failed.length) return;
    const apids = [...new Set(failed.map((item) => item.apid))];
    const entryIds = [...new Set(failed.map((item) => item.entry_id).filter(Boolean))];
    this._job = null;
    await this._startPush({ apids, entry_ids: entryIds });
  }

  async _createBackup() {
    const root = this.shadowRoot;
    const dialog = this._dialog;
    const encrypt = dialog.encrypt !== false;
    let passphrase = null;

    if (encrypt) {
      const first = (root.getElementById("bk-pass") || {}).value || "";
      const second = (root.getElementById("bk-pass2") || {}).value || "";
      if (!first) {
        this._say('Enter a passphrase, or choose “Save without encryption”.', "warn");
        return;
      }
      if (first !== second) {
        this._say("The two passphrases do not match.", "warn");
        return;
      }
      if (first.length < 8) {
        this._say("Use at least 8 characters.", "warn");
        return;
      }
      passphrase = first;
    }

    dialog.busy = true;
    this._render();

    const handle = await this._call({
      type: "ekey_ha_app/storage/backup/begin",
      encrypt,
      ...(passphrase ? { passphrase } : {}),
    });
    if (!handle) {
      dialog.busy = false;
      this._render();
      return;
    }

    dialog.downloadId = handle.download_id;
    this._job = {
      job_id: `backup-${handle.download_id}`,
      title: "Preparing the backup file",
      total: handle.chunks,
      index: 0,
      counts: { ok: 0, skipped: 0, failed: 0 },
      message: `Downloading part 1 of ${handle.chunks}…`,
      items: [],
      done: false,
      local: true,
    };
    this._render();

    const parts = [];
    try {
      for (let index = 0; index < handle.chunks; index++) {
        if (this._job && this._job.cancelling) break;
        const chunk = await this._ws({
          type: "ekey_ha_app/storage/backup/chunk",
          download_id: handle.download_id,
          index,
        });
        parts.push(b64ToBytes(chunk.data));
        this._job.index = index + 1;
        this._job.message = `Downloading part ${index + 1} of ${handle.chunks}…`;
        this._render();
      }
    } catch (err) {
      this._job = null;
      this._dialog = null;
      this._say(`The backup could not be downloaded: ${err.message || err}`, "err");
      this._render();
      return;
    }

    await this._ws({
      type: "ekey_ha_app/storage/backup/end",
      download_id: handle.download_id,
    }).catch(() => {});

    const total = parts.reduce((sum, part) => sum + part.length, 0);
    if (total !== handle.size) {
      /* A length check only: a SHA-256 here would need window.crypto.subtle, which
         does not exist over plain HTTP. The file carries its own digest, which the
         server checks on restore. */
      this._job = null;
      this._dialog = null;
      this._say(`The backup came back incomplete (${humanBytes(total)} of `
        + `${humanBytes(handle.size)}). Nothing was saved — try again.`, "err");
      this._render();
      return;
    }

    this._download(parts, handle.filename);
    this._job = null;
    this._dialog = null;
    this._say(`Backup saved as ${handle.filename} (${(this._storage || {}).record_count || 0} `
      + `records, ${humanBytes(handle.size)}). Check your browser's downloads.`, "ok");
    this._render();
  }

  /* This panel is a real Home Assistant page, not a sandboxed frame, so a Blob and
     a temporary <a download> is all a save needs — and it keeps the integration free
     of an HTTP view, which would be a second authenticated surface to look after. */
  _download(parts, filename) {
    const blob = new Blob(parts, { type: "application/octet-stream" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    this.shadowRoot.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  }

  async _pickRestoreFile(file) {
    if (!file) return;
    if (file.size > MAX_RESTORE_BYTES) {
      this._say(`That file is ${humanBytes(file.size)} — too large to be an ekey backup. `
        + "Nothing has been read.", "err");
      return;
    }
    /* Stashed BEFORE any re-render: input[type=file].value cannot be set
       programmatically, so the File object must never be the only copy of itself. */
    this._dialog.file = { name: file.name, size: file.size, handle: file };
    this._dialog.uploading = true;
    this._dialog.chunks = Math.max(1, Math.ceil(file.size / CHUNK_BYTES));
    this._dialog.sent = 0;
    this._render();

    try {
      const begun = await this._ws({
        type: "ekey_ha_app/storage/restore/begin",
        filename: file.name,
        size: file.size,
        chunks: this._dialog.chunks,
      });
      this._dialog.uploadId = begun.upload_id;

      for (let index = 0; index < this._dialog.chunks; index++) {
        const slice = file.slice(index * CHUNK_BYTES, (index + 1) * CHUNK_BYTES);
        const bytes = new Uint8Array(await slice.arrayBuffer());
        await this._ws({
          type: "ekey_ha_app/storage/restore/chunk",
          upload_id: begun.upload_id,
          index,
          data: bytesToB64(bytes),
        });
        this._dialog.sent = index + 1;
        this._render();
      }
    } catch (err) {
      this._dialog = { kind: "restore" };
      this._say(`That file could not be sent: ${err.message || err}`, "err");
      this._render();
      return;
    }

    this._dialog.uploading = false;
    await this._inspectRestore();
  }

  async _inspectRestore() {
    const dialog = this._dialog;
    const root = this.shadowRoot;
    const passphrase = (root.getElementById("rs-pass") || {}).value || "";
    dialog.busy = true;
    this._render();

    const result = await this._call({
      type: "ekey_ha_app/storage/restore/inspect",
      upload_id: dialog.uploadId,
      ...(passphrase ? { passphrase } : {}),
    });
    dialog.busy = false;
    if (!result) {
      this._render();
      return;
    }
    dialog.header = result.header;
    dialog.needsPassphrase = result.needs_passphrase;
    dialog.preview = result.preview;
    dialog.problems = result.problems;
    dialog.foreign = result.foreign;
    dialog.passphrase = passphrase;
    dialog.mode = dialog.mode || "merge";
    this._render();
  }

  async _commitRestore() {
    const dialog = this._dialog;
    const root = this.shadowRoot;
    const mode = dialog.mode || "merge";
    const deletes = (dialog.preview || {}).db_only_count || 0;

    if (mode === "replace" && deletes > 0) {
      const ack = root.getElementById("rs-ack");
      if (!ack || !ack.checked) {
        this._say(`Tick the box to confirm that ${deletes} record(s) will be deleted.`, "warn");
        return;
      }
    }

    dialog.busy = true;
    this._render();
    const result = await this._call({
      type: "ekey_ha_app/storage/restore/commit",
      upload_id: dialog.uploadId,
      ...(dialog.passphrase ? { passphrase: dialog.passphrase } : {}),
      mode,
      confirm_delete: mode === "replace" ? deletes : 0,
    });
    if (!result) {
      dialog.busy = false;
      this._render();
      return;
    }
    this._dialog = null;
    this._stale = false;
    this._say(`Restored ${result.restored} record(s): ${result.added} added, `
      + `${result.refreshed} refreshed, ${result.deleted} deleted.`
      + ((result.problems || []).length
        ? ` ${result.problems.length} damaged record(s) were skipped.` : ""), "ok");
    await this._loadStorage();
  }

  async _cleanStorage() {
    const root = this.shadowRoot;
    const word = (root.getElementById("cl-word") || {}).value || "";
    if (word !== "DELETE") {
      this._say("Type DELETE to confirm.", "warn");
      return;
    }
    const result = await this._call(
      { type: "ekey_ha_app/storage/clean", confirm: "DELETE" },
      null,
    );
    if (result) {
      this._dialog = null;
      this._say(`Deleted ${result.removed} record(s) from the database. Your scanners are `
        + "unchanged and every fingerprint on them still works.", "ok");
      await this._loadStorage();
    } else {
      this._render();
    }
  }

  async _addUser() {
    const root = this.shadowRoot;
    const name = (root.getElementById("new-name").value || "").trim();
    if (!name) { this._say("Enter a name first.", "warn"); return; }
    const person = root.getElementById("new-person").value || null;
    const result = await this._call(
      { type: "ekey_ha_app/users/add", entry_id: this._entryId, username: name, ha_person: person },
      `Added "${name}".`,
    );
    if (result) await this._load();
  }

  async _saveUser(userId) {
    const root = this.shadowRoot;
    const name = (root.getElementById("edit-name").value || "").trim();
    const person = root.getElementById("edit-person").value || null;
    if (!name) { this._say("The name cannot be blank.", "warn"); return; }
    const result = await this._call({
      type: "ekey_ha_app/users/update",
      entry_id: this._entryId,
      user_id: userId,
      username: name,
      ha_person: person,
    }, "Saved.");
    if (result) { this._editing = null; await this._load(); }
  }

  async _deleteUser(userId) {
    const user = (this._data.users || []).find((u) => u.id === userId);
    const count = ((user && user.fingers) || []).length;
    const question = count
      ? `Delete "${user.username}" and its ${count} fingerprint(s) from the scanner?`
      : `Delete "${user ? user.username : userId}"?`;
    if (!confirm(question)) return;
    const result = await this._call(
      { type: "ekey_ha_app/users/delete", entry_id: this._entryId, user_id: userId },
      "User deleted.",
    );
    if (result) { this._editing = null; await this._load(); } else { await this._load(); }
  }

  async _deleteFingerprint(apid) {
    if (!confirm("Delete this fingerprint from the scanner?")) return;
    const result = await this._call(
      { type: "ekey_ha_app/fingerprints/delete", entry_id: this._entryId, apid },
      "Fingerprint deleted.",
    );
    // Reload either way: on failure the sensor may still hold it, and the list
    // must reflect what the sensor actually has rather than what we hoped.
    await this._load();
    return result;
  }

  async _assign(apid) {
    const root = this.shadowRoot;
    const userSel = root.querySelector(`[data-assign-user="${apid}"]`);
    const fingerSel = root.querySelector(`[data-assign-finger="${apid}"]`);
    if (!userSel || !fingerSel) return;
    const result = await this._call({
      type: "ekey_ha_app/fingerprints/assign",
      entry_id: this._entryId,
      apid,
      user_id: userSel.value,
      finger: parseInt(fingerSel.value, 10),
    });
    if (result) {
      const evicted = (result.evicted || []).filter(Boolean);
      this._say(
        evicted.length
          ? "Assigned. The fingerprint that held that slot is now unassigned (still on the scanner)."
          : "Assigned.",
        "ok",
      );
      await this._load();
    }
  }

  async _doEnroll() {
    const root = this.shadowRoot;
    const userId = root.getElementById("en-user").value;
    const finger = parseInt(root.getElementById("en-finger").value, 10);
    this._message = null;

    /* The dialog stays open across the round trip, and only hands over to the progress
       view once the scanner has actually accepted the request. It used to close first,
       which was survivable while this was an inline block: as a modal, a failure would
       take the dialog away and leave its reason behind an overlay that no longer exists. */
    this._enrollStarting = true;
    this._render();

    const result = await this._call({
      type: "ekey_ha_app/enroll/start",
      entry_id: this._entryId,
      user_id: userId,
      finger,
    });

    this._enrollStarting = false;
    if (result) {
      this._enrollOpen = false;
      this._enroll = result.status || { apid: result.apid, done: false, message: "Starting…" };
    }
    this._render();
  }

  async _cancelEnroll(apid) {
    await this._call(
      { type: "ekey_ha_app/enroll/cancel", entry_id: this._entryId, apid },
      "Enrollment cancelled.",
    );
    this._enroll = null;
    await this._load();
  }
}

/* The pure helpers, hung off the class so the render test can reach them. This
   module has no exports by design — it is loaded for its side effect of defining
   the element — and these four are worth covering directly: a base64 bug would not
   show up as an error, it would show up as a backup file that cannot be restored
   six months from now. */
EkeyPanel.helpers = { bytesToB64, b64ToBytes, clip, humanBytes };

if (!customElements.get("ekey-panel")) {
  customElements.define("ekey-panel", EkeyPanel);
}
