"""Tests for the fingerprint transfer jobs.

This is the layer that writes to door controllers, so the tests are about the ways
that can go wrong rather than about it working:

* **one job at a time** — two fan-outs interleaving writes to one sensor is not
  something to discover afterwards;
* **``verified`` decides** — a write that answered 200 and was discarded must never
  be counted as stored, or the presence matrix starts claiming a door holds a
  fingerprint it does not;
* **permanent versus retryable** — a device-variant mismatch can never succeed and
  must be a *skip*, while a full sensor is a *failure* worth retrying. Offering a
  retry for the former teaches people to click it forever;
* **an unreadable scanner list is not "missing"** — pushing against a guess would
  be a minutes-long job with no idea what it is doing;
* **the user-document cap** — the backend replaces the whole document on a PUT and
  caps the body, so the size has to be checked before the write;
* **a template written but not assigned** is reported as such, because the finger
  already opens that door and retrying the write would be pointless.

Driven against a fake hass and ``FakeSession``, like the rest of this suite.
"""
import asyncio
from types import SimpleNamespace

import pytest

from custom_components.ekey_ha_app import jobs as jobs_mod
from custom_components.ekey_ha_app import vault as vault_mod
from custom_components.ekey_ha_app.api import EkeyAppClient
from custom_components.ekey_ha_app.connection import EkeyConnection
from custom_components.ekey_ha_app.const import DOMAIN
from custom_components.ekey_ha_app.jobs import (
    REASON_LIST_UNKNOWN,
    REASON_NOT_VERIFIED,
    REASON_SENSOR_FULL,
    REASON_TEMPLATE_ONLY,
    REASON_USERS_DOC_TOO_LARGE,
    REASON_VARIANT_MISMATCH,
    STATE_FAILED,
    STATE_OK,
    STATE_SKIPPED,
    JobBusy,
    VaultJobManager,
)
from custom_components.ekey_ha_app.templates import parse_template_hex

from .fake_http import FakeSession
from .test_templates import REAL_APID_A, REAL_APID_B, TEMPLATE_A, TEMPLATE_B

CONN = EkeyConnection(host="dev.local", port=8080, use_ssl=False, token="tok")

GET_A = f"/api/v1/fingerprints/{REAL_APID_A}/template"
GET_B = f"/api/v1/fingerprints/{REAL_APID_B}/template"
PUT_TEMPLATE = "/api/v1/fingerprints/template"

USERS = [{"id": "u1", "username": "Master", "fingers": [
    {"finger": 7, "apid": REAL_APID_A}, {"finger": 8, "apid": REAL_APID_B},
]}]


def template_reply(template):
    return {"rpc_error_code": "OK", "apFingerTemplate": template, "domainID": "avubs"}


def put_reply(apid, verified=True, verdict="device_response"):
    return {
        "rpc_error_code": "OK", "verdict": verdict, "apid": apid, "verified": verified,
    }


class FakeStore:
    """Stands in for homeassistant.helpers.storage.Store."""

    def __init__(self):
        self.saved = None

    async def async_load(self):
        return self.saved

    async def async_save(self, data):
        self.saved = data


class FakeBus:
    def __init__(self):
        self.events = []

    def async_fire(self, event_type, data=None):
        self.events.append((event_type, data or {}))


