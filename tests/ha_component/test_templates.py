"""Tests for template-blob validation.

This is the guard that decides what is allowed into the fingerprint database, and
the database is the only copy of something that cannot be re-derived. So the tests
worth having are not "valid hex parses" — they are the specific ways a blob turns
out not to be a fingerprint:

* **an error reply saved in place of a template.** The documented shell recipe
  pipes ``curl`` through ``sed``, and ``sed`` prints its input unchanged when the
  pattern does not match, so a JSON error ends up in the ``.txt`` file and is later
  PUT to a scanner as if it were a finger. The C suite regression-tests this from
  the other side; this is the Home Assistant side of the same trap.
* **a truncated file**, from a download that died half way. Caught by the blob's
  own length prefix rather than by a size guess.
* **a template that is not the one we asked for**, which is also what keeps an
  unknown future TIF layout from being filed under the wrong finger.

The two headline cases use REAL dumps from the fleet (the ``<apid>.txt`` files the
operator produced with that shell recipe), because a synthetic blob proves only
that the parser agrees with the test author.
"""
import pytest

from custom_components.ekey_ha_app.templates import (
    DEFAULT_DOMAIN_ID,
    MAX_TEMPLATE_BYTES,
    TemplateError,
    is_template_hex,
    parse_template_hex,
)

# Real header bytes, then filler. The dumps themselves are ~14 kB; carrying them
# verbatim would bury the assertions, so each fixture is built to the exact
# declared length instead — the header is what the parser reads, and the length is
# what it checks.
#
#   200bc861-ed21-42c6-b8f8-c3ffb8d6abee : "AA19" -> 0x19AA = 6570 bytes
#   630d687f-b617-4e8e-b62b-08f36405e6f7 : "791C" -> 0x1C79 = 7289 bytes
REAL_APID_A = "200bc861-ed21-42c6-b8f8-c3ffb8d6abee"
REAL_HEAD_A = "AA1900000000200BC861ED2142C6B8F8C3FFB8D6ABEE0A000000000000007F"
REAL_APID_B = "630d687f-b617-4e8e-b62b-08f36405e6f7"
REAL_HEAD_B = "791C00000000630D687FB6174E8EB62B08F36405E6F70A000000000000007F"


