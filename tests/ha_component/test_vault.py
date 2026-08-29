"""Tests for the fingerprint database.

The record maths is pure — every mutation takes a dict and returns a new one — so
most of this needs no Home Assistant at all. That is deliberate on two counts: it
keeps the suite free of a test harness this repo does not install, and the
new-dict-every-time property is itself load-bearing. The store is constructed with
``serialize_in_event_loop=False`` so a megabyte of JSON is encoded off the event
loop, and that is only safe while nothing mutates the payload underneath the
encoder.

The behaviours worth pinning down are the ones where a wrong answer loses data or
misattributes a fingerprint:

* the join key across scanners, which is what makes one person's records line up
  when every scanner has its own user ids;
* a re-enrolment, which mints a NEW APID for a finger slot that already has one —
  the old template still works on whatever scanners hold it, so it must be marked
  rather than silently replaced;
* a record with no template, which can name a finger but cannot repair anything
  and must never be counted as pushable;
* the view model never carrying template hex to a browser.
"""
from custom_components.ekey_ha_app.templates import DEFAULT_DOMAIN_ID, parse_template_hex
from custom_components.ekey_ha_app.vault import (
    NAME_KEY_PREFIX,
    build_records_view,
    drop_record,
    empty_vault,
    is_person_link,
    normalise,
    person_key,
    pushable,
    put_record,
    stored_apids,
    total_bytes,
)

from .test_templates import REAL_APID_A, REAL_APID_B, TEMPLATE_A, TEMPLATE_B

INFO_A = parse_template_hex(TEMPLATE_A)
INFO_B = parse_template_hex(TEMPLATE_B)


def with_a(data=None, **kwargs):
    """Store template A as Master's finger 7 unless told otherwise."""
    options = {
        "apid": REAL_APID_A,
        "username": "Master",
        "finger": 7,
        "template": INFO_A,
        "source_entry_id": "e1",
        "source_scanner_id": "192.168.10.190:8080",
        "dev_variant": 10,
    }
    options.update(kwargs)
    return put_record(data if data is not None else empty_vault(), **options)


# -------------------------------------------------------------------- identity


def test_a_linked_person_is_the_join_key():
    """HA is where identity already lives, and a link survives a rename."""
    assert person_key("Master", "person.janis") == "person.janis"


def test_without_a_link_the_name_is_the_key_folded_and_stripped():
    """The weak case, and the reason the UI nudges towards linking a person."""
    assert person_key("  MASTER ") == f"{NAME_KEY_PREFIX}master"
    assert person_key("Master") == person_key("master")


def test_a_non_person_entity_is_not_treated_as_a_link():
    """Only a real person.* entity counts — anything else falls back to the name."""
    assert person_key("Master", "sensor.nonsense") == f"{NAME_KEY_PREFIX}master"
    assert person_key("Master", "") == f"{NAME_KEY_PREFIX}master"


def test_the_two_kinds_of_key_can_never_collide():
    assert is_person_link("person.janis") is True
    assert is_person_link(f"{NAME_KEY_PREFIX}master") is False


def test_a_missing_username_still_produces_a_usable_key():
    assert person_key(None) == NAME_KEY_PREFIX


# --------------------------------------------------------------------- storing


def test_storing_a_fingerprint_keeps_everything_needed_to_restore_it():
    """A record that cannot be written back is not a backup."""
    data = with_a()
    record = data["records"][REAL_APID_A]

    assert record["template"] == TEMPLATE_A
    assert record["domain_id"] == DEFAULT_DOMAIN_ID   # the salt, or it is unrestorable
    assert record["dev_variant"] == 10                # what decides portability
    assert record["tif_len"] == INFO_A.tif_len
    assert record["sha256"] == INFO_A.sha256
    assert record["finger"] == 7
    assert record["person_key"] == f"{NAME_KEY_PREFIX}master"
    assert record["source"]["scanner_id"] == "192.168.10.190:8080"
    assert record["captured_at"] > 0


def test_a_mutation_does_not_touch_the_dict_it_was_given():
    """Required by serialize_in_event_loop=False: nothing may change under the encoder."""
    before = empty_vault()
    after = with_a(before)

    assert before["records"] == {}
    assert after is not before
    assert after["records"] is not before["records"]


def test_the_domain_id_comes_from_the_template_that_was_read():
    """A non-default salt must follow the blob, not be re-guessed later."""
    from dataclasses import replace

    data = with_a(template=replace(INFO_A, domain_id="MyProject"))
    assert data["records"][REAL_APID_A]["domain_id"] == "MyProject"


def test_refreshing_the_same_apid_keeps_when_it_was_first_captured():
    """The same finger read again is not a new fingerprint."""
    first = with_a()
    captured = first["records"][REAL_APID_A]["captured_at"]

    again = with_a(first)

    assert again["records"][REAL_APID_A]["captured_at"] == captured
    assert len(again["records"]) == 1


def test_a_linked_person_is_recorded_for_display():
    data = with_a(username="Master", ha_person="person.janis")
    assert data["people"]["person.janis"] == {
        "ha_person": "person.janis",
        "name": "Master",
    }


# ----------------------------------------------------------- the re-enrolment case


