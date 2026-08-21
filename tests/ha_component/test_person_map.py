"""Tests for the person ↔ app-user reconcile.

This is the riskiest code in the change: it runs once against a live installation
and rewrites the document that decides who can open a door. The properties worth
proving are therefore not "it produces output" but:

* running it **twice changes nothing** the second time;
* an APID the backend already knows **wins** over the legacy map;
* an **occupied finger slot is never overwritten**;
* one person is **never linked to two users**;
* a conflict **writes nothing** and is reported instead of guessed.

``build_reconcile_plan`` is pure — no HA, no network, no clock — which is what
makes all of that directly testable.
"""
from custom_components.ekey_ha_app.person_map import (
    EkeyPersonStore,
    as_person_map,
    build_reconcile_plan,
    find_user_by_apid,
    user_person,
)

IDS = iter([])


def counter_ids():
    """Deterministic ids so plans can be compared."""
    state = {"n": 0}

    def make() -> str:
        state["n"] += 1
        return f"new-{state['n']}"

    return make


LEGACY_ONE = {"person.jane": {"fingerprints": {"1": "apid-a", "2": "apid-b"}}}
NAMES = {"person.jane": "Jane Doe"}


# ------------------------------------------------------------------ helpers


def test_user_person_ignores_junk():
    assert user_person({"ha_person": "person.x"}) == "person.x"
    assert user_person({"ha_person": "light.kitchen"}) is None
    assert user_person({"ha_person": 42}) is None
    assert user_person({}) is None


def test_find_user_by_apid():
    users = [
        {"id": "u1", "fingers": [{"apid": "a", "finger": 1}]},
        {"id": "u2", "fingers": [{"apid": "b", "finger": 3}]},
    ]
    assert find_user_by_apid(users, "b")["id"] == "u2"
    assert find_user_by_apid(users, "zz") is None


def test_as_person_map_renders_the_legacy_shape():
    users = [
        {
            "id": "u1",
            "username": "Jane",
            "ha_person": "person.jane",
            "fingers": [{"apid": "a", "finger": 1}, {"apid": "b", "finger": 4}],
        },
        {"id": "u2", "username": "Nobody", "fingers": [{"apid": "c", "finger": 1}]},
    ]
    rendered = as_person_map(users)
    assert rendered == {"person.jane": {"fingerprints": {"1": "a", "4": "b"}}}
    # A user with no linked person contributes nothing — there is no person to key it on.
    assert "u2" not in rendered


# ------------------------------------------------------------ empty backend


def test_creates_a_user_and_attaches_both_fingers():
    plan = build_reconcile_plan(LEGACY_ONE, [], NAMES, new_id=counter_ids())
    assert plan.created == ["Jane Doe"]
    assert sorted(plan.attached) == [
        ("Jane Doe", 1, "apid-a"),
        ("Jane Doe", 2, "apid-b"),
    ]
    assert plan.conflicts == []
    assert plan.changed is True

    user = plan.users[0]
    assert user["username"] == "Jane Doe"
    assert user["ha_person"] == "person.jane"
    assert {f["finger"] for f in user["fingers"]} == {1, 2}
    # No timestamp is invented for a migrated fingerprint.
    assert all("enrolled_at" not in f for f in user["fingers"])


def test_person_name_falls_back_to_the_entity_slug():
    plan = build_reconcile_plan(LEGACY_ONE, [], {}, new_id=counter_ids())
    assert plan.created == ["jane"]


# ---------------------------------------------------------------- idempotence


def test_running_twice_is_a_no_op_the_second_time():
    first = build_reconcile_plan(LEGACY_ONE, [], NAMES, new_id=counter_ids())
    second = build_reconcile_plan(LEGACY_ONE, first.users, NAMES, new_id=counter_ids())
    assert second.created == []
    assert second.attached == []
    assert second.linked == []
    assert second.conflicts == []
    assert second.changed is False
    assert second.users == first.users


def test_idempotent_even_without_the_guard_flag():
    """A lost 'already migrated' flag must not duplicate anything."""
    plan = build_reconcile_plan(LEGACY_ONE, [], NAMES, new_id=counter_ids())
    for _ in range(3):
        plan = build_reconcile_plan(LEGACY_ONE, plan.users, NAMES, new_id=counter_ids())
    assert len(plan.users) == 1
    assert len(plan.users[0]["fingers"]) == 2


# ------------------------------------------------------- the backend wins


def test_apid_already_on_a_backend_user_only_gains_the_person_link():
    backend = [
        {"id": "u1", "username": "Enrolled On Device",
         "fingers": [{"apid": "apid-a", "finger": 7}]}
    ]
    plan = build_reconcile_plan(
        {"person.jane": {"fingerprints": {"1": "apid-a"}}}, backend, NAMES,
        new_id=counter_ids(),
    )
    assert plan.created == []
    assert plan.attached == []
    assert plan.linked == [("Enrolled On Device", "person.jane")]
    # The slot the BACKEND recorded is kept; the legacy map does not move it.
    assert plan.users[0]["fingers"] == [{"apid": "apid-a", "finger": 7}]


