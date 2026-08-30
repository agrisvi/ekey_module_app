"""Tests for the storage websocket commands.

These are the invariants that only exist at this layer — where the database, the
scanners and the browser meet:

* **the view never carries template hex.** ~14.6 kB per finger of biometric data,
  sent to a browser so it can draw a badge, would be a copy nobody asked for;
* **an unreadable scanner list is reported as unknown, never as empty.** If
  ``list_known`` were false while ``on_scanner`` carried a stale list, the panel
  would render "ok" for a door nobody could actually ask;
* **a restore writes to no scanner.** Recovering the database and changing what
  opens a door stay two separate decisions;
* **the confirmation word is re-checked here.** A check that only ever happened in
  the page proves nothing about what reached the server — the same principle the
  device's own admin page applies to its destructive routes;
* **``confirm_delete`` must match**, which closes the window where the database
  changed between the preview somebody approved and the commit they pressed.
"""
import asyncio
from types import SimpleNamespace

import pytest

from custom_components.ekey_ha_app import ws_api
from custom_components.ekey_ha_app import jobs as jobs_mod
from custom_components.ekey_ha_app import vault as vault_mod
from custom_components.ekey_ha_app.api import EkeyAppClient
from custom_components.ekey_ha_app.backup import create
from custom_components.ekey_ha_app.connection import EkeyConnection
from custom_components.ekey_ha_app.const import DOMAIN
from custom_components.ekey_ha_app.jobs import JobBusy, async_get_jobs
from custom_components.ekey_ha_app.templates import parse_template_hex

from .fake_http import FakeSession
from .test_jobs import FakeBus, FakeStore, _noop
from .test_templates import REAL_APID_A, REAL_APID_B, TEMPLATE_A, TEMPLATE_B

CONN = EkeyConnection(host="dev.local", port=8080, use_ssl=False, token="tok")
PASSPHRASE = "correct horse battery"


class Connection:
    """Captures what a command sent back.

    ``user`` is what ``@websocket_api.require_admin`` reads. Everything here grants
    physical access to a building, so every one of these commands is admin-only and
    the decorator is part of the contract, not decoration — see
    :func:`test_a_non_admin_is_refused`.
    """

    def __init__(self, admin: bool = True):
        self.user = SimpleNamespace(is_admin=admin, id="test-user")
        self.results = []
        self.errors = []

    def send_result(self, msg_id, result=None):
        self.results.append(result)

    def send_error(self, msg_id, code, message):
        self.errors.append((code, message))

    def async_handle_exception(self, msg, err):
        """What Home Assistant calls when a handler raises something unmapped.

        Recorded rather than swallowed: an exception arriving here means the command
        let something through that ``_handle_errors`` does not know about, and a
        test should fail loudly rather than see an empty result list.
        """
        self.errors.append(("unhandled", f"{type(err).__name__}: {err}"))

    @property
    def result(self):
        assert self.results, f"no result was sent (errors: {self.errors})"
        return self.results[-1]


@pytest.fixture(autouse=True)
def _stub_instance_id(monkeypatch):
    """Home Assistant's instance id, without a real Home Assistant.

    ``instance_id.async_get`` opens a Store of its own, which needs far more of a
    running system than these tests build. The value only has to be stable — it is
    hashed into a backup's ``installation`` marker so a restore can notice a file
    came from somewhere else.
    """

    async def _get(hass):
        return "test-instance-id"

    monkeypatch.setattr(ws_api.instance_id, "async_get", _get)


async def call(hass, command, conn, msg):
    """Invoke a command the way Home Assistant does, then wait for its reply.

    The decorator chain is require_admin -> async_response -> _handle_errors, and
    async_response SCHEDULES the handler on a background task rather than awaiting
    it. Driving it that way here means the admin check and the error mapping are
    both exercised, rather than tested around.

    Only the handler's own task is awaited. A command that starts a fingerprint job
    spawns a second task, and awaiting that one too would make it impossible to
    observe a job while it is still running.
    """
    before = len(hass.tasks)
    command(hass, conn, msg)
    for name, task in hass.tasks[before:]:
        if name.startswith("websocket_api"):
            await task
    return conn