def build(*, session=None, list_known=True, on_scanner=(), dev_variant=10,
          entry_ids=("e1",), titles=None):
    """A hass with one loaded scanner per entry id, sharing one FakeSession."""
    session = session or FakeSession()
    bus = FakeBus()
    tasks = []
    titles = titles or {e: f"Scanner {e}" for e in entry_ids}

    hass = SimpleNamespace(data={}, bus=bus, tasks=tasks)
    hass.async_create_background_task = lambda coro, name: tasks.append(
        asyncio.ensure_future(coro)
    ) or tasks[-1]
    hass.async_add_executor_job = lambda func, *a: asyncio.sleep(0, result=func(*a))

    entries = [SimpleNamespace(entry_id=e, title=titles[e]) for e in entry_ids]
    hass.config_entries = SimpleNamespace(async_entries=lambda domain: entries)

    hass.data[DOMAIN] = {}
    for entry in entries:
        hass.data[DOMAIN][entry.entry_id] = {
            "app_client": EkeyAppClient(CONN, session),
            "coordinator": SimpleNamespace(
                data={"device": {"dev_variant": dev_variant, "dev_sub_variant": 10,
                                 "prod_sn": "4500610138250001"}}
            ),
            "app_coordinator": SimpleNamespace(
                data={"scanner_list_known": list_known, "scanner_aps": list(on_scanner)},
                async_refresh_now=_noop,
            ),
        }

    # A fake store, so the vault never touches the real .storage directory.
    vault = vault_mod.EkeyVault.__new__(vault_mod.EkeyVault)
    vault.hass = hass
    vault._store = FakeStore()
    vault._data = vault_mod.empty_vault()
    vault._loaded = True
    hass.data[DOMAIN]["_vault_instance"] = vault

    return hass, session, bus, vault


async def _noop(*args, **kwargs):
    return None


async def run_to_completion(hass, manager):
    """Await the background task the manager spawned."""
    for task in list(hass.tasks):
        try:
            await task
        except asyncio.CancelledError:
            pass
    return manager.status()


def job_events(bus):
    return [data for name, data in bus.events if name == "ekey_storage_job"]


# ------------------------------------------------------------------ one at a time


async def test_a_second_job_is_refused_while_one_runs():
    """Two fan-outs writing to one sensor at once is not a state to be in."""
    hass, session, _, _ = build(on_scanner=[REAL_APID_A])
    session.add("GET", "/app/v1/users", body=USERS)
    session.add("GET", GET_A, body=template_reply(TEMPLATE_A))
    manager = VaultJobManager(hass)

    await manager.async_sync_from_scanner("e1", [REAL_APID_A])

    with pytest.raises(JobBusy):
        await manager.async_sync_from_scanner("e1", [REAL_APID_A])

    await run_to_completion(hass, manager)


async def test_a_new_job_is_allowed_once_the_previous_finished():
    hass, session, _, _ = build()
    session.add("GET", "/app/v1/users", body=USERS)
    session.add("GET", GET_A, body=template_reply(TEMPLATE_A))
    manager = VaultJobManager(hass)

    await manager.async_sync_from_scanner("e1", [REAL_APID_A])
    await run_to_completion(hass, manager)

    await manager.async_sync_from_scanner("e1", [REAL_APID_A])
    assert manager.running


# --------------------------------------------------------- sync from a scanner


async def test_sync_stores_a_template_and_names_its_owner():
    hass, session, bus, vault = build()
    session.add("GET", "/app/v1/users", body=USERS)
    session.add("GET", GET_A, body=template_reply(TEMPLATE_A))
    manager = VaultJobManager(hass)

    await manager.async_sync_from_scanner("e1", [REAL_APID_A])
    status = await run_to_completion(hass, manager)

    assert status["done"] is True and status["ok"] is True
    assert status["counts"] == {"ok": 1, "skipped": 0, "failed": 0}
    record = vault.data["records"][REAL_APID_A]
    assert record["template"] == TEMPLATE_A
    assert record["finger"] == 7
    assert record["username"] == "Master"
    assert record["dev_variant"] == 10
    assert record["source"]["entry_id"] == "e1"


async def test_sync_writes_nothing_to_the_scanner():
    """A read-only operation must stay one."""
    hass, session, _, _ = build()
    session.add("GET", "/app/v1/users", body=USERS)
    session.add("GET", GET_A, body=template_reply(TEMPLATE_A))
    manager = VaultJobManager(hass)

    await manager.async_sync_from_scanner("e1", [REAL_APID_A])
    await run_to_completion(hass, manager)

    assert [c["method"] for c in session.calls] == ["GET", "GET"]