def build(head: str, declared: int) -> str:
    """Pad a real header out to the length its own prefix declares."""
    total_hex = 4 + 2 * declared
    return head + "AB" * ((total_hex - len(head)) // 2)


TEMPLATE_A = build(REAL_HEAD_A, 6570)
TEMPLATE_B = build(REAL_HEAD_B, 7289)


# --------------------------------------------------------------- the real thing


def test_a_real_template_parses_and_names_its_own_finger():
    """The APID in the header is the APID in the dump's filename."""
    info = parse_template_hex(TEMPLATE_A)
    assert info.apid == REAL_APID_A
    assert info.tif_len == 6570
    assert info.version == 0
    assert info.byte_len == 6572           # 2-byte prefix + the TIF
    assert len(info.hex) == 4 + 2 * 6570   # 13144, as the real file measures


def test_the_second_real_template_too():
    """Two different sizes, so the length check is not accidentally hard-coded."""
    info = parse_template_hex(TEMPLATE_B)
    assert info.apid == REAL_APID_B
    assert info.tif_len == 7289
    assert len(info.hex) == 14582


def test_lowercase_and_whitespace_are_accepted_and_normalised():
    """A hand-made file ends in a newline, and hex has no case. Both are fine."""
    info = parse_template_hex(f"\n  {TEMPLATE_B.lower()}\t\n")
    assert info.hex == TEMPLATE_B
    assert info.apid == REAL_APID_B


def test_the_digest_covers_the_normalised_form():
    """Otherwise the same template read twice would look like two templates."""
    assert parse_template_hex(TEMPLATE_B.lower()).sha256 == (
        parse_template_hex(TEMPLATE_B).sha256
    )


# ------------------------------------------------------------ the APID contract


def test_the_expected_apid_is_cross_checked():
    info = parse_template_hex(TEMPLATE_B, expect_apid=REAL_APID_B)
    assert info.apid == REAL_APID_B


def test_a_template_for_a_different_finger_is_refused():
    """The scanner answering about another finger must never be stored as this one.

    This is also the backstop for a future TIF layout that moves the AP-ID: the
    identity stops matching, so the blob is refused instead of misfiled.
    """
    with pytest.raises(TemplateError) as err:
        parse_template_hex(TEMPLATE_B, expect_apid=REAL_APID_A)
    assert REAL_APID_B in str(err.value)


def test_the_expected_apid_comparison_ignores_case():
    parse_template_hex(TEMPLATE_B, expect_apid=REAL_APID_B.upper())


# ----------------------------------------------------------------- the refusals


def test_an_error_reply_saved_as_a_template_is_named_as_such():
    """The failure this whole module exists for."""
    with pytest.raises(TemplateError) as err:
        parse_template_hex('{"error":"endpoint not found"}')
    assert "JSON" in str(err.value)


def test_a_whole_get_reply_saved_as_a_template_is_refused():
    with pytest.raises(TemplateError):
        parse_template_hex(
            '{"cmd":"GET_AP_FINGER_TEMPLATE","rpc_error_code":"Error",'
            '"error_message":"Unknown_ap_id"}'
        )


def test_a_truncated_template_is_refused_by_its_own_length_prefix():
    """A download that died at 90% — the prefix still claims the full size."""
    with pytest.raises(TemplateError) as err:
        parse_template_hex(TEMPLATE_B[: int(len(TEMPLATE_B) * 0.9) & ~1])
    message = str(err.value)
    assert "7289" in message and "incomplete or damaged" in message


def test_a_template_with_extra_bytes_appended_is_refused():
    """The mirror image: the prefix is authoritative in both directions."""
    with pytest.raises(TemplateError):
        parse_template_hex(TEMPLATE_B + "DEADBEEF")


def test_non_hex_is_refused_and_says_what_it_found():
    with pytest.raises(TemplateError) as err:
        parse_template_hex(TEMPLATE_B[:-4] + "ZZZZ")
    assert "hexadecimal" in str(err.value)


def test_an_odd_number_of_digits_is_refused():
    with pytest.raises(TemplateError) as err:
        parse_template_hex(TEMPLATE_B + "A")
    assert "odd number" in str(err.value)


def test_empty_and_blank_are_refused():
    for value in ("", "   ", "\n"):
        with pytest.raises(TemplateError):
            parse_template_hex(value)


def test_too_short_to_hold_a_header_is_refused():
    """Shorter than the length prefix plus version plus AP-ID."""
    with pytest.raises(TemplateError) as err:
        parse_template_hex("AA1900000000200BC861")
    assert "too short" in str(err.value)


def test_over_the_scanner_limit_is_refused():
    """A blob the scanner would reject must not be stored as if it were pushable."""
    oversize = MAX_TEMPLATE_BYTES  # declared TIF length, so total is 2 bytes more
    with pytest.raises(TemplateError) as err:
        parse_template_hex(build("0027" + REAL_HEAD_A[4:], oversize))
    assert str(MAX_TEMPLATE_BYTES) in str(err.value)


def test_a_zero_length_template_is_refused():
    with pytest.raises(TemplateError):
        parse_template_hex("0000")


def test_a_non_string_is_refused_rather_than_crashing():
    for value in (None, 12345, b"AA19", ["AA19"]):
        with pytest.raises(TemplateError):
            parse_template_hex(value)


def test_template_error_is_a_value_error():
    """ws_api maps ValueError to invalid_request, which is what the panel shows."""
    assert issubclass(TemplateError, ValueError)


# --------------------------------------------------------------------- previews


def test_is_template_hex_never_raises():
    assert is_template_hex(TEMPLATE_A) is True
    assert is_template_hex('{"error":"nope"}') is False
    assert is_template_hex("") is False
    assert is_template_hex(None) is False


def test_the_default_domain_id_is_the_backends_own_default():
    """A read that omits domainID gets this, so a stored record must assume it."""
    assert DEFAULT_DOMAIN_ID == "avubs"
