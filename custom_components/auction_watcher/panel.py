"""The "Auctions" sidebar page.

The page is one JavaScript file (frontend/auction-watcher-panel.js) that reads
the auction sensors straight from Home Assistant's state, so it needs no
extra API. It appears when the first site is set up and goes away when the
last one is removed.
"""

from __future__ import annotations

from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN, VERSION

PANEL_URL_PATH = "auction-watcher"
STATIC_URL = "/auction_watcher_static"
WEBCOMPONENT = "auction-watcher-panel"
_STATIC_DONE = f"{DOMAIN}_static_registered"
_PANEL_DONE = f"{DOMAIN}_panel_registered"


async def async_register_panel(hass: HomeAssistant) -> None:
    if not hass.data.get(_STATIC_DONE):
        # Static paths can't be removed again, so this happens once per Home Assistant run
        await hass.http.async_register_static_paths([
            StaticPathConfig(STATIC_URL, str(Path(__file__).parent / "frontend"), cache_headers=False)
        ])
        hass.data[_STATIC_DONE] = True
    if hass.data.get(_PANEL_DONE):
        return
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=WEBCOMPONENT,
        sidebar_title="Auctions",
        sidebar_icon="mdi:gavel",
        # ?v= makes browsers fetch the new file after an update
        module_url=f"{STATIC_URL}/{WEBCOMPONENT}.js?v={VERSION}",
        require_admin=False,
        config={},
    )
    hass.data[_PANEL_DONE] = True


def async_remove_panel(hass: HomeAssistant) -> None:
    if hass.data.pop(_PANEL_DONE, False):
        frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)