async def test_sync_copies_an_unassigned_fingerprint_too():
    """A template nobody claims is still the only copy of someone's finger."""
    hass, session, _, vault = build()
    session.add("GET", "/app/v1/users", body=[])
    session.add("GET", GET_A, body=template_reply(TEMPLATE_A))
    manager = VaultJobManager(hass)

    await manager.async_sync_from_scanner("e1", [REAL_APID_A])
    status = await run_to_completion(hass, manager)

    assert status["counts"]["ok"] == 1
    assert "unassigned" in status["items"][0]["label"]


async def test_sync_over_the_whole_scanner_uses_its_saved_list():
    hass, session, _, vault = build(on_scanner=[REAL_APID_A, REAL_APID_B])
    session.add("GET", "/app/v1/users", body=USERS)
    session.add("GET", GET_A, body=template_reply(TEMPLATE_A))
    session.add("GET", GET_B, body=template_reply(TEMPLATE_B))
    manager = VaultJobManager(hass)

    await manager.async_sync_from_scanner("e1")
    status = await run_to_completion(hass, manager)

    assert status["total"] == 2
    assert status["counts"]["ok"] == 2


async def test_sync_refuses_when_the_scanner_list_is_unknown():
    """Never guess. An unreadable list is not an empty one."""
    hass, session, _, _ = build(list_known=False)
    manager = VaultJobManager(hass)

    with pytest.raises(ValueError) as err:
        await manager.async_sync_from_scanner("e1")

    assert "could not be asked" in str(err.value)


async def test_sync_reports_a_bad_template_and_keeps_going():
    """One rotten blob must not cost the rest of the sweep."""
    hass, session, _, vault = build()
    session.add("GET", "/app/v1/users", body=USERS)
    session.add("GET", GET_A, body=template_reply('{"error":"nope"}'))
    session.add("GET", GET_B, body=template_reply(TEMPLATE_B))
    manager = VaultJobManager(hass)

    await manager.async_sync_from_scanner("e1", [REAL_APID_A, REAL_APID_B])
    status = await run_to_completion(hass, manager)

    assert status["counts"] == {"ok": 1, "skipped": 0, "failed": 1}
    assert REAL_APID_B in vault.data["records"]
    assert REAL_APID_A not in vault.data["records"]


# ------------------------------------------------------------------------ push


async def stocked(hass, vault, *, dev_variant=10):
    """Put template A in the database as Master's finger 7."""
    await vault.async_put(
        apid=REAL_APID_A, username="Master", finger=7,
        template=parse_template_hex(TEMPLATE_A), dev_variant=dev_variant,
        source_entry_id="e1",
    )


async def test_push_writes_the_template_then_the_assignment():
    """Order matters: the template first, so an assignment never names a finger
    the sensor does not hold."""
    hass, session, _, vault = build()
    await stocked(hass, vault)
    session.add("PUT", PUT_TEMPLATE, body=put_reply(REAL_APID_A))
    session.add("GET", "/app/v1/users", body=[])
    session.add("PUT", "/app/v1/users", body={"status": "saved"})
    manager = VaultJobManager(hass)

    await manager.async_push()
    status = await run_to_completion(hass, manager)

    assert status["counts"] == {"ok": 1, "skipped": 0, "failed": 0}
    methods = [(c["method"], c["path"]) for c in session.calls]
    assert methods[0] == ("PUT", PUT_TEMPLATE)
    assert ("PUT", "/app/v1/users") in methods

    written = session.last_json("PUT", "/app/v1/users")
    assert written[0]["username"] == "Master"
    assert written[0]["fingers"][0] == {
        "apid": REAL_APID_A, "enrolled_at": written[0]["fingers"][0]["enrolled_at"],
        "finger": 7,
    }


