"""Tests for the sidebar panel registration.

Home Assistant sets up the config entries of one domain concurrently. With two
scanners — an ESP32 and the add-on, say — both entries reached
``async_register_panel`` while the first was still awaiting inside it, and the second
failed its whole setup on ``ValueError: Overwriting panel ekey owned by custom``.
What matters is therefore not that one call registers the panel, but that two
overlapping calls register it exactly once, and that a failed attempt leaves the
next one free to retry.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.ekey_ha_app import panel
from custom_components.ekey_ha_app.const import DOMAIN


async def _run_inline(func, *args):
    return func(*args)


def _hass():
    return SimpleNamespace(
        data={},
        http=SimpleNamespace(async_register_static_paths=AsyncMock()),
        async_add_executor_job=_run_inline,
    )


async def _slow_integration(*_args, **_kwargs):
    # Yield to the event loop, as the real lookup can: this is the gap the second
    # entry used to slip through.
    await asyncio.sleep(0)
    return SimpleNamespace(version="9.9.9")


async def test_concurrent_registrations_register_the_panel_once():
    hass = _hass()
    with patch.object(panel, "async_get_integration", _slow_integration), patch.object(
        panel.panel_custom, "async_register_panel", AsyncMock()
    ) as register:
        await asyncio.gather(
            panel.async_register_panel(hass), panel.async_register_panel(hass)
        )

    assert register.await_count == 1
    assert hass.http.async_register_static_paths.await_count == 1
    assert hass.data[DOMAIN][panel._REGISTERED] is True


async def test_a_failed_registration_can_be_retried():
    hass = _hass()
    with patch.object(panel, "async_get_integration", _slow_integration), patch.object(
        panel.panel_custom,
        "async_register_panel",
        AsyncMock(side_effect=[RuntimeError("boom"), None]),
    ) as register:
        with pytest.raises(RuntimeError):
            await panel.async_register_panel(hass)
        assert hass.data[DOMAIN][panel._REGISTERED] is False

        await panel.async_register_panel(hass)

    assert register.await_count == 2
    assert hass.data[DOMAIN][panel._REGISTERED] is True


async def test_module_url_follows_the_file_content(tmp_path):
    """Same version, different ekey-panel.js → different URL, so no stale cache.

    A fixed panel copied onto a test host without a version bump used to keep the
    URL ``?v=<version>``, and the browser kept running the old module.
    """
    js = tmp_path / "ekey-panel.js"

    async def module_url(content: bytes) -> str:
        js.write_bytes(content)
        hass = _hass()
        with patch.object(
            panel, "async_get_integration", _slow_integration
        ), patch.object(
            panel.panel_custom, "async_register_panel", AsyncMock()
        ) as register:
            await panel._async_register(hass, hass.data, tmp_path)
        assert hass.data[panel.PANEL_VERSION_KEY] == "9.9.9"  # display value untouched
        return register.await_args.kwargs["module_url"]

    first = await module_url(b"console.log(1);")
    again = await module_url(b"console.log(1);")
    changed = await module_url(b"console.log(2);")

    assert first.startswith("/ekey_ha_app_static/ekey-panel.js?v=9.9.9-")
    assert first == again
    assert changed != first


async def test_module_url_falls_back_to_the_version_when_unreadable(tmp_path):
    hass = _hass()
    with patch.object(panel, "async_get_integration", _slow_integration), patch.object(
        panel.panel_custom, "async_register_panel", AsyncMock()
    ) as register:
        await panel._async_register(hass, hass.data, tmp_path)  # no ekey-panel.js here

    assert register.await_args.kwargs["module_url"].endswith("?v=9.9.9")
