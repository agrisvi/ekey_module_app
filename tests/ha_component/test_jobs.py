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
    REASON_ENROLL_FAILED,
    REASON_LIST_UNKNOWN,
    REASON_STILL_PRESENT,
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

from .fake_http import FakeResponse, FakeSession
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


# ------------------------------------------------- asking before writing


def _answers_with(hass, entry_id, aps, *, known=True, success=True):
    """Make a refresh replace that scanner's cached picture with a different one."""
    app = hass.data[DOMAIN][entry_id]["app_coordinator"]

    async def refresh():
        app.data = {"scanner_list_known": known, "scanner_aps": list(aps)}
        app.last_update_success = success

    app.async_refresh_now = refresh
    return app


async def test_the_push_asks_the_scanners_before_deciding_what_is_missing():
    """The reported failure, in one test.

    The coordinator polls every five minutes, and the push read exactly that cache
    to decide where to write. A fingerprint deleted from a scanner inside that
    window still looked present, so the push skipped that door and reported a clean
    run — the copy went to one scanner and nobody was told the other was missed.
    """
    hass, session, _, vault = build(on_scanner=[REAL_APID_A])  # the stale picture
    await stocked(hass, vault)
    _answers_with(hass, "e1", [])  # what the scanner actually holds now
    session.add("PUT", PUT_TEMPLATE, body=put_reply(REAL_APID_A))
    session.add("GET", "/app/v1/users", body=[])
    session.add("PUT", "/app/v1/users", body={"status": "saved"})
    manager = VaultJobManager(hass)

    status = await manager.async_push()
    assert status["total"] == 1, "the stale cache said this scanner already had it"

    status = await run_to_completion(hass, manager)
    assert status["counts"] == {"ok": 1, "skipped": 0, "failed": 0}


async def test_a_scanner_that_has_it_already_is_still_left_alone():
    """The refresh must not turn into "write to everything regardless"."""
    hass, session, _, vault = build(on_scanner=[])  # the stale picture said missing
    await stocked(hass, vault)
    _answers_with(hass, "e1", [REAL_APID_A])  # it has it after all
    manager = VaultJobManager(hass)

    status = await manager.async_push()

    assert status["total"] == 0
    assert session.calls == []


async def test_a_refresh_that_fails_makes_the_list_unknown_not_stale():
    """A scanner that goes quiet keeps its last data — which must not be trusted.

    ``async_refresh`` swallows the failure and leaves the previous snapshot in
    place, so without the ``last_update_success`` check a scanner that dropped off
    the bus would keep answering with the list it held minutes ago, and a push
    would write against it.
    """
    hass, session, _, vault = build(on_scanner=[])
    await stocked(hass, vault)
    _answers_with(hass, "e1", [], success=False)
    manager = VaultJobManager(hass)

    status = await manager.async_push()

    assert status["total"] == 0, "unknown is not missing"
    assert session.calls == []


async def test_a_second_job_is_refused_before_the_refresh_not_after():
    """The busy check has to come first.

    Asking every scanner what it holds is several RS-485 round trips, and each
    await hands control back to the loop. A check that only happened once the work
    was known left a window seconds wide in which two clicks both got through.
    """
    hass, session, _, vault = build(on_scanner=[])
    await stocked(hass, vault)
    started = asyncio.Event()
    release = asyncio.Event()
    app = hass.data[DOMAIN]["e1"]["app_coordinator"]

    async def slow_refresh():
        started.set()
        await release.wait()

    app.async_refresh_now = slow_refresh
    manager = VaultJobManager(hass)

    first = asyncio.ensure_future(manager.async_push())
    await started.wait()  # inside the refresh, before any job exists

    with pytest.raises(JobBusy):
        await manager.async_push()

    release.set()
    await first
    await run_to_completion(hass, manager)


async def test_sync_from_a_scanner_reads_the_list_first():
    """Copying a list nobody re-read is copying what it looked like minutes ago."""
    hass, session, _, _ = build(on_scanner=[])
    _answers_with(hass, "e1", [REAL_APID_A])
    session.add("GET", "/app/v1/users", body=USERS)
    session.add("GET", GET_A, body=template_reply(TEMPLATE_A))
    manager = VaultJobManager(hass)

    status = await manager.async_sync_from_scanner("e1")

    assert status["total"] == 1, "the stale list was empty"


# ------------------------------------------------- deleting everywhere (phase 2)
#
# The ordering here is the one this project has already got wrong once: a record
# dropped before every scanner confirmed the fingerprint is gone leaves a finger
# that still opens a door with nothing in Home Assistant naming it. Every test
# below is about refusing to drop the record.

DELETE_A = f"/api/v1/fingerprints/{REAL_APID_A}"
LIST = "/api/v1/fingerprints"


