"""Tests for the template half of the client — reading and writing a fingerprint.

These two calls are how a fingerprint moves between scanners, and they are the
only calls in the integration where **the HTTP status is nearly meaningless**. The
scanner acknowledges the transport frames before it has decrypted anything, so a
write that was accepted and then thrown away comes back as HTTP 200 with
``rpc_error_code: "OK"`` and one field flipped. Everything worth pinning down here
follows from that:

* ``verified: true`` is the only success. ``verified: false`` must raise, even
  though every other signal in the reply says the write went fine.
* ``verdict`` separates "the device answered and refused" from "only the transport
  ever acknowledged it" — a definite no versus an unconfirmed write.
* ``domainID`` is the salt the blob was read under and the only one it can be
  written back under. The backend omits the field when it used its own default, so
  an absent value has to be read as ``avubs`` and stored, not as "unknown".
* a backend too old for these routes answers 404/501, which has to arrive as
  :class:`EkeyNotFoundError` — "this scanner cannot take part", not an outage.
"""
import pytest

from custom_components.ekey_ha_app.api import (
    EkeyApiError,
    EkeyAppClient,
    EkeyAuthError,
    EkeyNotFoundError,
    EkeyScannerRefused,
    EkeyTemplateRejected,
)
from custom_components.ekey_ha_app.connection import EkeyConnection
from custom_components.ekey_ha_app.templates import DEFAULT_DOMAIN_ID, TemplateError

from .fake_http import FakeSession
from .test_templates import REAL_APID_B, TEMPLATE_B

CONN = EkeyConnection(host="dev.local", port=8080, use_ssl=False, token="tok")

GET_PATH = f"/api/v1/fingerprints/{REAL_APID_B}/template"
PUT_PATH = "/api/v1/fingerprints/template"

GET_OK = {
    "cmd": "GET_AP_FINGER_TEMPLATE",
    "rpc_cmd_id": "0x0014",
    "rpc_error_code": "OK",
    "apFingerTemplate": TEMPLATE_B,
    "domainID": "avubs",
    "id": 14,
}
PUT_OK = {
    "cmd": "SET_AP_FINGER_TEMPLATE",
    "rpc_cmd_id": "0x0015",
    "rpc_error_code": "OK",
    "verdict": "device_response",
    "apid": REAL_APID_B,
    "verified": True,
    "id": 2,
}


def make():
    session = FakeSession()
    return EkeyAppClient(CONN, session), session


# ------------------------------------------------------------------------- GET


async def test_get_template_returns_a_validated_template():
    """The caller gets a checked blob, not a raw body it still has to trust."""
    client, session = make()
    session.add("GET", GET_PATH, body=GET_OK)

    info = await client.async_get_template(REAL_APID_B)

    assert info.apid == REAL_APID_B
    assert info.tif_len == 7289
    assert info.domain_id == "avubs"
    assert info.hex == TEMPLATE_B


async def test_get_template_omits_the_domain_id_when_it_is_the_default():
    """No body at all, so the backend applies its own default — one less way to differ."""
    client, session = make()
    session.add("GET", GET_PATH, body=GET_OK)
    await client.async_get_template(REAL_APID_B, domain_id=DEFAULT_DOMAIN_ID)
    assert session.last_json("GET", GET_PATH) is None


async def test_get_template_sends_a_non_default_domain_id_as_a_body():
    """There is no query-parameter form for it, so it can only travel in the body."""
    client, session = make()
    session.add("GET", GET_PATH, body=dict(GET_OK, domainID="MyProject"))
    info = await client.async_get_template(REAL_APID_B, domain_id="MyProject")
    assert session.last_json("GET", GET_PATH) == {"domainID": "MyProject"}
    assert info.domain_id == "MyProject"


async def test_an_absent_domain_id_is_recorded_as_the_backend_default():
    """The field is only emitted when non-empty. Absent must not become "unknown":
    a record without a domainID can never be written back."""
    client, session = make()
    body = {k: v for k, v in GET_OK.items() if k != "domainID"}
    session.add("GET", GET_PATH, body=body)
    info = await client.async_get_template(REAL_APID_B)
    assert info.domain_id == DEFAULT_DOMAIN_ID


async def test_get_template_refuses_a_template_for_another_finger():
    """The scanner answering about a different finger must not be stored as this one.

    The request is for one APID; the reply carries another finger's blob. Storing
    it would file a working fingerprint under the wrong identity — and this same
    check is what makes an unrecognised future TIF layout safe.
    """
    other = "11111111-2222-3333-4444-555555555555"
    client, session = make()
    session.add("GET", f"/api/v1/fingerprints/{other}/template", body=GET_OK)

    with pytest.raises(TemplateError) as err:
        await client.async_get_template(other)

    assert REAL_APID_B in str(err.value)


async def test_get_template_refuses_an_error_reply_in_the_template_field():
    """The `sed` trap, arriving over HTTP instead of through a file."""
    client, session = make()
    session.add("GET", GET_PATH, body=dict(GET_OK, apFingerTemplate='{"error":"nope"}'))
    with pytest.raises(TemplateError):
        await client.async_get_template(REAL_APID_B)


