"""The sidebar panel: registration and static hosting of its JS module.

One panel for the whole integration rather than one per scanner. A Home Assistant
can have several scanners, each with its own user list, so the panel offers a
picker — the same shape as the existing services, which take a ``scanner`` field.

The module is a plain ES module served from this integration's directory. There is
deliberately no build step and no dependency on Home Assistant's bundled ``lit``:
the frontend's internal module paths are not a stable public interface, and a panel
that breaks on a frontend update is worse than a plainer one that does not.

``cache_headers=False`` is intentional. The URL carries the integration version as
a query parameter for cache-busting, but a panel serving stale JavaScript after an
update is a confusing failure to diagnose, and this file is a few tens of KB — not
worth optimising at the cost of that.
"""
from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import (
    DOMAIN,
    PANEL_COMPONENT_NAME,
    PANEL_ICON,
    PANEL_JS_URL,
    PANEL_STATIC_PATH,
    PANEL_TITLE,
    PANEL_URL_PATH,
)

_LOGGER = logging.getLogger(__name__)

# Underscore-prefixed: hass.data[DOMAIN] is keyed by config-entry id, and the code
# that counts loaded entries filters these bookkeeping keys out.
_REGISTERED = "_panel_registered"

# The resolved integration version, published on hass.data so code that is not the
# panel can stamp it onto things — a backup file records which build wrote it, and
# "which version made this" is the first question about a file that will not load.
PANEL_VERSION_KEY = "_panel_version"


async def async_register_panel(hass: HomeAssistant) -> None:
    """Serve the panel's JS and add the sidebar entry. Idempotent."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(_REGISTERED):
        return

    static_dir = Path(__file__).parent / "www"
    if not static_dir.is_dir():
        _LOGGER.error(
            "Panel assets missing at %s — the sidebar panel will not be available",
            static_dir,
        )
        return

    await hass.http.async_register_static_paths(
        [StaticPathConfig(PANEL_STATIC_PATH, str(static_dir), cache_headers=False)]
    )

    try:
        integration = await async_get_integration(hass, DOMAIN)
        version = str(integration.version or "dev")
    except Exception:  # noqa: BLE001 — a missing version must not cost us the panel
        version = "dev"

    domain_data[PANEL_VERSION_KEY] = version

    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=PANEL_COMPONENT_NAME,
        module_url=f"{PANEL_JS_URL}?v={version}",
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        # Everything this panel does is administration: enrolling a finger is
        # granting physical access to the building.
        require_admin=True,
        # The version is handed to the panel so it can SHOW which build is loaded.
        # This integration is installed by copying files onto a Home Assistant host,
        # and a stale copy is indistinguishable from a bug that was never fixed — the
        # panel looks and behaves exactly as it did before. Printing the version turns
        # that into a glance instead of an investigation.
        config={"domain": DOMAIN, "version": version},
    )

    domain_data[_REGISTERED] = True
    _LOGGER.debug("Registered the ekey panel at /%s", PANEL_URL_PATH)


def async_remove_panel(hass: HomeAssistant) -> None:
    """Remove the sidebar entry when the last config entry unloads.

    The static path stays registered: Home Assistant has no public way to remove
    one, and serving an unused JS file costs nothing.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    if not domain_data.get(_REGISTERED):
        return
    frontend.async_remove_panel(hass, PANEL_URL_PATH)
    domain_data[_REGISTERED] = False