async def drain_jobs(hass):
    """Let any spawned fingerprint job run to completion."""
    for name, task in list(hass.tasks):
        if not name.startswith("websocket_api"):
            try:
                await task
            except asyncio.CancelledError:
                pass


def build(*, list_known=True, on_scanner=(), users=None, loaded=True,
          entry_ids=("e1",), dev_variant=10):
    session = FakeSession()
    bus = FakeBus()
    tasks = []
    hass = SimpleNamespace(data={}, bus=bus, tasks=tasks)
    def spawn(coro, name, **kwargs):
        task = asyncio.ensure_future(coro)
        tasks.append((name, task))
        return task

    hass.async_create_background_task = spawn
    hass.async_add_executor_job = lambda func, *a: asyncio.sleep(0, result=func(*a))
    hass.config = SimpleNamespace(location_name="Home", config_dir="/config")

    entries = [SimpleNamespace(entry_id=e, title=f"Scanner {e}") for e in entry_ids]
    hass.config_entries = SimpleNamespace(async_entries=lambda domain: entries)
    hass.data[DOMAIN] = {}

    for entry in entries:
        if not loaded:
            hass.data[DOMAIN][entry.entry_id] = {}
            continue
        hass.data[DOMAIN][entry.entry_id] = {
            "app_client": EkeyAppClient(CONN, session),
            "coordinator": SimpleNamespace(
                data={"device": {"dev_variant": dev_variant, "prod_sn": "45006"}}
            ),
            "app_coordinator": SimpleNamespace(
                data={
                    "scanner_list_known": list_known,
                    "scanner_aps": list(on_scanner),
                    "users": users if users is not None else [],
                },
                async_refresh_now=_noop,
            ),
        }

    vault = vault_mod.EkeyVault.__new__(vault_mod.EkeyVault)
    vault.hass = hass
    vault._store = FakeStore()
    vault._data = vault_mod.empty_vault()
    vault._loaded = True
    hass.data[DOMAIN]["_vault_instance"] = vault

    return hass, session, bus, vault


async def stock(vault, apid=REAL_APID_A, template=TEMPLATE_A, **kwargs):
    await vault.async_put(
        apid=apid, username=kwargs.pop("username", "Master"),
        finger=kwargs.pop("finger", 7),
        template=parse_template_hex(template), dev_variant=10, **kwargs,
    )


# ------------------------------------------------------------------ storage/get


async def test_the_view_carries_no_template_hex():
    hass, _, _, vault = build()
    await stock(vault)
    conn = Connection()

    await call(hass, ws_api.ws_storage_get, conn, {"id": 1})

    assert TEMPLATE_A not in str(conn.result)
    assert conn.result["users"][0]["fingers"][0]["has_template"] is True
    assert conn.result["record_count"] == 1


async def test_the_view_reports_what_each_scanner_holds():
    hass, _, _, vault = build(on_scanner=[REAL_APID_A])
    await stock(vault)
    conn = Connection()

    await call(hass, ws_api.ws_storage_get, conn, {"id": 1})

    row = conn.result["scanners"][0]
    assert row["loaded"] is True
    assert row["list_known"] is True
    assert row["on_scanner"] == [REAL_APID_A]
    assert row["dev_variant"] == 10


async def test_an_unreadable_list_is_reported_as_unknown_and_empty():
    """THE invariant of this layer. If list_known were false while on_scanner still
    carried a stale list, the panel would draw "ok" for a door nobody could ask."""
    hass, _, _, vault = build(list_known=False, on_scanner=[REAL_APID_A])
    await stock(vault)
    conn = Connection()

    await call(hass, ws_api.ws_storage_get, conn, {"id": 1})

    row = conn.result["scanners"][0]
    assert row["list_known"] is False
    assert row["on_scanner"] == []


