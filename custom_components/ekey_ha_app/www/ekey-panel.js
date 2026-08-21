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
    /* The serial-port reply, or null when this backend has no such setting (a device
       with the sensor on fixed pins) or is too old to answer. null means "render
       nothing", which is why the section never has to ask a capability flag. */
    this._serial = null;
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
    if (!this._entryId) return;
    try {
      this._unsub = this._hass.connection.subscribeMessage(
        (msg) => this._onEvent(msg),
        { type: "ekey_ha_app/subscribe", entry_id: this._entryId },
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
    if (type === "ekey_users_changed") {
      this._load();
      return;
    }
    if (type === "ekey_connection_lost") {
      this._say("The scanner connection was lost. Retrying…", "warn");
    }
  }

  async _load() {
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
    /* Separate from the users read, and separately tolerant: a backend without this
       endpoint must not blank out the user list, and a failure here is not worth a
       message — the section simply is not offered. */
    await this._loadSerial();
    this._loading = false;
    this._render();
  }

  async _loadSerial() {
    if (!this._entryId) {
      this._serial = null;
      return;
    }
    try {
      this._serial = await this._ws({
        type: "ekey_ha_app/serial/get",
        entry_id: this._entryId,
      });
    } catch (err) {
      this._serial = null;
    }
  }

  // -------------------------------------------------------------------- render

  _renderShell() {
    if (this.shadowRoot.childElementCount) return;
    const style = document.createElement("style");
    style.textContent = STYLE;
    const body = document.createElement("div");
    body.className = "wrap";
    this.shadowRoot.append(style, body);
    this._body = body;
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
    } else if (scanner && !scanner.loaded) {
      parts.push(`<div class="card"><p class="empty">This scanner is not loaded — it may be
        starting up or unreachable.</p></div>`);
    } else if (this._data && this._data.app_api === false) {
      parts.push(this._renderNoAppApi(scanner));
    } else if (this._data) {
      parts.push(this._renderAddUser());
      parts.push(this._renderUsers());
      parts.push(this._renderUnassigned());
      parts.push(this._renderSerial());
      modal = this._renderEnroll();
    } else if (this._loading) {
      parts.push('<div class="card"><p class="empty">Loading…</p></div>');
    }

    /* The modal carries its own copy of the message, so showing it here as well would
       print the same sentence twice, one of them behind the overlay. */
    parts.push(modal ? "" : this._renderMessage());
    /* Last, so the overlay paints over the page rather than relying on z-index alone
       to beat a card that comes after it. */
    parts.push(modal);
    this._body.innerHTML = parts.join("");
    this._wire();
  }

  _renderHead() {
    const picker = this._scanners.length > 1
      ? `<div style="min-width:220px">
           <label for="scanner">Scanner</label>
           <select id="scanner">${this._scanners.map((s) => `
             <option value="${esc(s.entry_id)}"${s.entry_id === this._entryId ? " selected" : ""}>
               ${esc(s.title)}${s.loaded ? "" : " (not loaded)"}
             </option>`).join("")}
           </select>
         </div>`
      : "";
    /* Which build is actually loaded. The integration is installed by copying files
       onto a Home Assistant host, so a stale copy behaves exactly like a fix that was
       never made — this makes the difference visible without opening devtools. */
    const version = (this.panel && this.panel.config && this.panel.config.version) || "";
    return `<div class="head">
        <div>
          <h1>ekey users &amp; fingerprints</h1>
          <p class="sub">Managed on the scanner itself — Home Assistant is the front-end.
            Recognitions keep working when Home Assistant is down.</p>
          ${version ? `<p class="sub muted">Integration ${esc(version)}</p>` : ""}
        </div>
        ${picker}
      </div>`;
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
        </div>
        ${users.length ? "" : '<p class="hint">No users yet — add one above, then enroll a finger.</p>'}
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

  /* Which port the scanner is wired to.
     Rendered only when the backend answered — a device with the sensor on fixed pins, or
     an installation where the port comes from the add-on configuration, simply has no
     section here rather than a control that cannot work. Whether the control appears at
     all, and whether a change needs a restart, are read from the reply: both depend on how
     that particular backend was started, so neither can be assumed here. */
  _renderSerial() {
    const s = this._serial;
    if (!s || !Array.isArray(s.ports) || !s.ports.length) return "";

    const active = s.active || s.selected || "";
    const options = s.ports.map((p) => {
      const marks = [];
      if (p.console) marks.push("system console");
      if (p.busy) marks.push("in use");
      const text = `${p.label || p.path} — ${p.path}` +
        (marks.length ? `  [${marks.join(", ")}]` : "");
      const chosen = s.selected && p.path === s.selected ? " selected" : "";
      return `<option value="${esc(p.path)}"${chosen}>${esc(text)}</option>`;
    }).join("");

    let hint;
    if (!s.editable) {
      hint = s.source === "cli"
        ? `The port is set outside Home Assistant — with the daemon's <code>-d</code>
           option, or in the add-on configuration. Change it there.`
        : "The port cannot be changed from here on this installation.";
    } else if (s.applies === "restart") {
      hint = `A different port takes effect when the daemon restarts: it is already
              connected to the current one.`;
    } else {
      hint = `No scanner is connected yet, so a port chosen here is tried within 30
              seconds — no restart needed.`;
    }

    const control = s.editable ? `
        <div class="bar">
          <select id="serial-pick">${options}</select>
          <button class="sm" id="serial-save">Use this port</button>
        </div>` : "";

    return `<div class="card">
        <h2>Scanner connection</h2>
        <div class="finger">
          <div>Serial port</div>
          <div class="apid">${esc(active || "none chosen yet")}</div>
        </div>
        ${control}
        <p class="hint">${hint} Ports marked <b>system console</b> are the machine's own
          terminal; choosing one can switch it into RS485 mode, so it asks first.</p>
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
      this._entryId = ev.target.value;
      this._editing = null;
      this._enrollOpen = false;
      this._enroll = null;
      this._message = null;
      await this._subscribe();
      await this._load();
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
    on("serial-save", "click", () => this._saveSerial());

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

  async _saveSerial() {
    const sel = this.shadowRoot.getElementById("serial-pick");
    if (!sel || !sel.value) return;

    /* The console flag comes from the option text the backend built, so the confirmation
       and the confirm_console the backend checks are about the same port. */
    const opt = sel.options[sel.selectedIndex];
    const isConsole = !!(opt && opt.textContent.indexOf("system console") >= 0);
    if (isConsole && !confirm(
      "That port is the machine's own system console. Using it can switch it into RS485 " +
      "mode and disturb whatever else is on it. Continue?")) {
      return;
    }

    const result = await this._call({
      type: "ekey_ha_app/serial/set",
      entry_id: this._entryId,
      path: sel.value,
      confirm_console: isConsole,
    });
    if (result) {
      /* The reply IS the new state, so there is nothing to re-fetch and no window in
         which the section could disagree with the backend. */
      this._serial = result;
      this._say(result.applies === "restart"
        ? "Saved. Restart the daemon to move to this port."
        : "Saved. Connecting on this port within 30 seconds.", "ok");
    }
    this._render();
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

if (!customElements.get("ekey-panel")) {
  customElements.define("ekey-panel", EkeyPanel);
}