async def test_verified_false_is_never_counted_as_stored():
    """THE test. 200, rpc_error_code OK, and the scanner kept nothing."""
    hass, session, _, vault = build()
    await stocked(hass, vault)
    session.add("PUT", PUT_TEMPLATE, body=put_reply(REAL_APID_A, verified=False))
    manager = VaultJobManager(hass)

    await manager.async_push()
    status = await run_to_completion(hass, manager)

    assert status["counts"]["ok"] == 0
    assert status["items"][0]["state"] == STATE_SKIPPED
    assert status["items"][0]["reason"] == REASON_NOT_VERIFIED
    # And no assignment was written for a template that is not there.
    assert session.last_json("PUT", "/app/v1/users") is None


async def test_an_unconfirmed_write_is_retryable_but_a_refusal_is_not():
    """The verdict is the difference between "the scanner looked and said no" and
    "nobody ever confirmed". The first can never be made to work; the second is
    worth another go, so they must not both become the same kind of result."""
    hass, session, _, vault = build()
    await stocked(hass, vault)
    session.add("PUT", PUT_TEMPLATE, body=put_reply(
        REAL_APID_A, verified=False, verdict="transport_ack_only"))
    manager = VaultJobManager(hass)

    await manager.async_push()
    status = await run_to_completion(hass, manager)

    assert status["items"][0]["state"] == STATE_FAILED
    assert status["items"][0]["reason"] == REASON_NOT_VERIFIED


async def test_a_variant_mismatch_is_skipped_before_any_write():
    """Permanent, and only ekey can change it — so never a retryable failure, and
    never a wasted transfer."""
    hass, session, _, vault = build(dev_variant=20)
    await stocked(hass, vault, dev_variant=10)
    manager = VaultJobManager(hass)

    await manager.async_push()
    status = await run_to_completion(hass, manager)

    assert status["items"][0]["state"] == STATE_SKIPPED
    assert status["items"][0]["reason"] == REASON_VARIANT_MISMATCH
    assert "never be copied" in status["items"][0]["detail"]
    assert session.calls == []


async def test_a_full_sensor_is_a_retryable_failure():
    hass, session, _, vault = build()
    await stocked(hass, vault)
    session.add("PUT", PUT_TEMPLATE, body={
        "rpc_error_code": "Error", "rpc_error_code_value": 10,
        "error_message": "Maximum_feature_count_reached",
    })
    manager = VaultJobManager(hass)

    await manager.async_push()
    status = await run_to_completion(hass, manager)

    assert status["items"][0]["state"] == STATE_FAILED
    assert status["items"][0]["reason"] == REASON_SENSOR_FULL


async def test_a_template_that_landed_without_an_assignment_says_so():
    """The finger already opens that door — retrying the write would be pointless,
    and pretending it failed would hide a working fingerprint."""
    hass, session, _, vault = build()
    await stocked(hass, vault)
    session.add("PUT", PUT_TEMPLATE, body=put_reply(REAL_APID_A))
    session.add("GET", "/app/v1/users", status=500, body={"error": "boom"})
    manager = VaultJobManager(hass)

    await manager.async_push()
    status = await run_to_completion(hass, manager)

    assert status["items"][0]["state"] == STATE_FAILED
    assert status["items"][0]["reason"] == REASON_TEMPLATE_ONLY
    assert "already" in status["items"][0]["detail"] or "works" in status["items"][0]["detail"]


async def test_an_oversized_user_document_is_refused_before_the_write():
    """The backend replaces the whole document and caps the body, so this has to be
    caught here rather than discovered as a rejection."""
    hass, session, _, vault = build()
    await stocked(hass, vault)
    fat = [
        {"id": f"u{n}", "username": "x" * 200, "fingers": [
            {"finger": f, "apid": REAL_APID_B, "enrolled_at": 1} for f in range(1, 11)
        ]}
        for n in range(30)
    ]
    session.add("PUT", PUT_TEMPLATE, body=put_reply(REAL_APID_A))
    session.add("GET", "/app/v1/users", body=fat)
    manager = VaultJobManager(hass)

    await manager.async_push()
    status = await run_to_completion(hass, manager)

    assert status["items"][0]["reason"] == REASON_USERS_DOC_TOO_LARGE
    assert "limit" in status["items"][0]["detail"]
    assert session.last_json("PUT", "/app/v1/users") is None


