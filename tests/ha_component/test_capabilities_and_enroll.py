"""Tests for capability detection, the enrolment wording, and the uniqueness rule.

The capability tests exist because of one specific failure mode: claiming an
ability the backend never advertised. An action type whose support is *unknown*
must behave exactly like one that is unsupported, or the failure moves from
"the UI told me" to "the door did not open at 3 a.m."
"""
import pytest

from custom_components.ekey_ha_app.api import EkeyAppClient, EkeyAuthError
from custom_components.ekey_ha_app.capabilities import (
    SOURCE_ABSENT,
    SOURCE_ENDPOINT,
    SOURCE_PROBE,
    SOURCE_UNAVAILABLE,
    Capabilities,
    async_detect,
)
from custom_components.ekey_ha_app.connection import EkeyConnection
from custom_components.ekey_ha_app.enroll import progress_text
from custom_components.ekey_ha_app.ws_api import _check_person_unique

from .fake_http import FakeSession

CONN = EkeyConnection(host="dev.local", port=8080, use_ssl=True, token="tok")

FULL_CAPS = {
    "platform": "linux",
    "core_version": "1.0.0",
    "action_types": [
        {"type": "led", "supported": True},
        {"type": "webhook", "supported": True},
        {"type": "gpio", "supported": False,
         "reason": "this backend has no directly attached GPIO"},
    ],
    "trigger_kinds": ["match_ok", "match_nok", "touch"],
    "template_tokens": ["apid", "username", "result", "ts", "finger"],
    "features": {"event_log": True, "wifi": False},
}


def client(session):
    return EkeyAppClient(CONN, session)


# ------------------------------------------------------------- the endpoint


async def test_endpoint_capabilities_are_parsed():
    session = FakeSession()
    session.add("GET", "/app/v1/capabilities", body=FULL_CAPS)
    caps = await async_detect(client(session))

    assert caps.source == SOURCE_ENDPOINT
    assert caps.known is True
    assert caps.has_app_api is True
    assert caps.platform == "linux"
    assert caps.supports_action("led") is True
    assert caps.supports_action("gpio") is False
    assert caps.action_reason("gpio") == "this backend has no directly attached GPIO"
    assert caps.action_reason("led") is None
    assert caps.has_feature("event_log") is True
    assert caps.has_feature("wifi") is False
    # A backend answering this endpoint has users by definition.
    assert caps.has_feature("users") is True


async def test_action_types_may_also_be_a_bare_list_of_names():
    session = FakeSession()
    session.add("GET", "/app/v1/capabilities", body={"action_types": ["led", "knx"]})
    caps = await async_detect(client(session))
    assert caps.supports_action("led") is True
    assert caps.supports_action("knx") is True
    assert caps.supports_action("gpio") is False


async def test_an_unlisted_action_type_is_unsupported():
    session = FakeSession()
    session.add("GET", "/app/v1/capabilities", body=FULL_CAPS)
    caps = await async_detect(client(session))
    assert caps.supports_action("mqtt") is False
    assert caps.action_reason("mqtt") == "not supported by this backend"


# ------------------------------------------------------------ the fallbacks


async def test_no_endpoint_but_users_present_is_a_probe_result():
    session = FakeSession()
    session.add("GET", "/app/v1/capabilities", status=404, body={})
    session.add("GET", "/app/v1/users", body=[])
    caps = await async_detect(client(session))

    assert caps.source == SOURCE_PROBE
    assert caps.has_app_api is True
    assert caps.known is False
    assert caps.has_feature("users") is True
    # Nothing else is claimed — that is the whole point.
    assert caps.supports_action("led") is False
    assert "does not report" in caps.action_reason("led")


async def test_no_app_layer_at_all():
    session = FakeSession()
    session.add("GET", "/app/v1/capabilities", status=404, body={})
    session.add("GET", "/app/v1/users", status=404, body={})
    caps = await async_detect(client(session))

    assert caps.source == SOURCE_ABSENT
    assert caps.has_app_api is False


async def test_auth_failure_is_raised_not_downgraded():
    """A rotated token must surface as reauth, never as 'too old'."""
    session = FakeSession()
    session.add("GET", "/app/v1/capabilities", status=401, body={})
    with pytest.raises(EkeyAuthError):
        await async_detect(client(session))


async def test_transport_failure_is_unavailable_not_absent():
    session = FakeSession()
    session.add("GET", "/app/v1/capabilities", status=500, body="boom")
    caps = await async_detect(client(session))
    assert caps.source == SOURCE_UNAVAILABLE
    assert caps.has_app_api is False


def test_as_dict_round_trips_the_fields_the_panel_reads():
    caps = Capabilities(source=SOURCE_ENDPOINT, platform="esp32",
                        action_types={"led": True}, features={"users": True})
    payload = caps.as_dict()
    assert payload["has_app_api"] is True
    assert payload["known"] is True
    assert payload["action_types"] == {"led": True}


# ---------------------------------------------------- enrolment wording


