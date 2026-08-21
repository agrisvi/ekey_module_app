"""Tests for the serial-port half of the client.

The port is now a setting rather than a compile-time constant, and this integration is
one of the two places it can be changed. What is worth pinning down is not that a GET
returns a dict — it is the handful of answers that mean something specific:

* a backend where the port is **not** a setting answers **501**, and that has to arrive
  as :class:`EkeyNotFoundError` so the panel omits the section instead of showing a
  control that cannot work;
* **409** means "authorised, but this port is set somewhere else" (the add-on's own
  configuration, or ``-d`` on the command line) — a different thing from a bad request,
  and the panel says so differently;
* the PUT reply **is** the new state, which is what lets the panel avoid a second read
  that could disagree with it;
* ``confirm_console`` must reach the backend, because that is the only guard against
  quietly switching the machine's own terminal into RS485 mode.
"""
import pytest

from custom_components.ekey_ha_app.api import (
    EkeyApiError,
    EkeyAppClient,
    EkeyAuthError,
    EkeyNotFoundError,
)
from custom_components.ekey_ha_app.connection import EkeyConnection

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
        {"path": "/dev/ttyUSB0", "label": "ekey FSX CONVERTER (ftdi_sio)", "kind": "usb"},
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
