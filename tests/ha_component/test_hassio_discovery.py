"""Tests for the add-on's Supervisor discovery.

The add-on announces ``{host, port, token}`` on every start and whenever its token is
regenerated. The properties worth pinning down:

* an entry that already reaches that daemon **takes the new token over** and reloads,
  with no dialog — that is how a regenerated token reaches the integration;
* "localhost" in a hand-made entry and "127.0.0.1" from the add-on are **the same
  daemon**, never two entries;
* an ESP32 entry (HTTPS) is never mistaken for the add-on;
* with no entry yet, the operator confirms once and is **not asked for a token**.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.ekey_ha_app.config_flow import ConfigFlow


def _entry(host="127.0.0.1", port=8080, token="old", ssl=False):
    return SimpleNamespace(
        data={"host": host, "port": port, "ssl": ssl, "token": token, "verify_ssl": False}
    )


def _info(**config):
    base = {"host": "127.0.0.1", "port": 8080, "token": "new"}
    base.update(config)
    return SimpleNamespace(config=base, name="ekey module Daemon", slug="x", uuid="u")


def _flow(*entries):
    flow = ConfigFlow()
    flow.hass = SimpleNamespace(data={})
    flow.context = {"source": "hassio"}
    flow.flow_id = "flow-1"
    flow.handler = "ekey_ha_app"
    flow._async_current_entries = MagicMock(return_value=list(entries))
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()
    flow.async_update_reload_and_abort = MagicMock(return_value={"type": "abort"})
    flow._async_finish = AsyncMock(return_value={"type": "create_entry"})
    return flow


async def test_an_existing_entry_takes_the_new_token_over():
    entry = _entry(host="localhost", token="old")
    flow = _flow(entry)

    await flow.async_step_hassio(_info(token="new"))

    flow.async_update_reload_and_abort.assert_called_once_with(
        entry, data_updates={"token": "new"}, reason="already_configured"
    )
    flow._async_finish.assert_not_called()


async def test_the_same_token_changes_nothing():
    flow = _flow(_entry(token="new"))

    result = await flow.async_step_hassio(_info(token="new"))

    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"
    flow.async_update_reload_and_abort.assert_not_called()


async def test_an_esp32_entry_is_not_the_add_on():
    flow = _flow(_entry(ssl=True, token="device"))

    result = await flow.async_step_hassio(_info())

    flow.async_update_reload_and_abort.assert_not_called()
    assert result["type"] == "form"
    assert result["step_id"] == "hassio_confirm"


async def test_a_new_add_on_is_confirmed_once_without_asking_for_a_token():
    flow = _flow()

    result = await flow.async_step_hassio(_info(token=" tok "))
    assert result["type"] == "form"
    assert result["step_id"] == "hassio_confirm"
    assert len(result["data_schema"].schema) == 0          # nothing to fill in
    flow.async_set_unique_id.assert_awaited_once_with("127.0.0.1:8080")

    await flow.async_step_hassio_confirm({})
    data = flow._async_finish.await_args.args[0]
    assert data == {
        "host": "127.0.0.1",
        "port": 8080,
        "ssl": False,
        "token": "tok",
        "verify_ssl": False,
    }


async def test_a_malformed_port_aborts():
    flow = _flow()

    result = await flow.async_step_hassio(_info(port="eighty"))

    assert result["type"] == "abort"
    assert result["reason"] == "invalid_discovery_info"
