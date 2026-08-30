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
/* The panel remembers the picker's choice here. Swapped per test — including for a
   store that throws on every access, which is what a private window really does. */
function fakeStorage(initial) {
  let value = initial === undefined ? null : initial;
  return {
    getItem: () => value,
    setItem: (key, next) => { value = next; },
    get written() { return value; },
  };
}
globalThis.window = { localStorage: fakeStorage() };

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

/* The fingerprint database view. The single most important assertion in this whole
 * file is that an unreadable scanner list renders as UNKNOWN and never as "missing":
 * a wrongly-shown "missing" invites a minutes-long push against a guess, and a
 * wrongly-shown "ok" hides a door that is out of step with the others. Everything
 * else here is about the two failure modes of the view itself — mistaking the
 * database for a scanner, and trusting a restored file's contents. */

const HOSTILE = '<img src=x onerror=alert(1)>"\'&';

function storagePanel(state) {
  const p = new Panel();
  p._mode = "storage";
  p._scanners = [
    { entry_id: "e1", title: "Front door", loaded: true },
    { entry_id: "e2", title: "Back door", loaded: true },
    { entry_id: "e3", title: "Garage", loaded: false },
  ];
  p._entryId = "e1";
  p._persons = [];
  p._storage = {
    record_count: 2, user_count: 1, bytes: 14000, changed: 1756000000,
    users: [{ key: "name:jane", username: "Jane", ha_person: null, fingers: [
      { finger: 2, apid: "a".repeat(36), has_template: true, dev_variant: 10 },
      { finger: 3, apid: "b".repeat(36), has_template: true, dev_variant: 99 },
    ] }],
    scanners: [
      { entry_id: "e1", title: "Front door", loaded: true, list_known: true,
        on_scanner: ["a".repeat(36)], on_scanner_count: 1, dev_variant: 10, template_api: true },
      { entry_id: "e2", title: "Back door", loaded: true, list_known: true,
        on_scanner: [], on_scanner_count: 0, dev_variant: 10, template_api: null },
      { entry_id: "e3", title: "Garage", loaded: false, list_known: false,
        on_scanner: [], on_scanner_count: 0, dev_variant: null, template_api: null },
    ],
    extras: [{ apid: "c".repeat(36), entry_ids: ["e1"], scanners: ["Front door"],
               user_hint: "Bob", finger_hint: 1 }],
    job: null,
  };
  Object.assign(p, state);
  return p;
}

console.log("the storage dropdown entry:");
{
  const p = storagePanel({});
  const head = p._renderHead();
  check("the picker appears with a single scanner too",
    panel({ _scanners: [{ entry_id: "e1", title: "One", loaded: true }], _entryId: "e1" })
      ._renderHead().includes('id="scanner"'));
  check("the storage option is present exactly once",
    head.split('value="__storage__"').length === 2);
  check("scanners and storage are in separate groups",
    head.includes('<optgroup label="Scanners">') && head.includes('<optgroup label="Home Assistant">'));
  check("storage is the selected option in storage mode",
    /value="__storage__" selected/.test(head));
  check("no scanner is selected in storage mode", !/value="e1" selected/.test(head));
  check("_entryId still names a real scanner", p._entryId === "e1");
  check("the title says which side you are on", head.includes("Fingerprint storage"));
  check("the picker is disabled while a job runs",
    storagePanel({ _job: { done: false } })._renderHead().includes('id="scanner" disabled'));
}

console.log("matrix chips:");
{
  const p = storagePanel({});
  const record = p._storage.users[0].fingers[0];   // on e1 only, variant 10
  const [front, back, garage] = p._storage.scanners;

  check("a sensor that holds it is ok", p._cellState(record, front) === "ok");
  check("a sensor that does not is missing", p._cellState(record, back) === "missing");
  /* THE assertion. */
  check("an unreadable scanner is unknown, never missing",
    p._cellState(record, garage) === "unknown");
  check("a variant mismatch is blocked, not missing",
    p._cellState(p._storage.users[0].fingers[1], back) === "blocked");
  check("a backend with no template routes is blocked",
    p._cellState(record, { loaded: true, list_known: true, on_scanner: [], template_api: false })
      === "blocked");
  check("unknown wins over a variant mismatch",
    p._cellState(p._storage.users[0].fingers[1], garage) === "unknown");

  const html = p._renderStorageUsers();
  check("the badge says on database", html.includes("on database"));
  check("the storage card never says on scanner", !html.includes(">on scanner<"));
  check("a missing chip names its scanner", html.includes("Back door"));
  check("an unknown chip is rendered", html.includes("chip unknown"));
  check("a blocked chip is rendered", html.includes("chip blocked"));
  /* Measured on one row's chips, not on the whole card — the legend above the rows
     lists the states in its own fixed order and would swamp the comparison. */
  const rowChips = p._matrixChips(record).html;
  check("deviations come before the healthy chips",
    rowChips.indexOf("chip missing") < rowChips.indexOf("chip ok"));
  check("a push button offers only the missing count",
    /data-push="a+"[^>]*>Push to 1 scanner…/.test(html.replace(/\s+/g, " ")));

  const noTemplate = storagePanel({});
  noTemplate._storage.users[0].fingers[0].has_template = false;
  const bare = noTemplate._renderStorageUsers();
  check("a record with no template says so", bare.includes("no template stored"));
  check("a record with no template offers no push",
    !/data-push="a+"/.test(bare));
}

