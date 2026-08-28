"""Tests for the serial port: the client calls, and the options flow that drives them.

The port is a setting rather than a compile-time constant, and the config entry's
**Configure** dialog is now the one place in Home Assistant it can be changed — it used
to be a card on the sidebar panel. What is worth pinning down is not that a GET returns
a dict; it is the handful of answers that mean something specific:

* a backend where the port is **not** a setting answers **501**, and that has to arrive
  as :class:`EkeyNotFoundError` so the dialog omits the step instead of offering a
  control that cannot work;
* **409** means "authorised, but this port is set somewhere else" (the add-on's own
  configuration, or ``-d`` on the command line) — a different thing from a bad request,
  and the flow says so differently;
* the PUT reply **is** the new state, which is what lets the flow report ``applies``
  without a second read that could disagree with it;
* ``confirm_console`` must reach the backend, because that is the only guard against
  quietly switching the machine's own terminal into RS485 mode.
"""
from types import SimpleNamespace

import pytest
import voluptuous as vol

from custom_components.ekey_ha_app.api import (
    EkeyApiError,
    EkeyAppClient,
    EkeyAuthError,
    EkeyNotFoundError,
)
from custom_components.ekey_ha_app.config_flow import EkeyOptionsFlow
from custom_components.ekey_ha_app.connection import EkeyConnection
from custom_components.ekey_ha_app.const import DOMAIN

from .fake_http import FakeSession

CONN = EkeyConnection(host="dev.local", port=8080, use_ssl=False, token="tok")

BODY = {
    "selected": "/dev/serial/by-id/usb-ekey-if00-port0",
    "active": "",
    "source": "file",
    "editable": True,
    "bound": False,
    "applies": "immediately",
    "ports": [
        # The by_id alias is on the entry that has one, because that is the shape the
        # daemon actually emits for a USB adapter — and the selection is stored under
        # that name, not the node. See test_form_preselects_a_by_id_selection.
        {"path": "/dev/ttyUSB0", "label": "ekey FSX CONVERTER (ftdi_sio)", "kind": "usb",
         "by_id": "/dev/serial/by-id/usb-ekey-if00-port0"},
        {"path": "/dev/ttyS0", "label": "ttyS0 (serial8250)", "kind": "internal",
         "console": True},
    ],
}


def make():
    session = FakeSession()
    return EkeyAppClient(CONN, session), session


# ------------------------------------------------------------------------- GET


async def test_get_serial_returns_the_body():
    client, session = make()
    session.add("GET", "/app/v1/serial", body=BODY)
    result = await client.async_get_serial()
    assert result["selected"] == BODY["selected"]
    assert result["editable"] is True
    # Internal ports are part of the list, not filtered out by the backend.
    assert [p["kind"] for p in result["ports"]] == ["usb", "internal"]


async def test_get_serial_not_a_setting_is_not_found():
    """A device with the sensor on fixed pins answers 501, and that is information."""
    client, session = make()
    session.add("GET", "/app/v1/serial", status=501,
                body={"error": "this backend does not choose its own serial port"})
    with pytest.raises(EkeyNotFoundError):
        await client.async_get_serial()


async def test_get_serial_unauthorized_is_its_own_error():
    client, session = make()
    session.add("GET", "/app/v1/serial", status=401, body={"error": "unauthorized"})
    with pytest.raises(EkeyAuthError):
        await client.async_get_serial()


async def test_get_serial_tolerates_a_non_dict_body():
    """Never hand a list to a caller that will read .get() off it."""
    client, session = make()
    session.add("GET", "/app/v1/serial", body=["not", "a", "dict"])
    assert await client.async_get_serial() == {}


# ------------------------------------------------------------------------- PUT


async def test_set_serial_sends_the_path_and_returns_the_new_state():
    client, session = make()
    saved = dict(BODY, selected="/dev/ttyUSB0", applies="restart", bound=True)
    session.add("PUT", "/app/v1/serial", body=saved)

    result = await client.async_set_serial("/dev/ttyUSB0")

    assert session.last_json("PUT", "/app/v1/serial") == {
        "path": "/dev/ttyUSB0",
        "confirm_console": False,
    }
    # The reply is the new state: this is what the panel renders, with no second read.
    assert result["selected"] == "/dev/ttyUSB0"
    assert result["applies"] == "restart"


