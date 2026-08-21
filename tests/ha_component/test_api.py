"""Tests for EkeyAppClient — the app-layer HTTP client.

The cases that matter here are the ones a naive client gets wrong:

* a **scanner refusal arrives as HTTP 200** with ``rpc_error_code: "Error"``, so a
  status-code check alone reports failure as success;
* ``/app/v1/*`` answering **404** is normal information (a backend with no app
  layer), not an outage;
* **401** must be distinguishable from both, because it means "ask for a new token",
  not "this device is too old";
* the event log's total lives in a **response header**, not the body.
"""
import pytest

from custom_components.ekey_ha_app.api import (
    EkeyApiError,
    EkeyAppClient,
    EkeyAuthError,
    EkeyBusyError,
    EkeyNotFoundError,
    EkeyScannerRefused,
)
from custom_components.ekey_ha_app.connection import EkeyConnection

from .fake_http import FakeResponse, FakeSession

CONN = EkeyConnection(host="dev.local", port=8080, use_ssl=True, token="tok")


def make(routes=None):
    session = FakeSession(routes)
    return EkeyAppClient(CONN, session), session


# --------------------------------------------------------------------- users


async def test_get_users_returns_the_list():
    client, _ = make()
    users = [{"id": "u1", "username": "Jane", "fingers": []}]
    client._session.add("GET", "/app/v1/users", body=users)
    assert await client.async_get_users() == users


async def test_get_users_absent_document_is_an_empty_list():
    client, session = make()
    session.add("GET", "/app/v1/users", body=[])
    assert await client.async_get_users() == []


async def test_get_users_rejects_a_non_array_document():
    client, session = make()
    session.add("GET", "/app/v1/users", body={"not": "an array"})
    with pytest.raises(EkeyApiError):
        await client.async_get_users()


async def test_put_users_sends_the_whole_list_and_a_bearer_token():
    client, session = make()
    session.add("PUT", "/app/v1/users", body={"status": "saved"})
    users = [{"id": "u1", "username": "Jane", "fingers": []}]
    await client.async_put_users(users)
    assert session.last_json("PUT", "/app/v1/users") == users
    assert session.calls[-1]["headers"]["Authorization"] == "Bearer tok"


async def test_put_users_refuses_a_non_list():
    client, _ = make()
    with pytest.raises(EkeyApiError):
        await client.async_put_users({"id": "u1"})


# ------------------------------------------------------------- error mapping


async def test_401_is_an_auth_error_not_a_missing_app_layer():
    client, session = make()
    session.add("GET", "/app/v1/users", status=401, body={"error": "unauthorized"})
    with pytest.raises(EkeyAuthError):
        await client.async_get_users()


async def test_404_on_app_route_is_not_found():
    client, session = make()
    session.add("GET", "/app/v1/users", status=404, body={"error": "endpoint not found"})
    with pytest.raises(EkeyNotFoundError):
        await client.async_get_users()


async def test_504_is_busy_not_broken():
    client, session = make()
    session.add("GET", "/app/v1/users", status=504, body={"error": "scanner timeout"})
    with pytest.raises(EkeyBusyError):
        await client.async_get_users()


async def test_non_json_body_is_reported_as_such():
    client, session = make()
    session.add("GET", "/app/v1/users", body="<html>nope</html>")
    with pytest.raises(EkeyApiError):
        await client.async_get_users()


async def test_control_characters_in_json_are_tolerated():
    """The daemon has been seen embedding raw control characters in strings."""
    client, session = make()
    session.add("GET", "/app/v1/users", body='[{"id":"u1","username":"Ja\x1ene"}]')
    users = await client.async_get_users()
    assert users[0]["id"] == "u1"


async def test_two_json_documents_in_one_body_are_tolerated():
    """Starting an enrollment answers with the RPC reply AND a notification.

    The scanner library emits ``START_AP_ENROLL`` and then deliberately lets the
    ``NOTIFY_AP_ENROLL_STATE`` block fire so the state also reaches the event
    stream; both land in the one per-request response accumulator. The body is
    then NDJSON, and a strict parse fails with "Extra data" while the scanner has
    already started enrolling.
    """
    client, session = make()
    session.add(
        "POST",
        "/api/v1/fingerprints/enroll",
        body=(
            '{"cmd":"START_AP_ENROLL","rpc_cmd_id":"0x0001",'
            '"rpc_error_code":"OK","apid":"AP-1"}\n'
            '{"cmd":"NOTIFY_AP_ENROLL_STATE","apid":"AP-1","enstat":10,"entryc":0}\n'
        ),
    )
    result = await client.async_enroll_start("AP-1")
    assert result["cmd"] == "START_AP_ENROLL"
    assert result["apid"] == "AP-1"