async def test_a_purge_deletes_everywhere_then_the_record_last():
    hass, session, _, vault = build(on_scanner=[REAL_APID_A])
    await stocked(hass, vault)
    session.add("DELETE", DELETE_A, body={"rpc_error_code": "OK"})
    session.add("GET", LIST, body={"aps": []})            # confirmed gone
    session.add("GET", "/app/v1/users", body=USERS)
    session.add("PUT", "/app/v1/users", body={"status": "saved"})
    manager = VaultJobManager(hass)

    await manager.async_purge_fingerprint(REAL_APID_A)
    status = await run_to_completion(hass, manager)

    methods = [(c["method"], c["path"]) for c in session.calls]
    assert methods[0] == ("DELETE", DELETE_A)
    assert methods[1] == ("GET", LIST), "absence is confirmed by re-reading, not assumed"
    assert REAL_APID_A not in vault.data["records"], "and only then is the record dropped"
    assert status["counts"]["failed"] == 0

    # The user document lost that finger too — a user holding a fingerprint that no
    # longer exists is the same confusion in reverse. Only that one: the user's
    # other finger has nothing to do with this delete, and a purge that took the
    # whole list with it would be far worse than the bug it fixes.
    written = session.last_json("PUT", "/app/v1/users")
    apids = [f["apid"] for u in written for f in u["fingers"]]
    assert REAL_APID_A not in apids
    assert REAL_APID_B in apids, "the user's other finger is untouched"
    assert len(written) == len(USERS), "and no user disappeared"


async def test_a_scanner_that_still_lists_it_keeps_the_record():
    """THE test. The delete answered 200 and the sensor kept the fingerprint."""
    hass, session, _, vault = build(on_scanner=[REAL_APID_A])
    await stocked(hass, vault)
    session.add("DELETE", DELETE_A, body={"rpc_error_code": "OK"})
    session.add("GET", LIST, body={"aps": [REAL_APID_A]})   # still there
    manager = VaultJobManager(hass)

    await manager.async_purge_fingerprint(REAL_APID_A)
    status = await run_to_completion(hass, manager)

    assert REAL_APID_A in vault.data["records"], "the record must survive"
    item = status["items"][0]
    assert item["state"] == STATE_FAILED
    assert item["reason"] == REASON_STILL_PRESENT
    assert "still opens that door" in item["detail"]
    assert session.last_json("PUT", "/app/v1/users") is None


async def test_an_unreadable_scanner_blocks_the_record_removal():
    """Unknown is not gone. A scanner that cannot be asked cannot confirm."""
    hass, session, _, vault = build(list_known=False)
    await stocked(hass, vault)
    manager = VaultJobManager(hass)

    await manager.async_purge_fingerprint(REAL_APID_A)
    status = await run_to_completion(hass, manager)

    assert REAL_APID_A in vault.data["records"]
    assert status["items"][0]["reason"] == REASON_LIST_UNKNOWN
    assert session.calls == [], "nothing is deleted against a list we could not read"


async def test_one_scanner_confirming_is_not_enough_for_two():
    """The record goes only when EVERY scanner has confirmed."""
    hass, session, _, vault = build(on_scanner=[REAL_APID_A], entry_ids=("e1", "e2"))
    await stocked(hass, vault)
    # e1 and e2 share one FakeSession, so this is the pair of exchanges in order:
    # e1 deletes and confirms; e2 deletes and still lists it.
    session.add_sequence("GET", LIST, [
        FakeResponse(200, {"aps": []}),
        FakeResponse(200, {"aps": [REAL_APID_A]}),
    ])
    session.add("DELETE", DELETE_A, body={"rpc_error_code": "OK"})
    session.add("GET", "/app/v1/users", body=[])
    manager = VaultJobManager(hass)

    await manager.async_purge_fingerprint(REAL_APID_A)
    status = await run_to_completion(hass, manager)

    assert REAL_APID_A in vault.data["records"], "one holdout keeps the record"
    states = [i["state"] for i in status["items"]]
    assert STATE_OK in states and STATE_FAILED in states
    assert status["items"][-1]["detail"].startswith("kept")


async def test_a_scanner_that_never_had_it_is_not_an_error():
    hass, session, _, vault = build(on_scanner=[])
    await stocked(hass, vault)
    session.add("GET", "/app/v1/users", body=[])
    manager = VaultJobManager(hass)

    await manager.async_purge_fingerprint(REAL_APID_A)
    status = await run_to_completion(hass, manager)

    assert status["items"][0]["state"] == STATE_OK
    assert status["items"][0]["detail"] == "was not on this scanner"
    assert REAL_APID_A not in vault.data["records"], "nothing holds it, so it can go"
    assert not any(c["method"] == "DELETE" for c in session.calls)


