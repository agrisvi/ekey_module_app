"""Tests for backup and restore.

The file these functions produce is the copy that leaves the building — and the one
an attacker would most like to have, because writing those templates into any
scanner of the same variant mints working credentials. So the tests that matter are
about refusing to trust a file, not about the happy path:

* a wrong passphrase, a truncated payload, and a header somebody edited must all
  fail, and the header case only fails at all because the header is fed to AES-GCM
  as associated data;
* a restored record whose "template" is really a saved error reply must be reported
  by name rather than restored and pushed to a door later;
* the header has to be readable *without* the passphrase, because that is what
  lets the restore dialog say what a file claims to hold before asking for a
  secret — while staying honest that an unverified header is only a claim.
"""
import base64
import json

import pytest

from custom_components.ekey_ha_app.backup import (
    BACKUP_FORMAT,
    BACKUP_VERSION,
    MIN_PASSPHRASE,
    BackupError,
    BackupLocked,
    create,
    open_payload,
    read_header,
    validate_records,
)
from custom_components.ekey_ha_app.templates import parse_template_hex
from custom_components.ekey_ha_app.vault import empty_vault, put_record

from .test_templates import REAL_APID_A, REAL_APID_B, TEMPLATE_A, TEMPLATE_B

PASSPHRASE = "correct horse battery"


def sample_vault():
    data = put_record(
        empty_vault(),
        apid=REAL_APID_A,
        username="Master",
        finger=7,
        template=parse_template_hex(TEMPLATE_A),
        dev_variant=10,
    )
    return put_record(
        data,
        apid=REAL_APID_B,
        username="Bob",
        finger=1,
        ha_person="person.bob",
        template=parse_template_hex(TEMPLATE_B),
        dev_variant=10,
    )


def make(passphrase=PASSPHRASE, vault=None):
    return create(
        vault if vault is not None else sample_vault(),
        passphrase=passphrase,
        created_by="ekey module App 1.4.0",
        installation="abc123",
    )


# ----------------------------------------------------------------- round trips


def test_an_encrypted_backup_round_trips():
    raw, filename = make()

    opened = open_payload(raw, PASSPHRASE)

    assert filename.endswith(".ekeybak")
    assert set(opened["records"]) == {REAL_APID_A, REAL_APID_B}
    assert opened["records"][REAL_APID_A]["template"] == TEMPLATE_A
    assert opened["people"]["person.bob"]["ha_person"] == "person.bob"


def test_an_unencrypted_backup_round_trips_and_carries_a_warning():
    raw, filename = make(passphrase=None)

    envelope = json.loads(raw)
    opened = open_payload(raw)

    assert filename.endswith(".json")
    assert "plain text" in envelope["WARNING"]
    assert envelope["encryption"] is None
    assert opened["records"][REAL_APID_A]["template"] == TEMPLATE_A


def test_the_template_hex_is_not_readable_in_an_encrypted_file():
    """The whole point: the file on the laptop must not be a template dump."""
    raw, _ = make()
    assert TEMPLATE_A.encode() not in raw
    assert b"Master" not in raw


def test_an_empty_database_can_still_be_backed_up():
    raw, _ = make(vault=empty_vault())
    assert open_payload(raw, PASSPHRASE) == {"records": {}, "people": {}}


# --------------------------------------------------------------- the header


def test_the_header_is_readable_without_the_passphrase():
    """What fills the restore dialog before anyone types a secret."""
    raw, _ = make()

    header, encrypted = read_header(raw)

    assert encrypted is True
    assert header["format"] == BACKUP_FORMAT
    assert header["version"] == BACKUP_VERSION
    assert header["record_count"] == 2
    assert header["user_count"] == 2
    assert header["has_templates"] is True
    assert header["created_by"] == "ekey module App 1.4.0"
    assert header["installation"] == "abc123"


def test_the_header_names_the_variants_and_salts_the_records_need():
    """A fleet on another variant can never use these templates, and a preview
    should say so instead of discovering it one failed write at a time."""
    header, _ = read_header(make()[0])
    assert header["dev_variants"] == [10]
    assert header["domain_ids"] == ["avubs"]


def test_the_encryption_details_are_declared():
    header, _ = read_header(make()[0])
    assert header["encryption"]["kdf"] == "scrypt"
    assert header["encryption"]["cipher"] == "AES-256-GCM"
    assert base64.b64decode(header["encryption"]["salt"])
    assert base64.b64decode(header["encryption"]["nonce"])


def test_two_backups_of_the_same_data_differ():
    """A fresh salt and nonce every time, or two files would leak that nothing changed."""
    first, _ = make()
    second, _ = make()
    assert first != second


# ------------------------------------------------------------------ refusals


def test_a_wrong_passphrase_is_refused_and_says_nothing_was_restored():
    raw, _ = make()
    with pytest.raises(BackupLocked) as err:
        open_payload(raw, "not the passphrase")
    assert "Nothing has been restored" in str(err.value)


def test_a_missing_passphrase_asks_for_one_rather_than_failing_obscurely():
    raw, _ = make()
    with pytest.raises(BackupLocked) as err:
        open_payload(raw)
    assert "encrypted" in str(err.value)


