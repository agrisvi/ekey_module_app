/* Exercise ekey-panel.js's render functions outside a browser.
 *
 * The panel is plain ES modules with no build step, which is what makes this possible:
 * the only DOM it needs to be instantiated is a handful of stubs, and from there the
 * HTML each state produces can be asserted on directly. No jsdom, no bundler, no
 * headless browser — `node tests/panel_render_test.mjs` and nothing else.
 *
 * What it is for: the enrolment dialog has four states (closed, picking, starting,
 * live) and two of them are only reachable mid-request, so they are exactly the ones
 * that rot unnoticed. In particular it pins down that the dialog is an OVERLAY rather
 * than a card in the page flow, and that it cannot be dismissed out from under a live
 * enrolment — the scanner would be left waiting for a finger with nothing on screen
 * saying so.
 *
 * Usage:
 *   node tests/panel_render_test.mjs [file-url-to-ekey-panel.js]
 */
import assert from "node:assert";

globalThis.HTMLElement = class {
  attachShadow() {
    this.shadowRoot = {
      childElementCount: 0,
      append() {},
      getElementById: () => null,
      querySelectorAll: () => [],
    };
    return this.shadowRoot;
  }
};
globalThis.customElements = {
  _defined: null,
  get() { return undefined; },
  define(name, cls) { this._defined = cls; },
};
globalThis.document = {
  createElement: () => ({}),
  addEventListener() {},
  removeEventListener() {},
};

/* Default to the component's own copy, resolved from this file so the test runs from
 * any working directory. An explicit argument still wins, for checking a copy. */
/* new URL(..., import.meta.url) is already a file: URL — going via .pathname and back
 * through pathToFileURL doubles the drive letter on Windows. */
const modulePath = process.argv[2]
  || new URL("../custom_components/ekey_ha_app/www/ekey-panel.js", import.meta.url).href;
await import(modulePath);
const Panel = globalThis.customElements._defined;
assert.ok(Panel, "the element registered itself");

function panel(state) {
  const p = new Panel();
  p._data = {
    users: [
      { id: "u1", username: "Test", fingers: [{ finger: 7, apid: "a".repeat(32) }] },
      { id: "u2", username: "Demo", fingers: [] },
    ],
    unassigned: [],
    missing: [],
    scanner_list_known: true,
  };
  Object.assign(p, state);
  return p;
}

let pass = 0, fail = 0;
function check(name, cond) {
  if (cond) { pass++; console.log("  ok   " + name); }
  else { fail++; console.log("  FAIL " + name); }
}

console.log("closed:");
check("renders nothing", panel({})._renderEnroll() === "");

console.log("picker open:");
{
  const html = panel({ _enrollOpen: true })._renderEnroll();
  check("is an overlay, not a card", html.includes('class="modal"') && html.includes("modal-box"));
  check("is not rendered as a page card", !html.trimStart().startsWith('<div class="card"'));
  check("has the dialog role", html.includes('role="dialog"') && html.includes('aria-modal="true"'));
  check("has the user select", html.includes('id="en-user"'));
  check("has the finger select", html.includes('id="en-finger"'));
  check("has Enroll and Cancel", html.includes('id="do-enroll"') && html.includes('id="close-enroll"'));
  check("marks an occupied slot", html.includes("Finger 7 — occupied"));
  check("buttons are enabled", !/id="do-enroll" disabled/.test(html));
  check("backdrop is addressable for the click handler", html.includes('id="enroll-modal"'));
}

console.log("start request in flight:");
{
  const html = panel({ _enrollOpen: true, _enrollStarting: true })._renderEnroll();
  check("still an overlay", html.includes('class="modal"'));
  check("Enroll is disabled", html.includes('id="do-enroll" disabled'));
  check("Cancel is disabled", html.includes('id="close-enroll" disabled'));
  check("selects are disabled", html.includes('id="en-user" disabled'));
  check('says "Starting…"', html.includes("Starting…"));
}

console.log("enrolment live:");
{
  const p = panel({
    _enrollOpen: false,
    _enroll: { apid: "abc", username: "Test", finger: 1, done: false, message: "Place the finger", templates: 2, tries: 3 },
  });
  const html = p._renderEnroll();
  check("stays an overlay after the picker closes", html.includes('class="modal"'));
  check("shows progress", html.includes("Place the finger"));
  check("shows counters", html.includes("Templates collected: 2") && html.includes("tries: 3"));
  check("offers Cancel enrollment", html.includes('data-cancel-enroll="abc"'));
  check("does not offer the picker", !html.includes('id="en-user"'));
}

console.log("enrolment finished:");
{
  const html = panel({ _enrollOpen: false, _enroll: { done: true } })._renderEnroll();
  check("the dialog is gone", html === "");
}

console.log("escape / backdrop guards:");
{
  const open = panel({ _enrollOpen: true });
  open._render = () => {};
  open._closeEnroll();
  check("closes an idle picker", open._enrollOpen === false);

  const starting = panel({ _enrollOpen: true, _enrollStarting: true });
  starting._render = () => {};
  starting._closeEnroll();
  check("refuses while the start request is in flight", starting._enrollOpen === true);

  const live = panel({ _enrollOpen: true, _enroll: { done: false } });
  live._render = () => {};
  live._closeEnroll();
  check("refuses while an enrolment is live", live._enrollOpen === true);
}