console.log("matrix degradation:");
{
  const one = storagePanel({});
  one._storage.scanners = [one._storage.scanners[0]];
  const oneHtml = one._renderStorageUsers();
  check("one scanner renders one chip per row",
    (oneHtml.match(/class="chip /g) || []).length > 0 && !oneHtml.includes("ok on"));

  const many = storagePanel({});
  many._storage.scanners = Array.from({ length: 8 }, (_, i) => ({
    entry_id: `s${i}`, title: `Door ${i}`, loaded: true, list_known: true,
    on_scanner: ["a".repeat(36)], on_scanner_count: 1, dev_variant: 10, template_api: true,
  }));
  many._storage.users[0].fingers = [many._storage.users[0].fingers[0]];
  const manyHtml = many._renderStorageUsers();
  check("eight healthy scanners collapse to one chip", manyHtml.includes("on all 8 scanners"));
  check("the collapsed chip keeps every name in its tooltip",
    manyHtml.includes("Door 0, Door 1"));

  many._storage.scanners[3].on_scanner = [];
  const drifted = many._renderStorageUsers();
  check("one missing out of eight expands only that one",
    drifted.includes("chip missing") && drifted.includes("ok on 7"));
  check("the deviation is rendered before the collapsed chip",
    drifted.indexOf("chip missing") < drifted.indexOf("ok on 7"));
}

console.log("storage mode versus scanner mode:");
{
  const p = storagePanel({});
  p._renderShell();
  const html = p._body.innerHTML;
  check("the banner says it is the database", html.includes("not a scanner"));
  check("the body is tinted via one class", p._body.className === "wrap storage");
  check("no Add user card in storage mode", !html.includes('id="add-user"'));
  check("the storage tools are present",
    html.includes('id="storage-sync"') && html.includes('id="storage-backup"')
    && html.includes('id="storage-restore"') && html.includes('id="storage-clean"'));
  check("extras are offered for adoption", html.includes("data-adopt="));
  /* The branch-order bug this cascade is arranged to avoid. */
  const stale = storagePanel({ _entryId: "e3" });
  stale._renderShell();
  check("an unloaded last-selected scanner does not hijack the storage view",
    !stale._body.innerHTML.includes("not loaded — it may be"));

  const scanner = panel({
    _scanners: [{ entry_id: "e1", title: "Front door", loaded: true }],
    _entryId: "e1",
  });
  scanner._renderShell();
  const scannerHtml = scanner._body.innerHTML;
  check("scanner mode offers Sync to storage", scannerHtml.includes('id="sync-to-storage"'));
  check("scanner mode offers a way into the database", scannerHtml.includes('id="goto-storage"'));
  check("scanner mode is not tinted", scanner._body.className === "wrap");
  check("scanner mode still renders its own view", scannerHtml.includes('id="start-enroll"'));
}

console.log("dialogs:");
{
  const base = () => storagePanel({});

  const sync = base();
  sync._dialog = { kind: "syncFrom", entryId: "e1" };
  let html = sync._renderStorageModal();
  check("syncFrom is an overlay", html.includes('class="modal"') && html.includes("modal-box"));
  check("syncFrom has the dialog role",
    html.includes('role="dialog"') && html.includes('aria-modal="true"'));
  check("syncFrom names the count it will copy", html.includes("Copy 1 fingerprint(s)"));
  sync._dialog.entryId = "e3";
  check("an unreadable scanner offers no fabricated count",
    sync._renderStorageModal().includes("Copy all fingerprints"));
  sync._dialog = { kind: "syncFrom", entryId: "e1", busy: true };
  check("syncFrom shows a busy state", sync._renderStorageModal().includes("Starting…"));

  const to = base();
  to._mode = "scanner";
  to._data = { app_api: true, users: [], unassigned: [], missing: [], scanner_list_known: true };
  to._dialog = { kind: "syncTo", entryId: "e1", preview: null };
  check("syncTo shows a loading state first",
    to._renderScannerModal().includes("Reading this scanner"));
  to._dialog.preview = { list_known: false, items: [] };
  check("syncTo offers NO continue over an unreadable list",
    !to._renderScannerModal().includes('id="st-go"'));
  to._dialog.preview = { list_known: true, new_count: 1, known_count: 1, items: [
    { apid: "a".repeat(36), user_hint: "Jane", finger_hint: 2, in_database: true },
    { apid: "c".repeat(36), user_hint: null, finger_hint: null, in_database: false },
  ] };
  html = to._renderScannerModal();
  check("syncTo previews what it will take in", html.includes('id="st-go"'));
  check("syncTo marks what is already stored", html.includes("already in the database"));
  check("syncTo names an unassigned fingerprint", html.includes("unassigned on this scanner"));

  const backup = base();
  backup._dialog = { kind: "backup", encrypt: true };
  html = backup._renderStorageModal();
  check("backup asks for a passphrase twice",
    html.includes('id="bk-pass"') && html.includes('id="bk-pass2"'));
  check("backup warns a lost passphrase is a lost backup", html.includes("lost backup"));
  backup._dialog.encrypt = false;
  html = backup._renderStorageModal();
  check("the unencrypted option drops the passphrase fields", !html.includes('id="bk-pass"'));
  check("the unencrypted option warns what the file contains",
    html.includes("plain text") && html.includes("another"));

  const restore = base();
  restore._dialog = { kind: "restore" };
  check("restore starts with a file picker",
    restore._renderStorageModal().includes('id="rs-file"'));
  restore._dialog = { kind: "restore", file: { name: "b.ekeybak", size: 1000 },
                      uploading: true, chunks: 4, sent: 2 };
  html = restore._renderStorageModal();
  check("the file input is GONE once a file is chosen", !html.includes('id="rs-file"'));
  check("the upload shows progress", html.includes('role="progressbar"'));
  restore._dialog = { kind: "restore", file: { name: "b.ekeybak", size: 1000 },
    needsPassphrase: true, header: { encryption: {}, record_count: 27, user_count: 6,
      created: "2026-08-28", created_by: "1.4.0" } };
  html = restore._renderStorageModal();
  check("a locked file asks for the passphrase", html.includes('id="rs-pass"'));
  check("a locked file says what it CLAIMS to hold", html.includes("It says it holds"));
  restore._dialog = { kind: "restore", file: { name: "b.ekeybak", size: 1000 },
    header: { encryption: {}, created: "x", created_by: "y" }, mode: "replace", problems: [],
    preview: { record_count: 5, new_count: 2, refresh_count: 3, db_only_count: 2,
               users: [{ username: "Jane", new: 2, known: 1, total: 3 }] } };
  html = restore._renderStorageModal();
  check("the preview offers merge or replace", html.includes('id="rs-mode"'));
  check("replacing with deletes demands an acknowledgement", html.includes('id="rs-ack"'));
  check("the restore button is dangerous when it deletes", /id="rs-go" class="danger"/.test(html));
  check("the preview says a restore writes to no scanner",
    html.includes("Nothing is written to any scanner"));

  const clean = base();
  clean._dialog = { kind: "clean" };
  html = clean._renderStorageModal();
  check("clean demands a typed word", html.includes('id="cl-word"'));
  check("clean starts with its button disabled", /id="cl-go" disabled/.test(html));
  check("clean says the scanners keep working", html.includes("keeps working"));
  check("clean names how much is lost", html.includes("Delete all 2 record(s)"));
}

console.log("job report:");
{
  const running = storagePanel({ _job: {
    job_id: "j1", title: "Copying from “Front door”", phase: "running", index: 7, total: 12,
    counts: { ok: 6, skipped: 1, failed: 0 }, message: "Copying 7 of 12", done: false,
    items: [
      { apid: "a".repeat(36), label: "Jane · finger 2", state: "ok", scanner: "Front door" },
      { apid: "b".repeat(36), label: "Bob · finger 1", state: "skipped",
        reason: "variant_mismatch", scanner: "Garage", detail: "different device variant" },
    ],
  } });
  let html = running._renderJob();
  check("a running job is an overlay", html.includes('class="modal"'));
  check("a running job shows progress", html.includes('aria-valuenow="7"'));
  check("a running job offers Stop", html.includes('id="job-stop"'));
  check("a running job offers no Close", !html.includes('id="job-close"'));
  check("newest item first while running",
    html.indexOf("Bob · finger 1") < html.indexOf("Jane · finger 2"));
  check("stopping keeps what was done", html.includes("already copied are kept"));
  check("a job cannot be dismissed while running", running._dialogLocked() === true);

  running._job.cancelling = true;
  check("cancelling says so", running._renderJob().includes("Stopping…"));

  const partial = storagePanel({ _job: {
    job_id: "j1", title: "t", total: 30, index: 30, done: true, ok: false,
    counts: { ok: 27, skipped: 2, failed: 1 }, message: "m",
    items: [
      { apid: "a".repeat(36), label: "ok one", state: "ok" },
      { apid: "b".repeat(36), label: "skipped one", state: "skipped", reason: "variant_mismatch" },
      { apid: "c".repeat(36), label: "failed one", state: "failed", reason: "sensor_full" },
    ],
  } });
  html = partial._renderJob();
  check("a partial result reports all three counts",
    html.includes("27 done, 2 skipped,") && html.includes("1 failed"));
  check("a partial result says the failures changed nothing",
    html.includes("changed nothing"));
  check("failures are listed before successes",
    html.indexOf("failed one") < html.indexOf("ok one"));
  check("only failures are offered a retry", html.includes("Retry the 1 that failed"));
  check("a finished job can be closed", html.includes('id="job-close"'));
  check("a finished job is dismissible", partial._dialogLocked() === false);

  const clean = storagePanel({ _job: {
    job_id: "j1", title: "t", total: 3, index: 3, done: true, ok: true,
    counts: { ok: 3, skipped: 0, failed: 0 }, items: [], message: "m",
  } });
  check("an all-ok job says verified", clean._renderJob().includes("verified"));
  check("an all-ok job offers no retry", !clean._renderJob().includes('id="job-retry"'));

  const stopped = storagePanel({ _job: {
    job_id: "j1", title: "t", total: 30, index: 7, done: true, ok: false, cancelled: true,
    counts: { ok: 7, skipped: 0, failed: 0 }, items: [], message: "m",
  } });
  check("a stopped job says how far it got",
    stopped._renderJob().includes("Stopped — 7 of 30"));

  /* A job must win over a picker: it is the only report of a live operation. */
  const both = storagePanel({ _dialog: { kind: "clean" }, _job: { job_id: "j", done: false,
    title: "t", total: 1, index: 0, counts: { ok: 0, skipped: 0, failed: 0 }, items: [] } });
  check("a running job beats an open dialog",
    both._renderStorageModal().includes('id="job-modal"')
    && !both._renderStorageModal().includes('id="dlg"'));
}

console.log("untrusted input from a backup file:");
{
  const p = storagePanel({});
  p._storage.users[0].username = HOSTILE;
  let html = p._renderStorageUsers();
  check("a hostile username is escaped in the body", html.includes("&lt;img"));
  check("no raw img tag survives", !html.includes("<img src=x"));

  /* The chips are the new risk: this view puts untrusted text into title= and
     aria-label=, which the old one never did. */
  const attr = storagePanel({});
  attr._storage.scanners[1].title = 'x" onmouseover="alert(1)';
  html = attr._renderStorageUsers();
  check("a hostile scanner title cannot break out of an attribute",
    !html.includes('onmouseover="'));

  const restore = storagePanel({});
  restore._dialog = { kind: "restore", file: { name: HOSTILE, size: 10 },
    header: { encryption: null, created: HOSTILE, created_by: HOSTILE }, problems: [HOSTILE],
    preview: { record_count: 1, new_count: 1, refresh_count: 0, db_only_count: 0,
               users: [{ username: HOSTILE, new: 1, known: 0, total: 1 }] } };
  html = restore._renderStorageModal();
  check("a hostile filename is escaped", !html.includes("<img src=x"));
  check("a hostile username from a file is escaped", html.includes("&lt;img"));
  /* Escaping neutralises the tag; it does not delete the words. The test is that
     no live markup survives, not that the text is gone. */
  check("hostile header text cannot open a tag", !html.includes("<img"));
  check("hostile header text arrives escaped", html.includes("&lt;img"));

  const job = storagePanel({ _job: { job_id: "j", title: HOSTILE, total: 1, index: 1,
    done: true, ok: false, counts: { ok: 0, skipped: 0, failed: 1 }, message: HOSTILE,
    items: [{ apid: HOSTILE, label: HOSTILE, state: "failed", detail: HOSTILE }] } });
  check("hostile job text is escaped", !job._renderJob().includes("<img src=x"));

  /* Clipping must happen before escaping: slicing an escaped string can cut
     "&quot;" into "&quot", which a browser still resolves inside an attribute. */
  const long = storagePanel({});
  long._storage.scanners[1].title = `${"y".repeat(118)}"`;
  html = long._renderStorageUsers();
  check("clipping does not leave a half-written entity", !/&quot(?!;)/.test(html));

  const many = storagePanel({});
  many._dialog = { kind: "restore", file: { name: "b", size: 1 }, header: {}, problems: [],
    preview: { record_count: 500, new_count: 500, refresh_count: 0, db_only_count: 0,
      users: Array.from({ length: 500 }, (_, i) => ({ username: `U${i}`, new: 1, known: 0, total: 1 })) } };
  html = many._renderStorageModal();
  check("a huge preview is clipped", html.includes("and 450 more user(s)"));
}

console.log("base64 helpers:");
{
  /* The chunked walk exists because btoa(String.fromCharCode(...bytes)) blows the
     argument limit somewhere around 100k, and one template alone is 14 kB. A bug
     here would not raise — it would produce a backup that cannot be restored. */
  const { bytesToB64, b64ToBytes, clip, humanBytes } = Panel.helpers;

  const size = 300 * 1024;
  const bytes = new Uint8Array(size);
  for (let i = 0; i < size; i++) bytes[i] = (i * 31 + 7) & 0xff;

  const round = b64ToBytes(bytesToB64(bytes));
  check("a 300 kB round trip preserves the length", round.length === size);
  check("a 300 kB round trip preserves every byte",
    round.every((value, index) => value === bytes[index]));
  check("an empty buffer round trips", b64ToBytes(bytesToB64(new Uint8Array(0))).length === 0);
  check("a single byte round trips", b64ToBytes(bytesToB64(new Uint8Array([0xff])))[0] === 0xff);
  /* Exactly on the 0x8000 window boundary, where an off-by-one would hide. */
  const edge = new Uint8Array(0x8000 * 2);
  edge[0x8000 - 1] = 0xaa;
  edge[0x8000] = 0xbb;
  const edgeBack = b64ToBytes(bytesToB64(edge));
  check("the chunk boundary is not off by one",
    edgeBack[0x8000 - 1] === 0xaa && edgeBack[0x8000] === 0xbb);

  check("clip shortens and marks", clip("x".repeat(200), 10) === `${"x".repeat(9)}…`);
  check("clip leaves short text alone", clip("short", 10) === "short");
  check("clip tolerates null", clip(null) === "");
  check("humanBytes reads in kB", humanBytes(14582) === "14.2 kB");
  check("humanBytes reads in MB", humanBytes(1500000) === "1.4 MB");
}

console.log("the picker remembers its choice:");
{
  /* Without this the page always reopens on the first scanner, so anyone working in
     the database has to re-pick "Fingerprint storage" after every reload. */
  const scanners = [
    { entry_id: "e1", title: "Front door", loaded: true },
    { entry_id: "e2", title: "Back door", loaded: true },
  ];

  {
    const p = panel({ _scanners: scanners, _entryId: "e1", _mode: "storage" });
    window.localStorage = fakeStorage();
    p._rememberSelection();
    const written = JSON.parse(window.localStorage.written);
    check("writes the mode", written.mode === "storage");
    check("writes the scanner alongside it", written.entry_id === "e1");
  }

  {
    const p = panel({ _scanners: scanners, _entryId: "e1" });
    p._applySelection({ mode: "storage", entry_id: "e2" });
    check("reopens in storage mode", p._mode === "storage");
    check("reopens on the remembered scanner", p._entryId === "e2");
  }

  {
    /* The remembered scanner was deleted from Home Assistant in the meantime. */
    const p = panel({ _scanners: scanners, _entryId: "e1" });
    p._applySelection({ mode: "scanner", entry_id: "gone" });
    check("ignores a scanner that no longer exists", p._entryId === "e1");
    check("stays in scanner mode", p._mode === "scanner");
  }

  {
    /* The sentinel must never come back as an entry id — see STORAGE_ID. */
    const p = panel({ _scanners: scanners, _entryId: "e1" });
    p._applySelection({ mode: "storage", entry_id: "__storage__" });
    check("never restores the sentinel as a scanner", p._entryId === "e1");
  }

  {
    const p = panel({ _scanners: scanners, _entryId: "e1" });
    window.localStorage = fakeStorage("not json at all");
    check("a corrupt value recalls as nothing", p._recallSelection() === null);
    window.localStorage = fakeStorage(JSON.stringify("a string, not an object"));
    check("a non-object value recalls as nothing", p._recallSelection() === null);
    window.localStorage = fakeStorage();
    check("an empty store recalls as nothing", p._recallSelection() === null);
  }

  {
    /* A private window throws on every access rather than returning null. */
    const p = panel({ _scanners: scanners, _entryId: "e1", _mode: "storage" });
    window.localStorage = {
      getItem() { throw new Error("The operation is insecure."); },
      setItem() { throw new Error("The operation is insecure."); },
    };
    let threw = false;
    try {
      check("a throwing store recalls as nothing", p._recallSelection() === null);
      p._rememberSelection();
    } catch (err) {
      threw = true;
    }
    check("a throwing store does not break the panel", !threw);
    window.localStorage = fakeStorage();
  }

  {
    check("applying nothing changes nothing", (() => {
      const p = panel({ _scanners: scanners, _entryId: "e1" });
      p._applySelection(null);
      return p._entryId === "e1" && p._mode === "scanner";
    })());
  }

  {
    /* The trap remembering the mode would otherwise create: with no scanners the head
       renders the storage title but the body renders the no-scanners card, and the
       picker is not drawn at all — so there is no way out, and the next load restores
       it again. */
    const p = panel({ _scanners: [], _entryId: null });
    p._applySelection({ mode: "storage", entry_id: null });
    check("storage mode is not restored with no scanners", p._mode === "scanner");

    const stuck = panel({ _scanners: [], _entryId: null, _mode: "storage" });
    const head = stuck._renderHead();
    check("that state really has no picker to leave by", !head.includes('id="scanner"'));
  }
}

console.log("switching view:");
{
  /* Both ways in go through _selectMode, because when they did not, the button cleared
     less than the dropdown and left the other view's dialog and edits on screen. */
  const scanners = [
    { entry_id: "e1", title: "Front door", loaded: true },
    { entry_id: "e2", title: "Back door", loaded: true },
  ];

  function switcher(state) {
    const p = panel({ _scanners: scanners, _entryId: "e1", ...state });
    p._hass = {
      connection: { subscribeMessage: async () => () => {} },
      callWS: async () => ({ users: [], unassigned: [], missing: [], scanner_list_known: true }),
    };
    window.localStorage = fakeStorage();
    return p;
  }

  {
    const p = switcher({ _editing: "u1", _enrollOpen: true, _dialog: { kind: "backup" }, _formCache: { x: 1 } });
    await p._selectMode("__storage__");
    check("the button switches to storage", p._mode === "storage");
    check("it clears a half-finished edit", p._editing === null);
    check("it closes the enrolment picker", p._enrollOpen === false);
    check("it closes any open dialog", p._dialog === null);
    check("it drops the cached form fields", Object.keys(p._formCache).length === 0);
    check("it never stores the sentinel as a scanner", p._entryId === "e1");
    check("it remembers the choice", JSON.parse(window.localStorage.written).mode === "storage");
  }

  {
    /* Switching scanner used to leave the previous scanner's users on screen for the
       whole round trip, under the new scanner's title. */
    const p = switcher({ _data: { users: [{ id: "old", username: "Someone else", fingers: [] }] } });
    const pending = p._selectMode("e2");
    check("the previous scanner's users are dropped immediately", p._data === null);
    await pending;
    check("the new scanner is selected", p._entryId === "e2");
    check("the mode is back to scanner", p._mode === "scanner");
  }

  {
    const p = switcher({ _mode: "storage", _storage: { records: [], scanners: [], users: [] } });
    const pending = p._selectMode("e1");
    check("the previous database snapshot is dropped immediately", p._storage === null);
    await pending;
  }
}

console.log("refresh asks the scanners:");
{
  /* The button used to re-read Home Assistant's own snapshot, which its five-minute
     poll fills — so it re-rendered an identical page and looked dead, while the
     matrix kept showing a fingerprint on a door it had been deleted from. */
  function spy(state) {
    const p = panel({ _scanners: [{ entry_id: "e1", title: "Front door", loaded: true }],
                      _entryId: "e1", ...state });
    p.sent = [];
    p._hass = {
      connection: { subscribeMessage: async () => () => {} },
      callWS: async (message) => {
        p.sent.push(message);
        return message.type === "ekey_ha_app/storage/get"
          ? { records: [], users: [], scanners: [], extras: [], record_count: 0,
              user_count: 0, bytes: 0, job: null }
          : { users: [], unassigned: [], missing: [], scanner_list_known: true };
      },
    };
    window.localStorage = fakeStorage();
    return p;
  }

  {
    const p = spy({ _mode: "storage" });
    await p._refresh();
    check("the storage read asks the scanners", p.sent[0].refresh === true);
  }

  {
    const p = spy({});
    await p._refresh();
    check("the scanner read asks that scanner", p.sent[0].refresh === true);
    check("it is still scoped to the entry", p.sent[0].entry_id === "e1");
  }

  {
    const p = spy({ _mode: "storage" });
    await p._loadStorage();
    check("an ordinary load does not poll every scanner", p.sent[0].refresh === false);
  }

  {
    /* An event burst must not become a burst of RS-485 round trips. */
    const p = spy({ _mode: "storage" });
    p._onEvent({ event_type: "ekey_storage_changed", data: {} });
    await Promise.resolve();
    await Promise.resolve();
    check("an event-driven reload does not poll", p.sent.every((m) => !m.refresh));
  }

  {
    /* A job just wrote to the doors — that is exactly when the matrix has to be true. */
    const p = spy({ _mode: "storage" });
    p._onEvent({ event_type: "ekey_storage_job", data: { done: true, ok: true, message: "done" } });
    await Promise.resolve();
    await Promise.resolve();
    check("the reload after a job does poll", p.sent.some((m) => m.refresh === true));
  }

  {
    const p = spy({ _mode: "storage" });
    let inFlight = null;
    p._hass.callWS = () => new Promise((resolve) => { inFlight = resolve; });
    p._storage = { records: [], users: [], scanners: [], extras: [],
                   record_count: 0, user_count: 0, bytes: 0, job: null };
    const pending = p._refresh();
    check("the button says what it is doing",
      p._renderStorageTools().includes("Asking the scanners…"));
    check("and cannot be pressed twice",
      /id="storage-refresh" disabled/.test(p._renderStorageTools()));
    inFlight({ records: [], users: [], scanners: [], extras: [], record_count: 0,
               user_count: 0, bytes: 0, job: null });
    await pending;
    check("and goes back to Refresh", p._renderStorageTools().includes(">Refresh<"));
  }

  {
    const p = spy({ _mode: "storage" });
    p._storage = {
      records: [], users: [], extras: [], record_count: 0, user_count: 0, bytes: 0,
      scanners: [
        { entry_id: "e1", title: "Front", loaded: true, as_of: Date.now() / 1000 - 3 },
        { entry_id: "e2", title: "Back", loaded: true, as_of: Date.now() / 1000 - 600 },
      ],
    };
    const html = p._renderStorageTools();
    check("the age shown is the stalest column", html.includes("10 minutes ago"));

    p._storage.scanners = [{ entry_id: "e1", title: "Front", loaded: true, as_of: null }];
    check("an unread scanner says so",
      p._renderStorageTools().includes("Scanners checked not yet"));
  }
}

console.log("enrolling from the database:");
{
  /* One presentation of a finger, one identity on every door. The dialog has to say
     that, and it cannot offer Enrol without a user to attach the finger to. */
  {
    const p = storagePanel({ _dialog: { kind: "enroll", entryId: "e1", finger: 1,
      users: [{ id: "u1", username: "Jane", fingers: [{ finger: 2, apid: "x" }] }],
      userId: "u1", taken: [2] } });
    const html = p._renderStorageModal();
    check("it is a dialog", html.includes('role="dialog"') && html.includes("modal-box"));
    check("it picks the scanner to enrol on", html.includes('id="en2-scanner"'));
    check("only loaded scanners are offered",
      html.includes("Front door") && html.includes("Back door") && !html.includes("Garage"));
    check("it picks the user", html.includes('id="en2-user"') && html.includes("Jane"));
    check("it picks the finger", html.includes('id="en2-finger"'));
    check("an occupied slot is marked", html.includes("Finger 2 — occupied"));
    check("it says how far the copy goes", html.includes("1 other scanner"));
    check("Enroll and Cancel", html.includes('id="en2-go"') && html.includes('id="en2-cancel"'));
  }

  {
    const p = storagePanel({ _dialog: { kind: "enroll", entryId: "e1", finger: 1,
      users: [], userId: null, taken: [] } });
    const html = p._renderStorageModal();
    check("no users means no Enroll", /id="en2-go"[^>]*disabled/.test(html));
    check("and it says why", html.includes("has to belong to somebody"));
  }

  {
    const p = storagePanel({ _dialog: { kind: "enroll", entryId: "e1", finger: 1 } });
    check("the user list is not invented while it loads",
      p._renderStorageModal().includes("Reading that scanner's users…"));
  }

  {
    const p = storagePanel({ _dialog: { kind: "enroll", entryId: "e1", finger: 1,
      users: [{ id: "u1", username: "Jane", fingers: [] }], userId: "u1", busy: true } });
    const html = p._renderStorageModal();
    check("a start in flight cannot be pressed twice", /id="en2-go"[^>]*disabled/.test(html));
    check("and says so", html.includes("Starting…"));
  }

  check("the storage view offers it", storagePanel({})._renderStorageTools()
    .includes('id="storage-enroll"'));
}

console.log("deleting everywhere:");
{
  /* The wording names BOTH sides every time — this is the action that changes doors
     and the database, and "Delete" alone would not say which. */
  {
    const p = storagePanel({});
    p._openPurge("a".repeat(36), "Jane · finger 2");
    check("it reads the holders off the chips", p._dialog.holders.join() === "Front door");
    check("and the scanners it could not ask", p._dialog.unknown.join() === "Garage");

    const html = p._renderStorageModal();
    check("the button counts the doors", html.includes("Delete from 1 scanner(s) and the database"));
    check("it names the fingerprint", html.includes("Jane · finger 2"));
    check("it explains the order", html.includes("re-read to confirm"));
    check("it warns about the unreadable scanner", html.includes("could not be asked"));
    check("it says the record is kept in that case", html.includes("record will be kept"));
    check("Cancel is offered", html.includes('id="pg-cancel"'));
  }

  {
    /* A fingerprint no scanner holds: still deletable, and the button must not
       claim otherwise. */
    const p = storagePanel({});
    p._storage.scanners = [{ entry_id: "e1", title: "Front door", loaded: true,
      list_known: true, on_scanner: [], on_scanner_count: 0, dev_variant: 10 }];
    p._openPurge("a".repeat(36), "Jane · finger 2");
    const html = p._renderStorageModal();
    check("no holders is stated plainly", html.includes("No scanner currently lists"));
    check("and the count is honest", html.includes("Delete from 0 scanner(s)"));
  }

  {
    const p = storagePanel({});
    const html = p._renderStorageUsers();
    check("every finger row offers it", (html.match(/data-purge=/g) || []).length === 2);
    check("the row action is marked dangerous", html.includes('class="sm ghost danger"'));
  }

  {
    /* A hostile username reaching the confirmation through the row's data attribute. */
    const p = storagePanel({});
    p._openPurge("a".repeat(36), '<img src=x onerror=alert(1)>');
    const html = p._renderStorageModal();
    check("the label is escaped", !html.includes("<img") && html.includes("&lt;img"));
  }
}

console.log("a finished job does not come back:");
{
  /* storage/get returns the last COMPLETED job as well as a running one. Adopting
     it re-opened its report as a modal on every reload — on each press of Refresh,
     and again on the reload that follows closing it. */
  const done = { job_id: "j1", done: true, ok: true, message: "All 1 written and verified.",
                 counts: { ok: 1, skipped: 0, failed: 0 }, total: 1, items: [] };
  const live = { job_id: "j2", done: false, message: "Writing 1 of 3", total: 3,
                 index: 1, counts: { ok: 0, skipped: 0, failed: 0 } };
  const empty = { records: [], users: [], scanners: [], extras: [],
                  record_count: 0, user_count: 0, bytes: 0 };

  function loader(job) {
    const p = panel({ _mode: "storage",
                      _scanners: [{ entry_id: "e1", title: "Front door", loaded: true }],
                      _entryId: "e1" });
    p._hass = { callWS: async () => ({ ...empty, job }),
                connection: { subscribeMessage: async () => () => {} } };
    return p;
  }

  {
    const p = loader(done);
    await p._loadStorage({ refresh: true });
    check("a finished job is not adopted", p._job === null);
    check("so Refresh opens no dialog", p._renderStorageModal() === "");
    check("it is reported as a line instead",
      p._renderStorageTools().includes("Last job: All 1 written and verified."));
  }

  {
    const p = loader(live);
    await p._loadStorage();
    check("a running job IS adopted", p._job !== null && p._job.job_id === "j2");
    check("and its dialog opens", p._renderStorageModal().includes("modal-box"));
    check("a running job is not also a Last job line",
      !p._renderStorageTools().includes("Last job:"));
  }

  {
    /* Close, then the reload that closing can trigger, must not bounce it back. */
    const p = loader(done);
    p._job = done;
    p._stale = true;
    p._closeDialog();
    check("closing clears it", p._job === null);
    await p._loadStorage();
    check("and the reload leaves it closed", p._job === null);
  }

  {
    /* A hostile or truncated message is still just text in a card. */
    const p = loader({ ...done, message: '<img src=x onerror=alert(1)>' });
    await p._loadStorage();
    const html = p._renderStorageTools();
    check("the last-job line is escaped", !html.includes("<img") && html.includes("&lt;img"));
  }
}

console.log("live subscription:");
{
  /* subscribeMessage returns a promise. Assigning it instead of awaiting it made the
     catch below unreachable and turned every refused subscription into an unhandled
     rejection in the browser console — a browser-level error for a condition the panel
     has a plain warning for. These pin the handling, not the happy path. */
  function fakeHass(subscribe) {
    return { connection: { subscribeMessage: subscribe }, callWS: async () => ({}) };
  }

  {
    const p = panel({ _entryId: "e1" });
    let unsubbed = false;
    p._hass = fakeHass(async () => () => { unsubbed = true; });
    await p._subscribe();
    check("keeps the unsubscribe handle, resolved", typeof p._unsub === "function");
    p._teardown();
    await Promise.resolve();
    check("teardown calls it", unsubbed);
    check("teardown forgets it", p._unsub === null);
  }

  {
    const p = panel({ _entryId: "e1" });
    p._hass = fakeHass(async () => { throw new Error("nope"); });
    let threw = false;
    try {
      await p._subscribe();
    } catch (err) {
      threw = true;
    }
    check("a refused subscription does not reject", !threw);
    check("a refused subscription warns instead",
      p._message && p._message.kind === "warn"
      && p._message.text.includes("Live updates are unavailable"));
    check("a refused subscription leaves no handle", !p._unsub);
  }

  {
    /* Switching scanner while the subscribe is in flight: the late handle belongs to a
       view that is gone, and keeping it would leave the old scanner's events arriving
       for the life of the connection. */
    const p = panel({ _entryId: "e1" });
    let released = false;
    let release;
    p._hass = fakeHass(() => new Promise((resolve) => { release = resolve; }));
    const pending = p._subscribe();
    p._teardown();                       // the scanner changed under it
    release(() => { released = true; });
    await pending;
    check("a subscription that lands after a teardown is released", released);
    check("a subscription that lands after a teardown is not kept", !p._unsub);
  }

  {
    const p = panel({ _mode: "storage", _entryId: "e1" });
    let sent = null;
    p._hass = fakeHass(async (cb, message) => { sent = message; return () => {}; });
    await p._subscribe();
    check("storage mode subscribes unscoped", sent && sent.entry_id === undefined);

    const q = panel({ _entryId: "e1" });
    let scoped = null;
    q._hass = fakeHass(async (cb, message) => { scoped = message; return () => {}; });
    await q._subscribe();
    check("scanner mode stays scoped", scoped && scoped.entry_id === "e1");
  }
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