async def test_get_template_surfaces_a_scanner_refusal():
    """HTTP 200 with rpc_error_code Error — an unknown APID, for instance."""
    client, session = make()
    session.add("GET", GET_PATH, body={
        "cmd": "GET_AP_FINGER_TEMPLATE", "rpc_error_code": "Error",
        "rpc_error_code_value": 11, "error_message": "Unknown_ap_id",
    })
    with pytest.raises(EkeyScannerRefused):
        await client.async_get_template(REAL_APID_B)


async def test_get_template_on_an_old_backend_is_not_found():
    """404/501 means "this scanner has no template API" — information, not an outage."""
    client, session = make()
    session.add("GET", GET_PATH, status=501, body={"error": "not implemented"})
    with pytest.raises(EkeyNotFoundError):
        await client.async_get_template(REAL_APID_B)


async def test_get_template_unauthorized_is_its_own_error():
    client, session = make()
    session.add("GET", GET_PATH, status=401, body={"error": "unauthorized"})
    with pytest.raises(EkeyAuthError):
        await client.async_get_template(REAL_APID_B)


# ------------------------------------------------------------------------- PUT


async def test_put_template_sends_the_blob_and_no_apid():
    """The APID travels inside the template; a path or a field for it would be a
    second opinion the scanner ignores."""
    client, session = make()
    session.add("PUT", PUT_PATH, body=PUT_OK)

    result = await client.async_put_template(TEMPLATE_B)

    assert session.last_json("PUT", PUT_PATH) == {"apFingerTemplate": TEMPLATE_B}
    assert result["apid"] == REAL_APID_B
    assert result["verified"] is True


async def test_put_template_sends_a_non_default_domain_id():
    client, session = make()
    session.add("PUT", PUT_PATH, body=PUT_OK)
    await client.async_put_template(TEMPLATE_B, domain_id="MyProject")
    assert session.last_json("PUT", PUT_PATH)["domainID"] == "MyProject"


async def test_put_template_normalises_the_hex_before_sending():
    client, session = make()
    session.add("PUT", PUT_PATH, body=PUT_OK)
    await client.async_put_template(f"  {TEMPLATE_B.lower()}\n")
    assert session.last_json("PUT", PUT_PATH)["apFingerTemplate"] == TEMPLATE_B


async def test_verified_false_raises_even_though_everything_else_says_ok():
    """THE test in this file. 200, rpc_error_code OK, and the template was discarded.

    Treating this as success is how the presence matrix ends up claiming a door
    holds a fingerprint it does not.
    """
    client, session = make()
    session.add("PUT", PUT_PATH, body=dict(PUT_OK, verified=False))

    with pytest.raises(EkeyTemplateRejected) as err:
        await client.async_put_template(TEMPLATE_B)

    assert err.value.verified is False
    assert err.value.verdict == "device_response"
    assert err.value.apid == REAL_APID_B


async def test_a_missing_verified_field_is_not_success_either():
    """An older backend that never learned to answer must not be read as a yes."""
    client, session = make()
    session.add("PUT", PUT_PATH, body={k: v for k, v in PUT_OK.items() if k != "verified"})
    with pytest.raises(EkeyTemplateRejected):
        await client.async_put_template(TEMPLATE_B)


async def test_transport_ack_only_is_reported_as_such():
    """Nobody ever confirmed the write — the caller may want to verify by reading
    the saved-AP list rather than declaring failure."""
    client, session = make()
    session.add("PUT", PUT_PATH, body=dict(PUT_OK, verified=False,
                                           verdict="transport_ack_only"))
    with pytest.raises(EkeyTemplateRejected) as err:
        await client.async_put_template(TEMPLATE_B)
    assert err.value.verdict == "transport_ack_only"


async def test_put_template_refuses_an_invalid_blob_before_any_request():
    """A bad template must not reach a door controller at all."""
    client, session = make()
    with pytest.raises(TemplateError):
        await client.async_put_template('{"error":"not a template"}')
    assert session.calls == []


async def test_put_template_surfaces_a_decryption_refusal():
    """Variant/domainID mismatch, reported by the scanner rather than by `verified`."""
    client, session = make()
    session.add("PUT", PUT_PATH, body={
        "cmd": "SET_AP_FINGER_TEMPLATE", "rpc_error_code": "Error",
        "rpc_error_code_value": 22, "error_message": "Error_encryption",
    })
    with pytest.raises(EkeyScannerRefused) as err:
        await client.async_put_template(TEMPLATE_B)
    assert "Error_encryption" in str(err.value)


async def test_put_template_too_large_for_the_command_buffer():
    """The library answers -3 -> HTTP 413 for a body it cannot hold."""
    client, session = make()
    session.add("PUT", PUT_PATH, status=413, body={"error": "body too large"})
    with pytest.raises(EkeyApiError):
        await client.async_put_template(TEMPLATE_B)


async def test_a_template_rejection_is_an_api_error():
    """So the existing websocket error mapping keeps working without a new branch."""
    assert issubclass(EkeyTemplateRejected, EkeyApiError)


# ---------------------------------------------------------------------- device


async def test_get_device_returns_the_variant_that_decides_portability():
    client, session = make()
    session.add("GET", "/api/v1/device", body={
        "cmd": "GET_DEVICE_INFORMATION", "rpc_error_code": "OK",
        "prod_sn": "4500610138250001", "sw_version": "3.0.85",
        "dev_variant": 10, "dev_sub_variant": 10, "id": 12,
    })

    info = await client.async_get_device()

    assert info["dev_variant"] == 10
    assert info["prod_sn"] == "4500610138250001"