async def test_set_serial_forwards_the_console_confirmation():
    client, session = make()
    session.add("PUT", "/app/v1/serial", body=BODY)
    await client.async_set_serial("/dev/ttyS0", confirm_console=True)
    assert session.last_json("PUT", "/app/v1/serial")["confirm_console"] is True


async def test_set_serial_refused_when_set_elsewhere():
    """409, not 400: the request was fine, the setting simply is not ours to change."""
    client, session = make()
    session.add("PUT", "/app/v1/serial", status=409,
                body={"error": "the serial port is set outside this page"})
    with pytest.raises(EkeyApiError) as err:
        await client.async_set_serial("/dev/ttyUSB0")
    assert "409" in str(err.value) or "outside" in str(err.value)


async def test_set_serial_rejected_path_is_an_error():
    client, session = make()
    session.add("PUT", "/app/v1/serial", status=400,
                body={"error": "/dev/nope is not a serial device on this host"})
    with pytest.raises(EkeyApiError):
        await client.async_set_serial("/dev/nope")


# ---------------------------------------------------------------- options flow
#
# The port moved off the panel and into the entry's Configure dialog, so these tests
# are about the decisions the flow makes that no HTTP test can see: which menu entries
# a given backend gets, that a console port cannot be selected in one step, and that the
# dialog's closing sentence comes from the reply's `applies` rather than from optimism.
# The flow is driven directly rather than through a real Home Assistant — the steps are
# plain coroutines returning dicts, and the objects they need are the config entry and
# hass.data, both small enough to stand in for.


class _Entry:
    """The two pieces of a ConfigEntry these steps touch."""

    def __init__(self, *, use_ssl: bool = False) -> None:
        self.entry_id = "entry-1"
        self.title = "ekey Scanner (dev.local:8080)"
        self.data = {
            "host": "dev.local",
            "port": 8080,
            "ssl": use_ssl,
            "token": "tok",
            "verify_ssl": False,
        }
        self.options: dict = {}


def options_flow(session: FakeSession, *, use_ssl: bool = False, caps=None):
    """An options flow whose entry is loaded, with a fake session under the client.

    ``caps`` stands in for the app coordinator's capabilities: ``None`` means the
    backend never said, which must lead to asking it rather than to hiding the step.
    """
    entry = _Entry(use_ssl=use_ssl)
    flow = EkeyOptionsFlow(entry)
    flow.hass = SimpleNamespace(
        data={
            DOMAIN: {
                entry.entry_id: {
                    "app_client": EkeyAppClient(CONN, session),
                    "app_coordinator": SimpleNamespace(capabilities=caps),
                }
            }
        }
    )
    flow.flow_id = "flow-1"
    flow.handler = DOMAIN
    return flow


def port_options(result) -> list[str]:
    """The values of the dropdown in a rendered serial form."""
    schema = result["data_schema"].schema
    key = next(k for k in schema if str(k) == "path")
    return [option["value"] for option in schema[key].config["options"]]


def port_default(result):
    """The preselected port, or None when the form offers no default."""
    schema = result["data_schema"].schema
    key = next(k for k in schema if str(k) == "path")
    if isinstance(key.default, vol.Undefined):
        return None
    return key.default()


async def test_local_entry_goes_straight_to_the_port_picker():
    """One menu entry is not a menu — the ordinary local-daemon case.

    A daemon has no Wi-Fi settings, so the port is the only thing this dialog does and
    an intermediate one-item menu would be a click that decides nothing.
    """
    session = FakeSession()
    session.add("GET", "/app/v1/serial", body=BODY)

    result = await options_flow(session).async_step_init()

    assert result["type"] == "form"
    assert result["step_id"] == "serial"
    assert port_options(result) == ["/dev/ttyUSB0", "/dev/ttyS0"]