async def test_the_reply_is_picked_out_of_the_pair_not_the_first_document():
    """Order is not guaranteed, so the reply is found by ``rpc_error_code``.

    This is the case that must not regress: a refusal arrives as HTTP 200 with
    ``rpc_error_code: "Error"``. Taking whichever document came first would report
    a refused enrollment as a successful one.
    """
    client, session = make()
    session.add(
        "POST",
        "/api/v1/fingerprints/enroll",
        body=(
            '{"cmd":"NOTIFY_AP_ENROLL_STATE","apid":"AP-1","enstat":10}\n'
            '{"cmd":"START_AP_ENROLL","rpc_error_code":"Error",'
            '"error_message":"already enrolling"}\n'
        ),
    )
    with pytest.raises(EkeyScannerRefused, match="already enrolling"):
        await client.async_enroll_start("AP-1")


async def test_a_trailing_fragment_does_not_lose_the_reply():
    """Half a notification is worth less than the reply in front of it."""
    client, session = make()
    session.add(
        "POST",
        "/api/v1/fingerprints/enroll",
        body='{"rpc_error_code":"OK","apid":"AP-9"}\n{"cmd":"NOTIFY_AP_ENR',
    )
    result = await client.async_enroll_start("AP-9")
    assert result["apid"] == "AP-9"


# ------------------------------------------------------ the 200-that-is-a-no


async def test_scanner_refusal_arrives_as_200_and_still_raises():
    client, session = make()
    session.add(
        "POST",
        "/api/v1/fingerprints/enroll",
        status=200,
        body={"rpc_error_code": "Error", "error_message": "storage full"},
    )
    with pytest.raises(EkeyScannerRefused) as excinfo:
        await client.async_enroll_start("apid-1")
    assert "storage full" in str(excinfo.value)


async def test_scanner_ok_response_passes_through():
    client, session = make()
    session.add(
        "POST", "/api/v1/fingerprints/enroll", body={"rpc_error_code": "OK", "id": 1}
    )
    assert (await client.async_enroll_start("apid-1"))["rpc_error_code"] == "OK"


# --------------------------------------------------------------- fingerprints


async def test_list_fingerprints_reads_the_aps_array():
    client, session = make()
    session.add("GET", "/api/v1/fingerprints", body={"num_aps": 2, "aps": ["a", "b"]})
    assert await client.async_list_fingerprints() == ["a", "b"]


async def test_list_fingerprints_handles_the_zero_case():
    """``aps`` is absent, not empty, when the sensor holds nothing."""
    client, session = make()
    session.add("GET", "/api/v1/fingerprints", body={"num_aps": 0})
    assert await client.async_list_fingerprints() == []


async def test_delete_fingerprint_url_encodes_the_apid():
    client, session = make()
    session.add("DELETE", "/api/v1/fingerprints/a%2Fb", body={"status": "deleted"})
    await client.async_delete_fingerprint("a/b")
    assert session.paths("DELETE") == ["/api/v1/fingerprints/a%2Fb"]


# --------------------------------------------------------------- capabilities


async def test_capabilities_absent_means_none_not_an_error():
    client, session = make()
    session.add("GET", "/app/v1/capabilities", status=404, body={"error": "nope"})
    assert await client.async_capabilities() is None


async def test_has_app_api_false_on_404_true_otherwise():
    client, session = make()
    session.add("GET", "/app/v1/users", status=404, body={"error": "nope"})
    assert await client.async_has_app_api() is False

    client2, session2 = make()
    session2.add("GET", "/app/v1/users", body=[])
    assert await client2.async_has_app_api() is True


async def test_has_app_api_propagates_auth_failure():
    """A wrong token must not be reported as 'no app layer'."""
    client, session = make()
    session.add("GET", "/app/v1/users", status=401, body={})
    with pytest.raises(EkeyAuthError):
        await client.async_has_app_api()


# ---------------------------------------------------------------- event log


async def test_events_reads_the_total_from_the_header():
    client, session = make()
    session.routes[("GET", "/app/v1/events")] = FakeResponse(
        200, [{"eventId": 1}, {"eventId": 2}], {"X-Event-Count": "57"}
    )
    rows, total = await client.async_get_events(limit=20, offset=0)
    assert len(rows) == 2
    assert total == 57


async def test_events_falls_back_to_the_page_length_without_the_header():
    client, session = make()
    session.routes[("GET", "/app/v1/events")] = FakeResponse(200, [{"eventId": 1}])
    _, total = await client.async_get_events()
    assert total == 1


async def test_events_passes_limit_and_offset():
    client, session = make()
    session.routes[("GET", "/app/v1/events")] = FakeResponse(200, [])
    await client.async_get_events(limit=5, offset=10)
    assert "limit=5" in session.calls[-1]["url"]
    assert "offset=10" in session.calls[-1]["url"]


# ------------------------------------------------------------------ no token


async def test_no_token_sends_no_authorization_header():
    """Local daemon mode may legitimately have no token at all."""
    conn = EkeyConnection(host="127.0.0.1", port=8080)
    session = FakeSession()
    session.add("GET", "/app/v1/users", body=[])
    await EkeyAppClient(conn, session).async_get_users()
    assert "Authorization" not in session.calls[-1]["headers"]