async def test_the_record_is_the_last_item_reported():
    """So the report reads in the order the work happened, database last."""
    hass, session, _, vault = build(on_scanner=[])
    await stocked(hass, vault)
    session.add("GET", "/app/v1/users", body=[])
    manager = VaultJobManager(hass)

    await manager.async_purge_fingerprint(REAL_APID_A)
    status = await run_to_completion(hass, manager)

    assert status["items"][-1]["scanner"] is None
    assert status["items"][-1]["detail"] == "removed from the database"


# ------------------------------------------------- enrol and fan out (phase 2)


class FakeEnrollManager:
    """The real one talks to a sensor and waits for a finger. This fires the same
    bus event the real one fires, which is the whole contract the job depends on."""

    def __init__(self, hass, entry_id, *, apid, ok=True, message="Enrolled.",
                 start_error=None):
        self.hass = hass
        self.entry_id = entry_id
        self.apid = apid
        self.ok = ok
        self.message = message
        self.start_error = start_error
        self.cancelled = []

    async def async_start(self, user_id, finger):
        if self.start_error:
            raise self.start_error
        self.hass.bus.async_fire("ekey_enroll_progress", {
            "entry_id": self.entry_id, "apid": self.apid, "done": False,
            "message": "Place the finger…",
        })
        # The terminal event, as the real manager fires it from _succeed().
        self.hass.bus.async_fire("ekey_enroll_progress", {
            "entry_id": self.entry_id, "apid": self.apid, "username": "Master",
            "finger": finger, "done": True, "ok": self.ok, "message": self.message,
        })
        return self.apid

    async def async_cancel(self, apid):
        self.cancelled.append(apid)


class ListenableBus(FakeBus):
    """FakeBus plus real listener dispatch, which the enroll job needs."""

    def __init__(self):
        super().__init__()
        self._listeners = {}
        self.listened = []

    def async_listen(self, event_type, callback):
        self.listened.append((event_type, callback))
        self._listeners.setdefault(event_type, []).append(callback)

        def _unsub():
            self._listeners[event_type].remove(callback)

        return _unsub

    def async_fire(self, event_type, data=None):
        super().async_fire(event_type, data)
        for cb in list(self._listeners.get(event_type, [])):
            cb(SimpleNamespace(data=data or {}, event_type=event_type))


def with_enroll(hass, entry_id="e1", **kwargs):
    bus = ListenableBus()
    hass.bus = bus
    manager = FakeEnrollManager(hass, entry_id, **kwargs)
    hass.data[DOMAIN][entry_id]["enroll_manager"] = manager
    return manager


async def test_enrolling_from_storage_copies_to_every_other_scanner():
    """One presentation of a finger, one APID, every door — the whole point."""
    hass, session, _, vault = build(on_scanner=[], entry_ids=("e1", "e2"))
    with_enroll(hass, apid=REAL_APID_A)
    # enroll.py's own capture ran inside the real _succeed; here the job's fallback
    # capture reads the template off the scanner it was enrolled on.
    session.add("GET", GET_A, body=template_reply(TEMPLATE_A))
    session.add("PUT", PUT_TEMPLATE, body=put_reply(REAL_APID_A))
    session.add("GET", "/app/v1/users", body=[])
    session.add("PUT", "/app/v1/users", body={"status": "saved"})
    manager = VaultJobManager(hass)

    status = await manager.async_enroll("e1", "u1", 7)
    assert status["total"] == 2, "the enrollment itself, plus one other scanner"

    status = await run_to_completion(hass, manager)

    assert REAL_APID_A in vault.data["records"], "the database has the copy"
    assert status["counts"] == {"ok": 2, "skipped": 0, "failed": 0}
    assert status["items"][0]["detail"] == "enrolled and copied into the database"
    assert status["items"][1]["scanner"] == "Scanner e2"
    assert "copied to all 1 other scanner" in status["message"]


async def test_a_failed_enrollment_copies_nothing():
    hass, session, _, vault = build(on_scanner=[], entry_ids=("e1", "e2"))
    with_enroll(hass, apid=REAL_APID_A, ok=False, message="Timed out.")
    manager = VaultJobManager(hass)

    await manager.async_enroll("e1", "u1", 7)
    status = await run_to_completion(hass, manager)

    assert status["items"][0]["state"] == STATE_FAILED
    assert status["items"][0]["reason"] == REASON_ENROLL_FAILED
    assert session.calls == [], "no template was read and nothing was written"
    assert REAL_APID_A not in vault.data["records"]