async def test_remote_entry_gets_the_port_alongside_wifi():
    """A device that has both offers both, in one menu."""
    session = FakeSession()
    session.add("GET", "/app/v1/serial", body=BODY)

    result = await options_flow(session, use_ssl=True).async_step_init()

    assert result["type"] == "menu"
    assert result["menu_options"] == ["serial", "wifi_push", "wifi_reset"]


async def test_a_device_with_fixed_pins_is_not_offered_the_port():
    """501 means "this backend does not choose its own port" — so nothing is offered."""
    session = FakeSession()
    session.add("GET", "/app/v1/serial", status=501,
                body={"error": "this backend does not choose its own serial port"})

    result = await options_flow(session).async_step_init()

    assert result["type"] == "abort"
    assert result["reason"] == "no_options"


async def test_wifi_still_offered_when_the_port_is_not_a_setting():
    """The two halves are independent: no port must not cost an ESP32 its Wi-Fi steps."""
    session = FakeSession()
    session.add("GET", "/app/v1/serial", status=501, body={"error": "no"})

    result = await options_flow(session, use_ssl=True).async_step_init()

    assert result["type"] == "menu"
    assert result["menu_options"] == ["wifi_push", "wifi_reset"]


async def test_capabilities_saying_no_skips_the_request_entirely():
    """A backend that already told us it has no port setting is not asked again.

    Not an optimisation for its own sake: async_step_init blocks the dialog opening, so
    a pointless round trip to a device is latency the operator sees.
    """
    session = FakeSession()
    caps = SimpleNamespace(known=True, has_feature=lambda name: False)

    result = await options_flow(session, caps=caps).async_step_init()

    assert result["type"] == "abort"
    assert result["reason"] == "no_options"
    assert session.calls == []


async def test_capabilities_that_never_said_are_still_asked():
    """Unknown must not become "not offered" — that is the older-firmware case."""
    session = FakeSession()
    session.add("GET", "/app/v1/serial", body=BODY)
    caps = SimpleNamespace(known=False, has_feature=lambda name: False)

    result = await options_flow(session, caps=caps).async_step_init()

    assert result["step_id"] == "serial"


async def test_form_preselects_a_by_id_selection():
    """The stored name is a by-id alias; the dropdown carries raw nodes.

    Matching only on ``path`` therefore selected nothing for every USB adapter — the
    exact bug the daemon's own list had — so both names have to be checked.
    """
    session = FakeSession()
    session.add("GET", "/app/v1/serial", body=BODY)

    result = await options_flow(session).async_step_init()

    assert port_default(result) == "/dev/ttyUSB0"


async def test_form_has_no_default_when_nothing_is_chosen_yet():
    session = FakeSession()
    session.add("GET", "/app/v1/serial", body=dict(BODY, selected="", active=""))

    result = await options_flow(session).async_step_init()

    assert port_default(result) is None


async def test_a_port_that_is_set_elsewhere_is_read_only_and_says_where():
    """The add-on case. Reporting which port is in use is the value of the step here."""
    session = FakeSession()
    session.add("GET", "/app/v1/serial", body=dict(
        BODY, editable=False, source="cli", active="/dev/ttyUSB0", bound=True,
        applies="restart"))

    result = await options_flow(session).async_step_init()

    assert result["type"] == "abort"
    assert result["reason"] == "serial_read_only"
    assert result["description_placeholders"]["port"] == "/dev/ttyUSB0"
    assert "add-on configuration" in result["description_placeholders"]["where"]


async def test_choosing_an_ordinary_port_saves_it_and_reports_no_restart():
    session = FakeSession()
    session.add("GET", "/app/v1/serial", body=BODY)
    session.add("PUT", "/app/v1/serial", body=dict(
        BODY, selected="/dev/ttyUSB0", applies="immediately"))

    flow = options_flow(session)
    await flow.async_step_init()
    result = await flow.async_step_serial({"path": "/dev/ttyUSB0"})

    assert session.last_json("PUT", "/app/v1/serial") == {
        "path": "/dev/ttyUSB0",
        "confirm_console": False,
    }
    assert result["type"] == "abort"
    assert result["reason"] == "serial_saved"
    assert result["description_placeholders"]["port"] == "/dev/ttyUSB0"