def test_a_truncated_payload_is_refused():
    """A download that died half way through."""
    raw, _ = make()
    envelope = json.loads(raw)
    envelope["payload"] = envelope["payload"][: len(envelope["payload"]) // 2]
    with pytest.raises(BackupError):
        open_payload(json.dumps(envelope).encode(), PASSPHRASE)


def test_an_edited_header_breaks_decryption():
    """THE test for the envelope design. The header is associated data, so a file
    whose preview was tampered with cannot then be opened — the preview a person
    approved is the payload they get."""
    raw, _ = make()
    envelope = json.loads(raw)
    envelope["record_count"] = 999            # lie about the contents
    with pytest.raises(BackupLocked):
        open_payload(json.dumps(envelope).encode(), PASSPHRASE)


def test_a_swapped_payload_is_refused():
    """Two genuine backups, one's ciphertext in the other's envelope."""
    first = json.loads(make()[0])
    second = json.loads(make()[0])
    first["payload"] = second["payload"]
    with pytest.raises(BackupLocked):
        open_payload(json.dumps(first).encode(), PASSPHRASE)


def test_a_tampered_unencrypted_payload_is_caught_by_its_own_checksum():
    """Plain files get no authentication, but they do get a digest."""
    raw, _ = make(passphrase=None)
    envelope = json.loads(raw)
    envelope["payload"]["records"][REAL_APID_A]["finger"] = 3
    with pytest.raises(BackupError) as err:
        open_payload(json.dumps(envelope).encode())
    assert "checksum" in str(err.value)


def test_a_file_that_is_not_json_is_refused():
    with pytest.raises(BackupError) as err:
        read_header(b"\x00\x01\x02 not json")
    assert "not" in str(err.value)


def test_a_json_file_that_is_not_a_backup_is_refused():
    with pytest.raises(BackupError) as err:
        read_header(b'{"some":"other file"}')
    assert "format marker" in str(err.value)


def test_a_future_backup_version_is_refused_by_name():
    raw, _ = make()
    envelope = json.loads(raw)
    envelope["version"] = 99
    with pytest.raises(BackupError) as err:
        read_header(json.dumps(envelope).encode())
    assert "version 99" in str(err.value)


def test_an_unsupported_cipher_is_refused():
    raw, _ = make()
    envelope = json.loads(raw)
    envelope["encryption"]["cipher"] = "ROT13"
    with pytest.raises(BackupError) as err:
        open_payload(json.dumps(envelope).encode(), PASSPHRASE)
    assert "unsupported encryption" in str(err.value)


def test_absurd_kdf_parameters_are_refused_rather_than_honoured():
    """A hostile file must not be able to ask for gigabytes of scrypt memory."""
    raw, _ = make()
    envelope = json.loads(raw)
    envelope["encryption"]["n"] = 2**30
    with pytest.raises(BackupError) as err:
        open_payload(json.dumps(envelope).encode(), PASSPHRASE)
    assert "safe limits" in str(err.value)


def test_a_short_passphrase_is_refused_when_creating():
    with pytest.raises(BackupError) as err:
        make(passphrase="a" * (MIN_PASSPHRASE - 1))
    assert str(MIN_PASSPHRASE) in str(err.value)


def test_backup_errors_are_value_errors():
    """So the websocket layer reports them as invalid_request, not as an outage."""
    assert issubclass(BackupError, ValueError)
    assert issubclass(BackupLocked, BackupError)


# ------------------------------------------------------- validating what came back


def test_validate_records_accepts_a_healthy_set():
    good, problems = validate_records(sample_vault()["records"])
    assert set(good) == {REAL_APID_A, REAL_APID_B}
    assert problems == []


def test_validate_records_rejects_an_error_reply_saved_as_a_template():
    """The trap this project has already fallen into once, arriving via a file."""
    records = dict(sample_vault()["records"])
    records[REAL_APID_A] = {**records[REAL_APID_A], "template": '{"error":"nope"}'}

    good, problems = validate_records(records)

    assert set(good) == {REAL_APID_B}
    assert len(problems) == 1
    assert REAL_APID_A in problems[0] and "JSON" in problems[0]


def test_validate_records_rejects_a_record_filed_under_the_wrong_apid():
    """A template that belongs to another finger must not be restored as this one."""
    records = {REAL_APID_A: dict(sample_vault()["records"][REAL_APID_B])}

    good, problems = validate_records(records)

    assert good == {}
    assert REAL_APID_A in problems[0]


def test_validate_records_keeps_metadata_only_records():
    """Legitimate: a finger known to exist on a scanner whose template was never read."""
    records = {REAL_APID_A: {"finger": 7, "template": None, "person_key": "name:master"}}
    good, problems = validate_records(records)
    assert set(good) == {REAL_APID_A}
    assert problems == []


def test_validate_records_rejects_a_record_that_is_not_a_record():
    good, problems = validate_records({REAL_APID_A: "junk"})
    assert good == {}
    assert "not a record" in problems[0]


def test_one_bad_record_does_not_cost_the_rest_of_the_restore():
    records = dict(sample_vault()["records"])
    records["11111111-2222-3333-4444-555555555555"] = {"template": "ZZZZ"}
    good, problems = validate_records(records)
    assert len(good) == 2
    assert len(problems) == 1