def test_re_enrolling_a_finger_marks_the_old_record_rather_than_replacing_it():
    """A new APID for an occupied slot means a second template exists.

    The old one still works on whatever scanners hold it, so dropping the record
    here would leave a working fingerprint this database no longer knows about —
    which is precisely how a fingerprint gets stranded on a sensor with nothing to
    claim it. Mark it and let the operator delete it everywhere.
    """
    data = with_a()
    data = put_record(
        data,
        apid=REAL_APID_B,
        username="Master",
        finger=7,
        template=INFO_B,
    )

    assert data["records"][REAL_APID_A]["superseded_by"] == REAL_APID_B
    assert data["records"][REAL_APID_B]["superseded_by"] is None
    assert len(data["records"]) == 2


def test_a_different_finger_of_the_same_person_supersedes_nothing():
    data = with_a()
    data = put_record(
        data, apid=REAL_APID_B, username="Master", finger=8, template=INFO_B
    )
    assert data["records"][REAL_APID_A]["superseded_by"] is None


def test_the_same_finger_of_a_different_person_supersedes_nothing():
    data = with_a()
    data = put_record(
        data, apid=REAL_APID_B, username="Somebody Else", finger=7, template=INFO_B
    )
    assert data["records"][REAL_APID_A]["superseded_by"] is None


def test_an_already_superseded_record_is_not_re_marked():
    """Otherwise a third enrolment rewrites history on the first record."""
    data = with_a()
    data = put_record(data, apid=REAL_APID_B, username="Master", finger=7,
                      template=INFO_B)
    third = "11111111-2222-3333-4444-555555555555"
    data = put_record(data, apid=third, username="Master", finger=7)

    assert data["records"][REAL_APID_A]["superseded_by"] == REAL_APID_B
    assert data["records"][REAL_APID_B]["superseded_by"] == third


# --------------------------------------------------------------------- dropping


def test_dropping_a_record_removes_it():
    data = drop_record(with_a(), REAL_APID_A)
    assert data["records"] == {}


def test_dropping_the_last_record_of_a_person_forgets_the_person():
    data = drop_record(with_a(), REAL_APID_A)
    assert data["people"] == {}


def test_dropping_one_record_keeps_a_person_who_has_others():
    data = with_a()
    data = put_record(data, apid=REAL_APID_B, username="Master", finger=8,
                      template=INFO_B)
    data = drop_record(data, REAL_APID_B)
    assert f"{NAME_KEY_PREFIX}master" in data["people"]


def test_dropping_an_unknown_apid_is_not_an_error():
    """A delete that raced another delete must not take the database down."""
    data = drop_record(with_a(), "11111111-2222-3333-4444-555555555555")
    assert len(data["records"]) == 1


def test_dropping_does_not_mutate_the_input():
    before = with_a()
    drop_record(before, REAL_APID_A)
    assert REAL_APID_A in before["records"]


# ------------------------------------------------------------------- pushable


def test_a_record_without_a_template_is_not_pushable():
    """It can name a finger but repair nothing, so it must not be counted."""
    data = put_record(empty_vault(), apid=REAL_APID_A, username="Master", finger=7)

    assert REAL_APID_A in stored_apids(data)
    assert pushable(data) == {}


def test_a_record_with_a_template_is_pushable():
    assert list(pushable(with_a())) == [REAL_APID_A]


def test_total_bytes_counts_only_stored_templates():
    data = with_a()
    data = put_record(data, apid=REAL_APID_B, username="Bob", finger=1)
    assert total_bytes(data) == INFO_A.byte_len


# ----------------------------------------------------------------- the view model


def test_the_view_never_carries_template_hex():
    """The panel has no use for it, and shipping biometric data to a browser for
    display is a copy nobody asked for."""
    view = build_records_view(with_a())
    assert TEMPLATE_A not in str(view)
    assert view["users"][0]["fingers"][0]["has_template"] is True


def test_the_view_reports_counts_and_size():
    view = build_records_view(with_a())
    assert view["record_count"] == 1
    assert view["user_count"] == 1
    assert view["bytes"] == INFO_A.byte_len


def test_the_view_groups_fingers_under_one_person_and_sorts_them():
    data = with_a()
    data = put_record(data, apid=REAL_APID_B, username="Master", finger=3,
                      template=INFO_B)

    view = build_records_view(data)

    assert view["user_count"] == 1
    assert [f["finger"] for f in view["users"][0]["fingers"]] == [3, 7]


def test_the_view_sorts_people_by_name():
    data = with_a(username="Zoe")
    data = put_record(data, apid=REAL_APID_B, username="Alice", finger=1,
                      template=INFO_B)
    view = build_records_view(data)
    assert [u["username"] for u in view["users"]] == ["Alice", "Zoe"]


def test_the_view_names_a_person_without_a_link_by_their_name():
    view = build_records_view(with_a())
    assert view["users"][0]["username"] == "Master"
    assert view["users"][0]["ha_person"] is None


def test_the_view_of_an_empty_database():
    view = build_records_view(empty_vault())
    assert view["record_count"] == 0
    assert view["users"] == []


# ------------------------------------------------------------------ normalising


def test_normalise_repairs_junk_instead_of_failing():
    """A hand-edited file must not take the database out of service."""
    assert normalise(None) == empty_vault()
    assert normalise("nonsense") == empty_vault()
    assert normalise({"records": "not a dict"})["records"] == {}


def test_normalise_drops_records_that_are_not_dicts():
    data = normalise({"records": {REAL_APID_A: "junk", REAL_APID_B: {"finger": 1}}})
    assert list(data["records"]) == [REAL_APID_B]


def test_normalise_keeps_unknown_keys_out_of_the_way():
    """A file written by a future version stays loadable."""
    data = normalise({"records": {}, "people": {}, "changed": 5, "future": "x"})
    assert data["changed"] == 5