/* The checks above call _renderEnroll() directly. This one goes through _render(),
   which is what the button actually triggers — a dialog that renders perfectly but is
   never placed in the page looks exactly like a dialog that was never written. */
console.log("full _render() path:");
{
  const p = panel({ _enrollOpen: true });
  p._scanners = [{ entry_id: "e1", name: "ekey", loaded: true }];
  p._entryId = "e1";
  let html = "";
  p._body = { set innerHTML(v) { html = v; }, get innerHTML() { return html; } };
  p._wire = () => {};
  p._render();
  check("the overlay reaches the page", html.includes('id="enroll-modal"'));
  check("the users card is still rendered behind it", html.includes("Users &amp; fingerprints"));
  check("the overlay is last in the markup",
        html.lastIndexOf("enroll-modal") > html.lastIndexOf("Users &amp; fingerprints"));

  const closed = panel({});
  closed._scanners = p._scanners;
  closed._entryId = "e1";
  let html2 = "";
  closed._body = { set innerHTML(v) { html2 = v; }, get innerHTML() { return html2; } };
  closed._wire = () => {};
  closed._render();
  check("no overlay when the dialog is closed", !html2.includes("enroll-modal"));
  check("the Enroll button is there to open it", html2.includes('id="start-enroll"'));
}

/* Wiring, not just markup. _wire() looks elements up by id after every render, so an id
   that gets renamed in the template leaves a button that renders perfectly and does
   nothing. The stub below only hands back elements whose id is actually present in the
   HTML that was just written, which is what makes the assertion meaningful. */
console.log("wiring:");
{
  const p = panel({});
  p._scanners = [{ entry_id: "e1", name: "ekey", loaded: true }];
  p._entryId = "e1";
  let html = "";
  const bound = new Map();
  p._body = { set innerHTML(v) { html = v; }, get innerHTML() { return html; } };
  p.shadowRoot.getElementById = (id) => {
    if (!new RegExp(`id="${id}"`).test(html)) return null;   // not rendered → not bindable
    return { addEventListener: (ev, fn) => bound.set(`${id}:${ev}`, fn) };
  };
  p.shadowRoot.querySelectorAll = () => [];
  p._render();

  check("Refresh exists in the markup", html.includes('id="reload"'));
  check("Refresh is bound to a click handler", bound.has("reload:click"));
  check("Enroll is bound to a click handler", bound.has("start-enroll:click"));

  let loaded = 0;
  p._load = () => { loaded++; };
  bound.get("reload:click")();
  check("clicking Refresh reloads", loaded === 1);

  bound.get("start-enroll:click")();
  check("clicking Enroll opens the dialog", p._enrollOpen === true);
}

/* The panel refreshes itself when the backend says something changed. Both messages
   matter: enrol progress carries the terminal "done", and users_changed covers every
   other path (a delete, a Home Assistant service call, a second browser tab). */
console.log("live events:");
{
  const p = panel({});
  let loaded = 0;
  p._load = () => { loaded++; };
  p._render = () => {};
  p._say = () => {};

  p._onEvent({ event_type: "ekey_enroll_progress", data: { done: false, message: "Place the finger" } });
  check("progress does not reload mid-enrolment", loaded === 0);
  check("progress is kept for the dialog", p._enroll.message === "Place the finger");

  p._onEvent({ event_type: "ekey_enroll_progress", data: { done: true, ok: true, message: "Enrolled." } });
  check("a finished enrolment reloads the user list", loaded === 1);

  p._onEvent({ event_type: "ekey_users_changed", data: {} });
  check("users_changed reloads the user list", loaded === 2);

  p._onEvent({ event_type: "something_else", data: {} });
  check("an unrelated event does not reload", loaded === 2);
}

console.log("message placement:");
{
  const p = panel({ _enrollOpen: true, _message: { text: "Something failed", kind: "err" } });
  check("the message is inside the dialog", p._renderEnroll().includes("Something failed"));
}

/* The serial port is no longer on this page — it is a connection setting and lives in
 * the config entry's Configure dialog (EkeyOptionsFlow; tests/ha_component/test_serial.py
 * covers it). Two things are worth pinning down about the move rather than deleting the
 * section's tests outright: that no port control came back here by accident, and that the
 * one screen where a wrong port is the likely cause says where the setting now is. A
 * panel that quietly drops the pointer leaves the operator with an unreachable backend
 * and nowhere obvious to look. */
console.log("serial port has moved to the options flow:");
{
  const p = panel({
    _scanners: [{ entry_id: "e1", title: "ekey Scanner", loaded: false }],
    _entryId: "e1",
  });
  p._renderShell();
  const html = p._body.innerHTML;
  check("no port picker is rendered anywhere", !html.includes("serial-pick"));
  check("no Use-this-port button is rendered", !html.includes("serial-save"));
  check("the panel no longer has a serial renderer",
    typeof p._renderSerial === "undefined");
  check("an unloaded scanner points at the Configure dialog",
    html.includes("Configure") && html.includes("serial port"));
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