def test_apid_on_a_user_linked_to_someone_else_is_a_conflict():
    backend = [
        {"id": "u1", "username": "Bob", "ha_person": "person.bob",
         "fingers": [{"apid": "apid-a", "finger": 1}]}
    ]
    plan = build_reconcile_plan(
        {"person.jane": {"fingerprints": {"1": "apid-a"}}}, backend, NAMES,
        new_id=counter_ids(),
    )
    assert plan.changed is False
    assert len(plan.conflicts) == 1
    assert "person.bob" in plan.conflicts[0] or "person.jane" in plan.conflicts[0]
    # Nothing was rewritten.
    assert plan.users[0]["ha_person"] == "person.bob"


# ------------------------------------------------------- slots and identities


def test_an_occupied_slot_is_never_overwritten():
    backend = [
        {"id": "u1", "username": "Jane Doe", "ha_person": "person.jane",
         "fingers": [{"apid": "already-here", "finger": 1}]}
    ]
    plan = build_reconcile_plan(
        {"person.jane": {"fingerprints": {"1": "apid-a"}}}, backend, NAMES,
        new_id=counter_ids(),
    )
    assert plan.attached == []
    assert len(plan.conflicts) == 1
    assert "finger 1" in plan.conflicts[0]
    assert plan.users[0]["fingers"] == [{"apid": "already-here", "finger": 1}]


def test_an_existing_user_with_a_matching_name_is_linked_not_duplicated():
    backend = [{"id": "u1", "username": "Jane Doe", "fingers": []}]
    plan = build_reconcile_plan(LEGACY_ONE, backend, NAMES, new_id=counter_ids())
    assert plan.created == []
    assert plan.linked == [("Jane Doe", "person.jane")]
    assert len(plan.users) == 1
    assert {f["finger"] for f in plan.users[0]["fingers"]} == {1, 2}


def test_two_people_get_two_users():
    legacy = {
        "person.jane": {"fingerprints": {"1": "a"}},
        "person.bob": {"fingerprints": {"1": "b"}},
    }
    plan = build_reconcile_plan(
        legacy, [], {"person.jane": "Jane", "person.bob": "Bob"}, new_id=counter_ids()
    )
    assert sorted(plan.created) == ["Bob", "Jane"]
    assert len(plan.users) == 2
    assert {u["ha_person"] for u in plan.users} == {"person.jane", "person.bob"}


def test_a_person_already_linked_is_not_linked_again_to_another_user():
    backend = [
        {"id": "u1", "username": "Jane Doe", "ha_person": "person.jane", "fingers": []},
        {"id": "u2", "username": "Other", "fingers": [{"apid": "apid-a", "finger": 5}]},
    ]
    plan = build_reconcile_plan(
        {"person.jane": {"fingerprints": {"1": "apid-a"}}}, backend, NAMES,
        new_id=counter_ids(),
    )
    # apid-a belongs to "Other", but person.jane is already u1 — refuse, do not merge.
    assert plan.linked == []
    assert len(plan.conflicts) == 1
    assert plan.users[1].get("ha_person") is None


# ------------------------------------------------------------- malformed input


def test_a_non_numeric_finger_slot_is_reported_not_crashed():
    plan = build_reconcile_plan(
        {"person.jane": {"fingerprints": {"thumb": "apid-a"}}}, [], NAMES,
        new_id=counter_ids(),
    )
    assert plan.attached == []
    assert len(plan.conflicts) == 1
    assert "thumb" in plan.conflicts[0]


def test_junk_legacy_entries_are_skipped():
    legacy = {
        "person.jane": "not a dict",
        "person.bob": {"fingerprints": "not a dict"},
        "person.ok": {"fingerprints": {"1": "apid-a"}},
    }
    plan = build_reconcile_plan(legacy, [], {}, new_id=counter_ids())
    assert plan.created == ["ok"]


def test_the_input_users_are_not_mutated():
    backend = [{"id": "u1", "username": "Jane Doe", "fingers": []}]
    snapshot = [{"id": "u1", "username": "Jane Doe", "fingers": []}]
    build_reconcile_plan(LEGACY_ONE, backend, NAMES, new_id=counter_ids())
    assert backend == snapshot


# ------------------------------------------------------------------ migration


async def test_store_migration_keeps_v1_verbatim():
    """The v1 map must survive untouched — it is the recovery path."""
    store = EkeyPersonStore.__new__(EkeyPersonStore)  # no HA needed for the hook
    v1 = {"person.jane": {"fingerprints": {"1": "apid-a"}}}
    migrated = await store._async_migrate_func(1, 1, v1)
    assert migrated["legacy"] == v1
    assert migrated["migrated_to_backend"] == {}


async def test_store_migration_handles_empty_v1():
    store = EkeyPersonStore.__new__(EkeyPersonStore)
    migrated = await store._async_migrate_func(1, 1, None)
    assert migrated == {"legacy": {}, "migrated_to_backend": {}}


async def test_store_migration_refuses_a_newer_schema():
    store = EkeyPersonStore.__new__(EkeyPersonStore)
    try:
        await store._async_migrate_func(3, 1, {})
    except NotImplementedError:
        return
    raise AssertionError("a future schema version must not be silently accepted")