async def test_an_enrollment_that_cannot_be_captured_does_not_fan_out():
    """The finger works on the scanner it was enrolled on, and the report says the
    database has no copy — rather than reporting a clean run that copied nothing."""
    hass, session, _, vault = build(on_scanner=[], entry_ids=("e1", "e2"))
    with_enroll(hass, apid=REAL_APID_A)
    session.add("GET", GET_A, status=404, body={"error": "no such route"})
    manager = VaultJobManager(hass)

    await manager.async_enroll("e1", "u1", 7)
    status = await run_to_completion(hass, manager)

    assert status["items"][0]["state"] == STATE_FAILED
    assert "the finger is enrolled and works" in status["items"][0]["detail"]
    assert not any(c["method"] == "PUT" for c in session.calls)
    assert "no copy" in status["message"]


async def test_an_enrollment_that_will_not_start_is_reported_as_such():
    hass, session, _, _ = build(entry_ids=("e1",))
    with_enroll(hass, apid=REAL_APID_A, start_error=RuntimeError("sensor busy"))
    manager = VaultJobManager(hass)

    await manager.async_enroll("e1", "u1", 7)
    status = await run_to_completion(hass, manager)

    assert status["items"][0]["reason"] == REASON_ENROLL_FAILED
    assert "sensor busy" in status["items"][0]["detail"]


async def test_a_scanner_without_an_app_layer_cannot_enrol():
    hass, _, _, _ = build(entry_ids=("e1",))
    hass.bus = ListenableBus()
    hass.data[DOMAIN]["e1"].pop("enroll_manager", None)
    manager = VaultJobManager(hass)

    with pytest.raises(jobs_mod.UnknownScannerJob):
        await manager.async_enroll("e1", "u1", 7)


async def test_the_progress_listener_is_a_home_assistant_callback():
    """Not cosmetic. Home Assistant decides thread-vs-loop from the function it is
    handed, and an unmarked listener is dispatched to a worker thread — where both
    hass.bus.async_fire and asyncio.Event.set are illegal. In the field that raised
    inside the listener before it could record the terminal state, so a successful
    enrollment was reported as a 300-second timeout and nothing was captured."""
    hass, session, _, vault = build(on_scanner=[], entry_ids=("e1",))
    bus = ListenableBus()
    hass.bus = bus
    with_enroll(hass, apid=REAL_APID_A)
    hass.bus = bus                      # with_enroll swaps in its own; keep this one
    manager_obj = FakeEnrollManager(hass, "e1", apid=REAL_APID_A)
    hass.data[DOMAIN]["e1"]["enroll_manager"] = manager_obj
    session.add("GET", GET_A, body=template_reply(TEMPLATE_A))
    manager = VaultJobManager(hass)

    await manager.async_enroll("e1", "u1", 7)
    await run_to_completion(hass, manager)

    listeners = [cb for name, cb in bus.listened if name == "ekey_enroll_progress"]
    assert listeners, "the job listened for enrollment progress"
    assert all(getattr(cb, "_hass_callback", False) for cb in listeners), (
        "every enrollment-progress listener must be marked @callback"
    )


async def test_a_progress_relay_failure_does_not_strand_the_job():
    """The terminal state is recorded before anything else can fail."""
    hass, session, _, vault = build(on_scanner=[], entry_ids=("e1",))
    with_enroll(hass, apid=REAL_APID_A)
    session.add("GET", GET_A, body=template_reply(TEMPLATE_A))
    manager = VaultJobManager(hass)
    real_emit = manager._emit

    def exploding_emit(job, item=None):
        # Only the relayed progress line — the same failure the un-marked listener
        # produced in the field, and nothing else.
        if job.message == "Place the finger…":
            raise RuntimeError("bus is unhappy")
        return real_emit(job, item)

    manager._emit = exploding_emit

    await manager.async_enroll("e1", "u1", 7)
    status = await run_to_completion(hass, manager)

    assert status["done"], "the job still finished"
    assert REAL_APID_A in vault.data["records"], "and still captured the template"


async def test_the_fan_out_uses_the_same_write_path_as_a_push():
    """A variant mismatch is a skip in both, not a failure — one set of rules about
    when a door is considered to have a fingerprint, not two."""
    hass, session, _, vault = build(on_scanner=[], entry_ids=("e1", "e2"))
    with_enroll(hass, apid=REAL_APID_A)
    session.add("GET", GET_A, body=template_reply(TEMPLATE_A))
    hass.data[DOMAIN]["e2"]["coordinator"] = SimpleNamespace(
        data={"device": {"dev_variant": 20, "dev_sub_variant": 10, "prod_sn": "x"}}
    )
    manager = VaultJobManager(hass)

    await manager.async_enroll("e1", "u1", 7)
    status = await run_to_completion(hass, manager)

    assert status["items"][1]["state"] == STATE_SKIPPED
    assert status["items"][1]["reason"] == REASON_VARIANT_MISMATCH
    assert not any(c["method"] == "PUT" for c in session.calls)
