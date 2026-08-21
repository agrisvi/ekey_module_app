"""Tests for the removal of entities earlier versions created.

Three selects and two buttons existed only to build a user interface out of
entities. They are gone, but Home Assistant keeps a registry row for every entity it
has ever seen, and a row whose platform no longer creates it renders as
"unavailable" — inside the user's dashboards and automation editors, not just on the
device page. So the integration deletes those rows itself on the next setup.

What is worth proving here is not that the function calls a registry, but:

* every retired unique_id is **actually asked for**, spelled exactly as the class
  that used to create it spelled it — a typo leaves the ghost behind forever and
  nothing else in the code would notice;
* a row that does not exist is **left alone**, so a fresh install is silent and the
  second setup is a no-op;
* the entities that are still supported are **never** in the list.
"""
from unittest.mock import MagicMock

from homeassistant.const import Platform

from custom_components.ekey_ha_app import (
    _RETIRED_ENTITIES,
    _async_remove_retired_entities,
)
from custom_components.ekey_ha_app.const import DOMAIN

ENTRY_ID = "abc123"


def _entry():
    entry = MagicMock()
    entry.entry_id = ENTRY_ID
    return entry


def _registry(existing: dict[str, str]):
    """A fake entity registry that knows about ``{unique_id: entity_id}``."""
    registry = MagicMock()
    registry.async_get_entity_id.side_effect = (
        lambda domain, platform, unique_id: existing.get(unique_id)
    )
    return registry


def _run(monkeypatch, registry):
    hass = MagicMock()
    monkeypatch.setattr(
        "custom_components.ekey_ha_app.er.async_get", lambda _hass: registry
    )
    _async_remove_retired_entities(hass, _entry())


def test_the_retired_list_is_the_five_helper_entities():
    """The historical unique_id suffixes, verbatim from the classes that made them."""
    assert set(_RETIRED_ENTITIES) == {
        (Platform.SELECT, "_person_selector"),
        (Platform.SELECT, "_finger_selector"),
        (Platform.SELECT, "_enrolled_fingerprints"),
        (Platform.BUTTON, "_check_orphaned"),
        (Platform.BUTTON, "_show_fingerprints"),
    }


def test_supported_entities_are_not_retired():
    """The LED buttons and the three sensors must never be swept up."""
    suffixes = {suffix for _domain, suffix in _RETIRED_ENTITIES}
    for keep in ("_led_green", "_led_red", "_device_info", "_fingerprint_count",
                 "_last_access"):
        assert keep not in suffixes


def test_existing_rows_are_removed(monkeypatch):
    """Each retired row is looked up under this entry's id and removed."""
    existing = {
        f"{ENTRY_ID}{suffix}": f"{domain}.ekey{suffix}"
        for domain, suffix in _RETIRED_ENTITIES
    }
    registry = _registry(existing)

    _run(monkeypatch, registry)

    asked = {call.args[2] for call in registry.async_get_entity_id.call_args_list}
    assert asked == set(existing)
    # The integration must identify itself as the platform, or the lookup silently
    # finds nothing and every ghost survives.
    assert {call.args[1] for call in registry.async_get_entity_id.call_args_list} == {DOMAIN}
    removed = {call.args[0] for call in registry.async_remove.call_args_list}
    assert removed == set(existing.values())


def test_a_fresh_install_removes_nothing(monkeypatch):
    """No rows exist: no removals, no exception."""
    registry = _registry({})

    _run(monkeypatch, registry)

    assert registry.async_get_entity_id.call_count == len(_RETIRED_ENTITIES)
    registry.async_remove.assert_not_called()


def test_only_this_entrys_rows_are_touched(monkeypatch):
    """A second scanner's identically-named helpers belong to a different entry."""
    other = {f"other-entry{suffix}": f"select.other{suffix}"
             for _domain, suffix in _RETIRED_ENTITIES}
    registry = _registry(other)

    _run(monkeypatch, registry)

    registry.async_remove.assert_not_called()


# ---------------------------------------------------------------------------
# Telling the user about it
#
# Deleting the rows cleans up the entity list and says nothing to whoever was USING
# one, and the symptom on the other side is baffling: an automation built from the
# old relay-pulse blueprint referenced select.<device>_enrolled_fingerprints, and
# with the entity gone its picker shows "Unknown entity selected" over an empty
# dropdown with nothing naming the cause. That is what the repair issue is for, so
# what these tests hold to account is that it appears when it should, says which
# automation, and goes away on its own.
# ---------------------------------------------------------------------------
from types import SimpleNamespace  # noqa: E402  (grouped with its own tests)

from custom_components.ekey_ha_app import (  # noqa: E402
    _RETIRED_REFERENCE_ISSUE,
    _async_check_retired_references,
    _async_retired_entity_ids,
)