async def test_an_unloaded_scanner_still_appears_but_claims_nothing():
    hass, _, _, vault = build(loaded=False)
    conn = Connection()

    await call(hass, ws_api.ws_storage_get, conn, {"id": 1})

    row = conn.result["scanners"][0]
    assert row["loaded"] is False
    assert row["list_known"] is False


async def test_a_fingerprint_only_on_a_scanner_is_reported_as_extra():
    """It works today; what is missing is Home Assistant's copy."""
    hass, _, _, vault = build(
        on_scanner=[REAL_APID_B],
        users=[{"id": "u1", "username": "Bob",
                "fingers": [{"finger": 1, "apid": REAL_APID_B}]}],
    )
    await stock(vault)
    conn = Connection()

    await call(hass, ws_api.ws_storage_get, conn, {"id": 1})

    extras = conn.result["extras"]
    assert len(extras) == 1
    assert extras[0]["apid"] == REAL_APID_B
    assert extras[0]["user_hint"] == "Bob"
    assert extras[0]["finger_hint"] == 1
    assert extras[0]["entry_ids"] == ["e1"]


async def test_nothing_is_extra_when_the_list_could_not_be_read():
    """Never invent an adoptable fingerprint out of an unreadable list."""
    hass, _, _, vault = build(list_known=False, on_scanner=[REAL_APID_B])
    conn = Connection()
    await call(hass, ws_api.ws_storage_get, conn, {"id": 1})
    assert conn.result["extras"] == []


async def test_a_stored_fingerprint_is_not_extra():
    hass, _, _, vault = build(on_scanner=[REAL_APID_A])
    await stock(vault)
    conn = Connection()
    await call(hass, ws_api.ws_storage_get, conn, {"id": 1})
    assert conn.result["extras"] == []


async def test_the_view_hands_over_a_running_job():
    """So a page that loads mid-job adopts it instead of showing nothing."""
    hass, session, _, vault = build(on_scanner=[REAL_APID_A])
    session.add("GET", "/app/v1/users", body=[])
    session.add("GET", f"/api/v1/fingerprints/{REAL_APID_A}/template",
                body={"rpc_error_code": "OK", "apFingerTemplate": TEMPLATE_A})
    conn = Connection()

    await call(hass, ws_api.ws_storage_sync_from_scanner, conn, {"id": 1, "entry_id": "e1", "apids": [REAL_APID_A]}
    )
    await call(hass, ws_api.ws_storage_get, conn, {"id": 2})

    assert conn.result["job"] is not None
    assert conn.result["job"]["kind"] == "sync_from_scanner"
    await drain_jobs(hass)


# ------------------------------------------------------------ scanner_preview


async def test_the_preview_separates_new_from_already_stored():
    hass, _, _, vault = build(
        on_scanner=[REAL_APID_A, REAL_APID_B],
        users=[{"id": "u1", "username": "Master", "fingers": [
            {"finger": 7, "apid": REAL_APID_A}, {"finger": 8, "apid": REAL_APID_B},
        ]}],
    )
    await stock(vault)
    conn = Connection()

    await call(hass, ws_api.ws_storage_scanner_preview, conn, {"id": 1, "entry_id": "e1"})

    assert conn.result["new_count"] == 1
    assert conn.result["known_count"] == 1
    assert {i["apid"] for i in conn.result["items"]} == {REAL_APID_A, REAL_APID_B}


async def test_the_preview_of_an_unreadable_scanner_offers_nothing():
    hass, _, _, vault = build(list_known=False, on_scanner=[REAL_APID_A])
    conn = Connection()

    await call(hass, ws_api.ws_storage_scanner_preview, conn, {"id": 1, "entry_id": "e1"})

    assert conn.result["list_known"] is False
    assert conn.result["items"] == []