async def test_push_skips_a_scanner_whose_list_is_unknown():
    """Unknown is not missing, so there is nothing to push there yet."""
    hass, session, _, vault = build(list_known=False)
    await stocked(hass, vault)
    manager = VaultJobManager(hass)

    await manager.async_push()
    status = await run_to_completion(hass, manager)

    assert status["total"] == 0
    assert session.calls == []


async def test_push_skips_a_scanner_that_already_has_it():
    hass, session, _, vault = build(on_scanner=[REAL_APID_A])
    await stocked(hass, vault)
    manager = VaultJobManager(hass)

    await manager.async_push()
    status = await run_to_completion(hass, manager)

    assert status["total"] == 0


async def test_push_ignores_a_record_with_no_template():
    """It can name a finger but repair nothing."""
    hass, session, _, vault = build()
    await vault.async_put(apid=REAL_APID_A, username="Master", finger=7)
    manager = VaultJobManager(hass)

    await manager.async_push()
    status = await run_to_completion(hass, manager)

    assert status["total"] == 0


async def test_push_ignores_a_superseded_record():
    """A re-enrolled finger's old template must not be pushed back out."""
    hass, session, _, vault = build()
    await stocked(hass, vault)
    await vault.async_put(
        apid=REAL_APID_B, username="Master", finger=7,
        template=parse_template_hex(TEMPLATE_B), dev_variant=10,
    )
    manager = VaultJobManager(hass)

    await manager.async_push()
    status = await run_to_completion(hass, manager)

    assert status["total"] == 1  # only the new one


async def test_push_reaches_every_loaded_scanner():
    hass, session, _, vault = build(entry_ids=("e1", "e2"))
    await stocked(hass, vault)
    session.add("PUT", PUT_TEMPLATE, body=put_reply(REAL_APID_A))
    session.add("GET", "/app/v1/users", body=[])
    session.add("PUT", "/app/v1/users", body={"status": "saved"})
    manager = VaultJobManager(hass)

    await manager.async_push()
    status = await run_to_completion(hass, manager)

    assert status["total"] == 2
    assert {i["entry_id"] for i in status["items"]} == {"e1", "e2"}


# ------------------------------------------------------------------- reporting


async def test_a_job_is_only_ok_when_nothing_was_skipped_or_failed():
    hass, session, _, vault = build(entry_ids=("e1", "e2"), dev_variant=20)
    await stocked(hass, vault, dev_variant=10)
    manager = VaultJobManager(hass)

    await manager.async_push()
    status = await run_to_completion(hass, manager)

    assert status["counts"]["skipped"] == 2
    assert status["ok"] is False


async def test_the_terminal_event_carries_every_item():
    """So the final report is right even if progress events were missed."""
    hass, session, bus, vault = build()
    await stocked(hass, vault)
    session.add("PUT", PUT_TEMPLATE, body=put_reply(REAL_APID_A))
    session.add("GET", "/app/v1/users", body=[])
    session.add("PUT", "/app/v1/users", body={"status": "saved"})
    manager = VaultJobManager(hass)

    await manager.async_push()
    await run_to_completion(hass, manager)

    final = job_events(bus)[-1]
    assert final["done"] is True
    assert len(final["items"]) == 1
    assert all(e["items"] is None for e in job_events(bus)[:-1])


async def test_progress_events_carry_no_entry_id():
    """A None passes the panel's scanner-scoped subscription filter, which is what
    keeps a job visible whichever view is open."""
    hass, session, bus, vault = build()
    await stocked(hass, vault)
    session.add("PUT", PUT_TEMPLATE, body=put_reply(REAL_APID_A))
    session.add("GET", "/app/v1/users", body=[])
    session.add("PUT", "/app/v1/users", body={"status": "saved"})
    manager = VaultJobManager(hass)

    await manager.async_push()
    await run_to_completion(hass, manager)

    assert all(e["entry_id"] is None for e in job_events(bus))
    assert all(e["item"] is None or e["item"]["scanner"] for e in job_events(bus))