@pytest.mark.parametrize(
    "enstat,expected_fragment",
    [
        (10, "Waiting"),
        (20, "Reading the finger"),
        (35, "Captures complete"),
        (40, "Enrolled."),
        (50, "Cancelled."),
        (60, "timed out"),
        (70, "already enrolled"),
    ],
)
def test_progress_text_covers_every_terminal_state(enstat, expected_fragment):
    assert expected_fragment in progress_text(enstat, 1, 1, 0, 0)


def test_progress_text_explains_the_retry_reasons():
    # These are the messages that actually help someone standing at the sensor.
    assert "same finger" in progress_text(30, 2, 1, 0, 30)
    assert "centred" in progress_text(30, 2, 1, 20, 0)
    assert "Clean the sensor" in progress_text(30, 2, 1, 30, 0)
    assert "too wet" in progress_text(30, 2, 1, 70, 0)
    assert "accepted" in progress_text(30, 2, 3, 0, 0)


def test_progress_text_survives_an_unknown_state():
    assert "State 99" in progress_text(99, 0, 0, 0, 0)


# --------------------------------------------- one person, one user, per scanner


def test_person_uniqueness_allows_a_free_person():
    users = [{"id": "u1", "username": "Jane", "ha_person": "person.jane"}]
    _check_person_unique(users, "person.bob")   # must not raise


def test_person_uniqueness_rejects_a_second_user_for_one_person():
    users = [{"id": "u1", "username": "Jane", "ha_person": "person.jane"}]
    with pytest.raises(ValueError) as excinfo:
        _check_person_unique(users, "person.jane")
    assert "already linked" in str(excinfo.value)


def test_person_uniqueness_ignores_the_user_being_edited():
    users = [{"id": "u1", "username": "Jane", "ha_person": "person.jane"}]
    _check_person_unique(users, "person.jane", exclude_id="u1")   # must not raise


def test_person_uniqueness_allows_unlinking():
    users = [{"id": "u1", "username": "Jane", "ha_person": "person.jane"}]
    _check_person_unique(users, None)   # must not raise


# --------------------------------------------------- refresh-before-announce
#
# The ordering that made an enrollment look like it did not refresh the list.
# ws_users_get is served from the coordinator's CACHED snapshot, and the panel
# reloads the instant it sees the terminal progress message — so if the cache is
# refreshed after the announcement, the panel reads the state from before the
# write and shows a user without the finger just enrolled.


class _OrderRecorder:
    """Records the sequence of side effects _succeed() produces."""

    def __init__(self) -> None:
        self.events: list[str] = []


class _FakeBus:
    def __init__(self, log: _OrderRecorder) -> None:
        self._log = log

    def async_fire(self, event_type, data=None):
        self._log.events.append(f"fire:{event_type}")

    def async_listen(self, *args, **kwargs):
        return lambda: None


class _FakeHass:
    def __init__(self, log: _OrderRecorder) -> None:
        self.bus = _FakeBus(log)
        self.data: dict = {}


class _FakeCoordinator:
    def __init__(self, log: _OrderRecorder) -> None:
        self._log = log

    async def async_refresh_now(self):
        self._log.events.append("refresh")


class _FakeClient:
    def __init__(self, log: _OrderRecorder, users) -> None:
        self._log = log
        self._users = users

    async def async_get_users(self):
        return [dict(u) for u in self._users]

    async def async_put_users(self, users):
        self._log.events.append("write")
        self._users = users


async def test_the_cache_is_refreshed_before_the_enrollment_is_announced():
    from custom_components.ekey_ha_app.enroll import EnrollManager, EnrollSession

    log = _OrderRecorder()
    hass = _FakeHass(log)
    users = [{"id": "u1", "username": "Jane", "fingers": []}]
    manager = EnrollManager(hass, "entry1", _FakeClient(log, users), _FakeCoordinator(log))

    session = EnrollSession("AP-1", "u1", "Jane", 2)
    await manager._succeed(session)

    assert session.done and session.ok, "the enrollment finished successfully"

    # write → refresh → THEN the two announcements. Anything else and the panel
    # reloads into a stale snapshot.
    assert log.events[0] == "write"
    assert log.events[1] == "refresh"
    assert log.events[2].startswith("fire:"), "progress is announced after the refresh"
    assert "refresh" not in log.events[2:], "and the refresh does not happen twice"


async def test_a_refresh_failure_still_finishes_the_enrollment():
    """The fingerprint IS on the scanner; a stale list must not hide that."""
    from custom_components.ekey_ha_app.enroll import EnrollManager, EnrollSession

    log = _OrderRecorder()
    hass = _FakeHass(log)

    class _BrokenCoordinator:
        async def async_refresh_now(self):
            raise RuntimeError("backend unreachable")

    users = [{"id": "u1", "username": "Jane", "fingers": []}]
    manager = EnrollManager(hass, "entry1", _FakeClient(log, users), _BrokenCoordinator())

    session = EnrollSession("AP-1", "u1", "Jane", 2)
    await manager._succeed(session)

    assert session.done and session.ok
    assert any(e.startswith("fire:") for e in log.events), "it was still announced"