async def test_the_preview_of_an_unknown_scanner_is_a_not_found():
    hass, _, _, _ = build()
    conn = Connection()
    await call(hass, ws_api.ws_storage_scanner_preview, conn, {"id": 1, "entry_id": "nope"})
    assert conn.errors and conn.errors[0][0] == "not_found"


# ----------------------------------------------------------------------- clean


async def test_clean_removes_everything_and_reports_how_much():
    hass, _, _, vault = build()
    await stock(vault)
    conn = Connection()

    await call(hass, ws_api.ws_storage_clean, conn, {"id": 1, "confirm": "DELETE"})

    assert conn.result == {"removed": 1}
    assert vault.data["records"] == {}


async def test_clean_refuses_without_the_confirmation_word():
    """Re-checked server-side: a check that only happened in the page proves nothing."""
    hass, _, _, vault = build()
    await stock(vault)
    conn = Connection()

    await call(hass, ws_api.ws_storage_clean, conn, {"id": 1, "confirm": "delete"})

    assert conn.errors[0][0] == "invalid_request"
    assert REAL_APID_A in vault.data["records"]


async def test_clean_does_not_touch_any_scanner():
    hass, session, _, vault = build()
    await stock(vault)
    conn = Connection()
    await call(hass, ws_api.ws_storage_clean, conn, {"id": 1, "confirm": "DELETE"})
    assert session.calls == []


# --------------------------------------------------------------- backup round trip


async def test_a_backup_can_be_pulled_down_in_chunks_and_read_back():
    hass, _, _, vault = build()
    await stock(vault)
    conn = Connection()

    await call(hass, ws_api.ws_storage_backup_begin, conn, {"id": 1, "encrypt": True, "passphrase": PASSPHRASE}
    )
    handle = conn.result
    assert handle["filename"].endswith(".ekeybak")
    assert handle["chunks"] >= 1

    import base64

    blob = b""
    for index in range(handle["chunks"]):
        await call(hass, ws_api.ws_storage_backup_chunk, conn, {"id": 2, "download_id": handle["download_id"], "index": index}
        )
        blob += base64.b64decode(conn.result["data"])

    assert len(blob) == handle["size"]

    from custom_components.ekey_ha_app.backup import open_payload

    assert open_payload(blob, PASSPHRASE)["records"][REAL_APID_A]["template"] == TEMPLATE_A


async def test_a_backup_without_a_passphrase_is_refused_unless_unencrypted():
    hass, _, _, vault = build()
    conn = Connection()

    await call(hass, ws_api.ws_storage_backup_begin, conn, {"id": 1, "encrypt": True})

    assert conn.errors[0][0] == "invalid_request"
    assert "passphrase" in conn.errors[0][1]


async def test_an_unencrypted_backup_is_allowed_when_asked_for_explicitly():
    hass, _, _, vault = build()
    await stock(vault)
    conn = Connection()

    await call(hass, ws_api.ws_storage_backup_begin, conn, {"id": 1, "encrypt": False})

    assert conn.result["filename"].endswith(".json")


async def test_an_expired_download_says_so_instead_of_failing_obscurely():
    hass, _, _, _ = build()
    conn = Connection()
    await call(hass, ws_api.ws_storage_backup_chunk, conn, {"id": 1, "download_id": "gone", "index": 0}
    )
    assert conn.errors[0][0] == "invalid_request"
    assert "expired" in conn.errors[0][1]


# -------------------------------------------------------------- restore round trip


def backup_bytes(vault_data, passphrase=PASSPHRASE):
    payload, _ = create(
        vault_data, passphrase=passphrase, created_by="test", installation="other"
    )
    return payload