async def test_counts_are_absolute_so_a_dropped_event_costs_nothing():
    hass, session, bus, vault = build(entry_ids=("e1", "e2"))
    await stocked(hass, vault)
    session.add("PUT", PUT_TEMPLATE, body=put_reply(REAL_APID_A))
    session.add("GET", "/app/v1/users", body=[])
    session.add("PUT", "/app/v1/users", body={"status": "saved"})
    manager = VaultJobManager(hass)

    await manager.async_push()
    await run_to_completion(hass, manager)

    indexes = [e["index"] for e in job_events(bus)]
    assert indexes == sorted(indexes)
    assert job_events(bus)[-1]["index"] == 2


async def test_status_survives_for_a_panel_that_loads_after_the_job():
    hass, session, _, vault = build()
    session.add("GET", "/app/v1/users", body=USERS)
    session.add("GET", GET_A, body=template_reply(TEMPLATE_A))
    manager = VaultJobManager(hass)

    await manager.async_sync_from_scanner("e1", [REAL_APID_A])
    await run_to_completion(hass, manager)

    assert manager.status()["done"] is True
    assert manager.running is False


# ------------------------------------------------------------------ cancelling


async def test_cancelling_stops_after_the_current_item_and_keeps_what_was_done():
    """Not a task cancellation: a transfer already in flight has to land, because a
    partially written template is the worst available outcome."""
    hass, session, _, vault = build(on_scanner=[REAL_APID_A, REAL_APID_B])
    session.add("GET", "/app/v1/users", body=USERS)
    session.add("GET", GET_A, body=template_reply(TEMPLATE_A))
    session.add("GET", GET_B, body=template_reply(TEMPLATE_B))
    manager = VaultJobManager(hass)

    await manager.async_sync_from_scanner("e1")
    manager.async_cancel()
    status = await run_to_completion(hass, manager)

    assert status["cancelled"] is True
    assert status["counts"]["ok"] < 2
    assert "Stopped" in status["message"]


async def test_cancelling_an_unknown_job_id_does_nothing():
    hass, session, _, vault = build()
    session.add("GET", "/app/v1/users", body=USERS)
    session.add("GET", GET_A, body=template_reply(TEMPLATE_A))
    manager = VaultJobManager(hass)

    await manager.async_sync_from_scanner("e1", [REAL_APID_A])
    assert manager.async_cancel("not-this-job") is False
    await run_to_completion(hass, manager)


async def test_a_backend_without_the_template_routes_is_remembered():
    """Learned from use, not probed: there is no capability flag for these routes,
    and probing every scanner on every page load would cost a round trip each for
    an answer that only matters when somebody actually transfers something."""
    from custom_components.ekey_ha_app.jobs import TEMPLATE_API_KEY

    hass, session, _, _ = build()
    session.add("GET", "/app/v1/users", body=USERS)
    session.add("GET", GET_A, status=501, body={"error": "not implemented"})
    manager = VaultJobManager(hass)

    await manager.async_sync_from_scanner("e1", [REAL_APID_A])
    status = await run_to_completion(hass, manager)

    assert status["items"][0]["reason"] == "no_template_api"
    assert hass.data[DOMAIN]["e1"][TEMPLATE_API_KEY] is False


async def test_a_working_backend_is_remembered_too():
    from custom_components.ekey_ha_app.jobs import TEMPLATE_API_KEY

    hass, session, _, _ = build()
    session.add("GET", "/app/v1/users", body=USERS)
    session.add("GET", GET_A, body=template_reply(TEMPLATE_A))
    manager = VaultJobManager(hass)

    await manager.async_sync_from_scanner("e1", [REAL_APID_A])
    await run_to_completion(hass, manager)

    assert hass.data[DOMAIN]["e1"][TEMPLATE_API_KEY] is True


async def test_cancelling_when_nothing_runs_is_not_an_error():
    hass, _, _, _ = build()
    assert VaultJobManager(hass).async_cancel() is False