async def test_the_closing_message_follows_the_reply_not_the_request():
    """``applies`` is a fact about the backend right now, and only the reply has it."""
    session = FakeSession()
    session.add("GET", "/app/v1/serial", body=BODY)
    session.add("PUT", "/app/v1/serial", body=dict(
        BODY, selected="/dev/ttyUSB0", bound=True, applies="restart"))

    flow = options_flow(session)
    await flow.async_step_init()
    result = await flow.async_step_serial({"path": "/dev/ttyUSB0"})

    assert result["reason"] == "serial_saved_restart"


async def test_the_console_port_takes_a_second_deliberate_step():
    """Nothing is written until the confirmation, and then it carries confirm_console."""
    session = FakeSession()
    session.add("GET", "/app/v1/serial", body=BODY)
    session.add("PUT", "/app/v1/serial", body=dict(BODY, selected="/dev/ttyS0"))

    flow = options_flow(session)
    await flow.async_step_init()
    asked = await flow.async_step_serial({"path": "/dev/ttyS0"})

    assert asked["type"] == "form"
    assert asked["step_id"] == "serial_console"
    assert asked["description_placeholders"]["port"] == "/dev/ttyS0"
    # The whole point: the write has not happened yet.
    assert [c["method"] for c in session.calls] == ["GET"]

    saved = await flow.async_step_serial_console({"confirm": True})

    assert saved["type"] == "abort"
    assert session.last_json("PUT", "/app/v1/serial") == {
        "path": "/dev/ttyS0",
        "confirm_console": True,
    }


async def test_declining_the_console_returns_to_the_picker():
    """Declining is not an error, and must not cost the dialog."""
    session = FakeSession()
    session.add("GET", "/app/v1/serial", body=BODY)

    flow = options_flow(session)
    await flow.async_step_init()
    await flow.async_step_serial({"path": "/dev/ttyS0"})
    result = await flow.async_step_serial_console({"confirm": False})

    assert result["type"] == "form"
    assert result["step_id"] == "serial"
    assert [c["method"] for c in session.calls] == ["GET"]


async def test_a_409_is_reported_as_set_elsewhere_not_as_a_bad_port():
    """The distinction the backend went to the trouble of making has to survive here."""
    session = FakeSession()
    session.add("GET", "/app/v1/serial", body=BODY)
    session.add("PUT", "/app/v1/serial", status=409,
                body={"error": "the serial port is set outside this page"})

    flow = options_flow(session)
    await flow.async_step_init()
    result = await flow.async_step_serial({"path": "/dev/ttyUSB0"})

    assert result["type"] == "form"
    assert result["step_id"] == "serial"
    assert result["errors"] == {"base": "serial_elsewhere"}


async def test_a_rejected_port_re_shows_the_form_with_an_error():
    session = FakeSession()
    session.add("GET", "/app/v1/serial", body=BODY)
    session.add("PUT", "/app/v1/serial", status=400,
                body={"error": "/dev/ttyUSB0 is not a serial device on this host"})

    flow = options_flow(session)
    await flow.async_step_init()
    result = await flow.async_step_serial({"path": "/dev/ttyUSB0"})

    assert result["errors"] == {"base": "serial_failed"}
    # Still a usable form, not a dead end.
    assert port_options(result) == ["/dev/ttyUSB0", "/dev/ttyS0"]


async def test_a_rotated_token_is_an_auth_error_on_the_form():
    session = FakeSession()
    session.add("GET", "/app/v1/serial", body=BODY)
    session.add("PUT", "/app/v1/serial", status=401, body={"error": "unauthorized"})

    flow = options_flow(session)
    await flow.async_step_init()
    result = await flow.async_step_serial({"path": "/dev/ttyUSB0"})

    assert result["errors"] == {"base": "invalid_auth"}


async def test_an_empty_port_list_is_not_offered():
    """A backend that answers but enumerated nothing has no choice to present."""
    session = FakeSession()
    session.add("GET", "/app/v1/serial", body=dict(BODY, ports=[]))

    result = await options_flow(session).async_step_init()

    assert result["type"] == "abort"
    assert result["reason"] == "no_options"