def _registry_with_deleted(deleted: dict[str, str]):
    """A fake registry whose ``deleted_entities`` holds ``{suffix: entity_id}``."""
    registry = MagicMock()
    registry.deleted_entities = {
        (domain, DOMAIN, f"{ENTRY_ID}{suffix}"): SimpleNamespace(
            entity_id=deleted[suffix]
        )
        for domain, suffix in _RETIRED_ENTITIES
        if suffix in deleted
    }
    return registry


def _check(monkeypatch, registry, automations=None, scripts=None):
    """Run the reference check with stubbed automation/script lookups.

    The two lookups are patched where they are imported FROM, because
    ``_async_check_retired_references`` imports them inside the function — on purpose,
    so a custom component does not drag the automation and script components into
    every import of its own package.
    """
    automations = automations or {}
    scripts = scripts or {}
    monkeypatch.setattr(
        "custom_components.ekey_ha_app.er.async_get", lambda _hass: registry
    )
    monkeypatch.setattr(
        "homeassistant.components.automation.automations_with_entity",
        lambda _hass, entity_id: automations.get(entity_id, []),
    )
    monkeypatch.setattr(
        "homeassistant.components.script.scripts_with_entity",
        lambda _hass, entity_id: scripts.get(entity_id, []),
    )
    created: list[dict] = []
    deleted: list[str] = []
    monkeypatch.setattr(
        "custom_components.ekey_ha_app.ir.async_create_issue",
        lambda hass, domain, issue_id, **kw: created.append(
            {"domain": domain, "issue_id": issue_id, **kw}
        ),
    )
    monkeypatch.setattr(
        "custom_components.ekey_ha_app.ir.async_delete_issue",
        lambda hass, domain, issue_id: deleted.append(issue_id),
    )
    _async_check_retired_references(MagicMock(), _entry())
    return created, deleted


def test_retired_ids_come_from_the_registrys_deleted_rows(monkeypatch):
    """The ids are recovered, not remembered.

    This is what makes the check work on an installation upgraded BEFORE the check
    existed: the rows were removed on an earlier start, so nothing in memory could
    have remembered them — but Home Assistant keeps a deleted-entity row forever.
    """
    registry = _registry_with_deleted(
        {"_enrolled_fingerprints": "select.door_ekey_enrolled_fingerprints"}
    )
    monkeypatch.setattr(
        "custom_components.ekey_ha_app.er.async_get", lambda _hass: registry
    )

    assert _async_retired_entity_ids(MagicMock(), _entry()) == [
        "select.door_ekey_enrolled_fingerprints"
    ]


def test_no_references_means_no_issue(monkeypatch):
    """The normal upgrade: the entity is gone and nobody was using it."""
    registry = _registry_with_deleted(
        {"_enrolled_fingerprints": "select.door_ekey_enrolled_fingerprints"}
    )

    created, deleted = _check(monkeypatch, registry)

    assert created == []
    # Deleted rather than merely skipped, so an issue raised on an earlier start
    # clears itself once the user has fixed their automation.
    assert deleted == [f"{_RETIRED_REFERENCE_ISSUE}_{ENTRY_ID}"]


def test_a_referencing_automation_raises_a_repair(monkeypatch):
    """The reported case: an automation from the old blueprint still points at it."""
    registry = _registry_with_deleted(
        {"_enrolled_fingerprints": "select.door_ekey_enrolled_fingerprints"}
    )

    created, deleted = _check(
        monkeypatch,
        registry,
        automations={
            "select.door_ekey_enrolled_fingerprints": ["automation.ekey_open_the_door"]
        },
    )

    assert deleted == []
    assert len(created) == 1
    issue = created[0]
    assert issue["domain"] == DOMAIN
    assert issue["issue_id"] == f"{_RETIRED_REFERENCE_ISSUE}_{ENTRY_ID}"
    assert issue["translation_key"] == _RETIRED_REFERENCE_ISSUE
    # Not fixable in place: the blueprint's inputs changed, so there is nothing a
    # repair flow could safely rewrite on the user's behalf.
    assert issue["is_fixable"] is False
    # The placeholders must name BOTH sides, or the issue is a riddle: which entity,
    # and which automation to go and edit.
    placeholders = issue["translation_placeholders"]
    assert "select.door_ekey_enrolled_fingerprints" in placeholders["entities"]
    assert "automation.ekey_open_the_door" in placeholders["users"]


def test_a_referencing_script_counts_too(monkeypatch):
    """Scripts reference entities exactly as automations do."""
    registry = _registry_with_deleted(
        {"_person_selector": "select.door_ekey_person_selector"}
    )

    created, _deleted = _check(
        monkeypatch,
        registry,
        scripts={"select.door_ekey_person_selector": ["script.enrol_a_finger"]},
    )

    assert len(created) == 1
    assert "script.enrol_a_finger" in created[0]["translation_placeholders"]["users"]


def test_a_fresh_install_raises_nothing(monkeypatch):
    """Nothing was ever deleted, so there is nothing to have referenced."""
    created, deleted = _check(monkeypatch, _registry_with_deleted({}))

    assert created == []
    assert deleted == [f"{_RETIRED_REFERENCE_ISSUE}_{ENTRY_ID}"]
