"""Auction Watcher: follow your online auctions from Home Assistant."""

from __future__ import annotations

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.typing import ConfigType

from .const import CONF_COOKIE, DOMAIN, SERVICE_REFRESH, SERVICE_SET_BID_LIMIT
from .coordinator import AuctionWatcherCoordinator
from .cookies import parse_cookie_input
from .sites import govauctions as ga

PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type AuctionWatcherConfigEntry = ConfigEntry[AuctionWatcherCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's services once."""

    def _coordinators() -> list[AuctionWatcherCoordinator]:
        return [
            e.runtime_data for e in hass.config_entries.async_entries(DOMAIN)
            if e.state is ConfigEntryState.LOADED
            and isinstance(getattr(e, "runtime_data", None), AuctionWatcherCoordinator)
        ]

    async def set_bid_limit(call: ServiceCall) -> None:
        auction_id = str(call.data["auction_id"]).strip()
        targets = [c for c in _coordinators() if c.data and auction_id in c.data]
        if not targets:
            raise ServiceValidationError(f"Auction {auction_id} isn't one you're following")
        for coord in targets:
            await coord.async_set_bid_limit(auction_id, int(call.data["limit"]))

    async def refresh(call: ServiceCall) -> None:
        for coord in _coordinators():
            await coord.async_request_refresh()

    hass.services.async_register(
        DOMAIN, SERVICE_SET_BID_LIMIT, set_bid_limit,
        schema=vol.Schema({
            vol.Required("auction_id"): cv.string,
            vol.Required("limit"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100000)),
        }),
    )
    hass.services.async_register(DOMAIN, SERVICE_REFRESH, refresh)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: AuctionWatcherConfigEntry) -> bool:
    session = async_create_clientsession(hass, cookie_jar=aiohttp.DummyCookieJar())
    cookies = parse_cookie_input(entry.data[CONF_COOKIE], domain=ga.COOKIE_DOMAIN)
    coordinator = AuctionWatcherCoordinator(hass, entry, ga.GovAuctionsClient(session, cookies))
    await coordinator.async_load()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    return True


async def _async_entry_updated(hass: HomeAssistant, entry: AuctionWatcherConfigEntry) -> None:
    # Only reload when the user changed options, not when we saved refreshed cookies
    if dict(entry.options) != entry.runtime_data.options_snapshot:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: AuctionWatcherConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
