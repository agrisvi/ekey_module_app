"""Tests for where the panel finds each scanner's own page.

A loopback entry is the add-on: from the BROWSER, 127.0.0.1 is the browser's own
machine, so it must never be offered as a URL — the panel frames it through ingress.
Anything else is framed by its address, with the stored token handed over.
"""
from custom_components.ekey_ha_app.ws_api import scanner_page


def test_a_device_is_opened_by_its_https_address_with_its_token():
    page = scanner_page(
        {"host": "fp-sensor.local", "port": 8080, "ssl": True, "token": "tok"}
    )
    assert page == {
        "page_url": "https://fp-sensor.local:8080/",
        "page_addon": False,
        "page_token": "tok",
    }


def test_a_loopback_daemon_is_the_add_on_and_has_no_url():
    for host in ("127.0.0.1", "localhost", "::1"):
        page = scanner_page({"host": host, "port": 8080, "ssl": False, "token": "t"})
        assert page["page_addon"] is True
        assert page["page_url"] == ""


def test_a_daemon_elsewhere_is_opened_over_http():
    page = scanner_page({"host": "192.168.10.50", "port": 8080, "ssl": False})
    assert page["page_url"] == "http://192.168.10.50:8080/"
    assert page["page_addon"] is False
    assert page["page_token"] == ""


def test_an_ipv6_address_is_bracketed():
    page = scanner_page({"host": "fd00::5", "port": 8080, "ssl": True, "token": "t"})
    assert page["page_url"] == "https://[fd00::5]:8080/"