async def upload(hass, conn, blob, chunks=1):
    import base64

    await call(hass, ws_api.ws_storage_restore_begin, conn, {"id": 1, "filename": "backup.ekeybak", "size": len(blob), "chunks": chunks},
    )
    upload_id = conn.result["upload_id"]
    step = -(-len(blob) // chunks)
    for index in range(chunks):
        piece = blob[index * step : (index + 1) * step]
        await call(hass, ws_api.ws_storage_restore_chunk, conn, {"id": 2, "upload_id": upload_id, "index": index,
             "data": base64.b64encode(piece).decode()},
        )
    return upload_id


async def test_inspecting_an_encrypted_file_describes_it_without_the_passphrase():
    """What lets the dialog say what a file claims to hold before asking for a secret."""
    hass, _, _, vault = build()
    await stock(vault)
    source = dict(vault.data)
    await vault.async_clean()
    conn = Connection()

    upload_id = await upload(hass, conn, backup_bytes(source))
    await call(hass, ws_api.ws_storage_restore_inspect, conn, {"id": 3, "upload_id": upload_id})

    assert conn.result["encrypted"] is True
    assert conn.result["needs_passphrase"] is True
    assert conn.result["preview"] is None
    assert conn.result["header"]["record_count"] == 1
    assert conn.result["foreign"] is True    # created with installation="other"


async def test_inspecting_with_the_passphrase_previews_the_contents():
    hass, _, _, vault = build()
    await stock(vault)
    source = dict(vault.data)
    await vault.async_clean()
    conn = Connection()

    upload_id = await upload(hass, conn, backup_bytes(source))
    await call(hass, ws_api.ws_storage_restore_inspect, conn, {"id": 3, "upload_id": upload_id, "passphrase": PASSPHRASE}
    )

    preview = conn.result["preview"]
    assert preview["record_count"] == 1
    assert preview["new_count"] == 1
    assert preview["refresh_count"] == 0
    assert preview["db_only_count"] == 0
    assert preview["users"][0]["username"] == "Master"


async def test_a_wrong_passphrase_on_inspect_says_nothing_was_restored():
    hass, _, _, vault = build()
    await stock(vault)
    source = dict(vault.data)
    conn = Connection()

    upload_id = await upload(hass, conn, backup_bytes(source))
    await call(hass, ws_api.ws_storage_restore_inspect, conn, {"id": 3, "upload_id": upload_id, "passphrase": "wrong one here"}
    )

    assert conn.errors[0][0] == "invalid_request"
    assert "Nothing has been restored" in conn.errors[0][1]


async def test_committing_a_restore_writes_the_records_and_no_scanner():
    """A restore never touches a sensor: recovering the database and changing what
    opens a door stay two separate decisions."""
    hass, session, _, vault = build()
    await stock(vault)
    source = dict(vault.data)
    await vault.async_clean()
    conn = Connection()

    upload_id = await upload(hass, conn, backup_bytes(source), chunks=3)
    await call(hass, ws_api.ws_storage_restore_commit, conn, {"id": 4, "upload_id": upload_id, "passphrase": PASSPHRASE, "mode": "merge",
         "confirm_delete": 0},
    )

    assert conn.result == {
        "restored": 1, "added": 1, "refreshed": 0, "deleted": 0, "problems": [],
    }
    assert vault.data["records"][REAL_APID_A]["template"] == TEMPLATE_A
    assert session.calls == []


async def test_a_merge_keeps_records_the_file_does_not_have():
    hass, _, _, vault = build()
    await stock(vault)
    source = dict(vault.data)
    await vault.async_clean()
    await stock(vault, apid=REAL_APID_B, template=TEMPLATE_B, username="Bob", finger=1)
    conn = Connection()

    upload_id = await upload(hass, conn, backup_bytes(source))
    await call(hass, ws_api.ws_storage_restore_commit, conn, {"id": 4, "upload_id": upload_id, "passphrase": PASSPHRASE, "mode": "merge",
         "confirm_delete": 0},
    )

    assert set(vault.data["records"]) == {REAL_APID_A, REAL_APID_B}
    assert conn.result["deleted"] == 0


async def test_a_replace_deletes_what_the_file_does_not_have_but_only_when_confirmed():
    hass, _, _, vault = build()
    await stock(vault)
    source = dict(vault.data)
    await vault.async_clean()
    await stock(vault, apid=REAL_APID_B, template=TEMPLATE_B, username="Bob", finger=1)
    conn = Connection()
    upload_id = await upload(hass, conn, backup_bytes(source))

    # The count the panel showed is one; claiming zero must be refused rather than
    # quietly deleting something the operator never approved.
    await call(hass, ws_api.ws_storage_restore_commit, conn, {"id": 4, "upload_id": upload_id, "passphrase": PASSPHRASE,
         "mode": "replace", "confirm_delete": 0},
    )
    assert conn.errors[0][0] == "invalid_request"
    # Nothing was touched: the database still holds only the record the file lacks.
    assert set(vault.data["records"]) == {REAL_APID_B}

    await call(hass, ws_api.ws_storage_restore_commit, conn, {"id": 5, "upload_id": upload_id, "passphrase": PASSPHRASE,
         "mode": "replace", "confirm_delete": 1},
    )
    assert set(vault.data["records"]) == {REAL_APID_A}
    assert conn.result["deleted"] == 1


async def test_a_record_whose_template_is_an_error_reply_is_reported_not_restored():
    """The trap this project already fell into once, arriving through a file."""
    hass, _, _, vault = build()
    await stock(vault)
    source = dict(vault.data)
    source["records"] = {
        **source["records"],
        REAL_APID_B: {"person_key": "name:bob", "username": "Bob", "finger": 1,
                      "template": '{"error":"endpoint not found"}'},
    }
    await vault.async_clean()
    conn = Connection()

    upload_id = await upload(hass, conn, backup_bytes(source))
    await call(hass, ws_api.ws_storage_restore_commit, conn, {"id": 4, "upload_id": upload_id, "passphrase": PASSPHRASE,
         "confirm_delete": 0},
    )

    assert conn.result["restored"] == 1
    assert len(conn.result["problems"]) == 1
    assert REAL_APID_B in conn.result["problems"][0]
    assert REAL_APID_B not in vault.data["records"]


async def test_an_incomplete_upload_is_refused_by_name():
    hass, _, _, vault = build()
    conn = Connection()
    blob = backup_bytes(vault_mod.empty_vault())

    await call(hass, ws_api.ws_storage_restore_begin, conn, {"id": 1, "filename": "b.ekeybak", "size": len(blob), "chunks": 3},
    )
    upload_id = conn.result["upload_id"]
    import base64

    await call(hass, ws_api.ws_storage_restore_chunk, conn, {"id": 2, "upload_id": upload_id, "index": 0,
         "data": base64.b64encode(blob[:10]).decode()},
    )
    await call(hass, ws_api.ws_storage_restore_inspect, conn, {"id": 3, "upload_id": upload_id})

    assert conn.errors[0][0] == "invalid_request"
    assert "incomplete" in conn.errors[0][1]


async def test_aborting_an_upload_frees_it():
    hass, _, _, vault = build()
    conn = Connection()
    upload_id = await upload(hass, conn, backup_bytes(vault_mod.empty_vault()))

    await call(hass, ws_api.ws_storage_restore_abort, conn, {"id": 9, "upload_id": upload_id})
    assert conn.result == {"ok": True}

    await call(hass, ws_api.ws_storage_restore_inspect, conn, {"id": 10, "upload_id": upload_id})
    assert conn.errors[-1][0] == "invalid_request"


async def test_a_file_that_is_not_a_backup_is_refused():
    hass, _, _, _ = build()
    conn = Connection()
    upload_id = await upload(hass, conn, b'{"just":"some json"}')

    await call(hass, ws_api.ws_storage_restore_inspect, conn, {"id": 3, "upload_id": upload_id})

    assert conn.errors[0][0] == "invalid_request"
    assert "format marker" in conn.errors[0][1]


# -------------------------------------------------------------------- job cancel


async def test_a_non_admin_is_refused():
    """Reading this view means reading who can open the doors; writing means
    changing it. Both are admin-only, and the guard is worth a test because it is
    one decorator away from being lost in a refactor."""
    from homeassistant.exceptions import Unauthorized

    hass, _, _, vault = build()
    await stock(vault)
    conn = Connection(admin=False)

    with pytest.raises(Unauthorized):
        await call(hass, ws_api.ws_storage_get, conn, {"id": 1})

    with pytest.raises(Unauthorized):
        await call(hass, ws_api.ws_storage_clean, conn, {"id": 2, "confirm": "DELETE"})

    assert REAL_APID_A in vault.data["records"]


async def test_cancelling_reports_whether_anything_was_running():
    hass, _, _, _ = build()
    conn = Connection()
    await call(hass, ws_api.ws_storage_job_cancel, conn, {"id": 1})
    assert conn.result == {"cancelling": False}


async def test_a_second_job_is_refused_with_its_own_error_code(monkeypatch):
    """So the panel can say "a job is already running" rather than presenting a
    refusal as a malformed request.

    The refusal itself is tested against the manager in test_jobs; what matters
    here is only that ``JobBusy`` reaches the browser as its own code rather than as
    a generic backend error. Raised directly, because whether a real job is still
    running by the time a second call arrives is a matter of scheduling and would
    make this a flaky test of the wrong thing.
    """
    hass, _, _, _ = build()
    conn = Connection()

    async def busy(*args, **kwargs):
        raise JobBusy("another fingerprint job is already running")

    monkeypatch.setattr(async_get_jobs(hass), "async_sync_from_scanner", busy)

    await call(hass, ws_api.ws_storage_sync_from_scanner, conn, {"id": 1, "entry_id": "e1"})

    assert conn.errors[0][0] == "job_running"
    assert "already running" in conn.errors[0][1]


# ------------------------------------------------- reading the scanners, or not


def _counts_refreshes(hass, entry_id="e1"):
    """Count the "go and ask that scanner" calls, and let one change its answer."""
    app = hass.data[DOMAIN][entry_id]["app_coordinator"]
    calls = []

    async def refresh():
        calls.append(entry_id)

    app.async_refresh_now = refresh
    return calls, app


async def test_the_view_can_ask_the_scanners_or_read_the_snapshot():
    """Refresh has to mean "ask them". It used to re-read Home Assistant's own
    five-minute-old snapshot, so pressing it re-rendered an identical page."""
    hass, _, _, vault = build()
    await stock(vault)
    calls, _ = _counts_refreshes(hass)

    await call(hass, ws_api.ws_storage_get, Connection(), {"id": 1})
    assert calls == [], "an ordinary load must not poll every scanner"

    await call(hass, ws_api.ws_storage_get, Connection(), {"id": 2, "refresh": True})
    assert calls == ["e1"]


async def test_a_refresh_changes_what_the_matrix_says():
    """End to end: the cached picture says the scanner holds it, the scanner says
    otherwise, and the refreshed view reports the scanner."""
    hass, _, _, vault = build(on_scanner=[REAL_APID_A])
    await stock(vault)
    _, app = _counts_refreshes(hass)

    async def refresh():
        app.data = {**app.data, "scanner_aps": []}

    app.async_refresh_now = refresh
    conn = Connection()

    await call(hass, ws_api.ws_storage_get, conn, {"id": 1, "refresh": True})

    assert conn.result["scanners"][0]["on_scanner"] == []


async def test_a_scanner_that_went_quiet_is_unknown_not_last_seen():
    """A failed refresh keeps the previous data — which must not be reported as
    current, or a push writes against a list from minutes ago."""
    hass, _, _, vault = build(on_scanner=[REAL_APID_A])
    await stock(vault)
    app = hass.data[DOMAIN]["e1"]["app_coordinator"]
    app.last_update_success = False
    conn = Connection()

    await call(hass, ws_api.ws_storage_get, conn, {"id": 1})

    row = conn.result["scanners"][0]
    assert row["list_known"] is False
    assert row["on_scanner"] == [], "the stale list must not be handed out"


async def test_every_row_says_when_it_was_read():
    hass, _, _, vault = build()
    await stock(vault)
    hass.data[DOMAIN]["e1"]["app_coordinator"].data["read_at"] = 1756500000.0
    conn = Connection()

    await call(hass, ws_api.ws_storage_get, conn, {"id": 1})

    assert conn.result["scanners"][0]["as_of"] == 1756500000.0


async def test_an_unloaded_scanner_still_has_the_field():
    hass, _, _, _ = build(loaded=False)
    conn = Connection()

    await call(hass, ws_api.ws_storage_get, conn, {"id": 1})

    assert conn.result["scanners"][0]["as_of"] is None


async def test_the_preview_reads_the_list_it_is_about_to_copy():
    """Approving a copy of a list nobody re-read is approving what it looked like
    minutes ago."""
    hass, _, _, _ = build(on_scanner=[])
    _, app = _counts_refreshes(hass)

    async def refresh():
        app.data = {**app.data, "scanner_aps": [REAL_APID_A]}

    app.async_refresh_now = refresh
    conn = Connection()

    await call(hass, ws_api.ws_storage_scanner_preview, conn, {"id": 1, "entry_id": "e1"})

    assert [i["apid"] for i in conn.result["items"]] == [REAL_APID_A]


# ------------------------------------------------- enrol and purge (phase 2)


async def test_a_purge_confirmation_must_name_the_fingerprint(monkeypatch):
    """Re-checked server-side. The panel is not the only possible caller, and a
    delete that reaches every scanner is not something to take on trust."""
    hass, _, _, vault = build()
    await stock(vault)
    started = []
    monkeypatch.setattr(
        async_get_jobs(hass), "async_purge_fingerprint",
        lambda apid: started.append(apid) or {"job_id": "j"},
    )
    conn = Connection()

    await call(hass, ws_api.ws_storage_purge_fingerprint, conn,
               {"id": 1, "apid": REAL_APID_A, "confirm": REAL_APID_B})

    assert conn.errors and conn.errors[0][0] == ws_api.ERR_INVALID
    assert "does not name" in conn.errors[0][1]
    assert started == [], "and no job was started"

    await call(hass, ws_api.ws_storage_purge_fingerprint, Connection(),
               {"id": 2, "apid": REAL_APID_A, "confirm": REAL_APID_A})
    assert started == [REAL_APID_A]


async def test_purging_is_admin_only():
    from homeassistant.exceptions import Unauthorized

    hass, _, _, vault = build()
    await stock(vault)
    conn = Connection(admin=False)

    with pytest.raises(Unauthorized):
        await call(hass, ws_api.ws_storage_purge_fingerprint, conn,
                   {"id": 1, "apid": REAL_APID_A, "confirm": REAL_APID_A})

    assert REAL_APID_A in vault.data["records"]


async def test_enrolling_is_admin_only():
    from homeassistant.exceptions import Unauthorized

    hass, _, _, _ = build()
    conn = Connection(admin=False)

    with pytest.raises(Unauthorized):
        await call(hass, ws_api.ws_storage_enroll, conn,
                   {"id": 1, "entry_id": "e1", "user_id": "u1", "finger": 3})


async def test_an_unknown_scanner_says_so_rather_than_raising(monkeypatch):
    """UnknownScannerJob carries a reason worth repeating — a backend with no app
    layer cannot enrol, and "unknown entry_id" would be the wrong explanation."""
    hass, _, _, _ = build()

    async def refuse(entry_id, user_id, finger):
        raise jobs_mod.UnknownScannerJob("“Front door” cannot enrol — no app layer")

    monkeypatch.setattr(async_get_jobs(hass), "async_enroll", refuse)
    conn = Connection()

    await call(hass, ws_api.ws_storage_enroll, conn,
               {"id": 1, "entry_id": "e1", "user_id": "u1", "finger": 3})

    assert conn.errors[0][0] == "not_found"
    assert "cannot enrol" in conn.errors[0][1]
