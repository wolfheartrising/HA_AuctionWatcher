"""Auction Watcher: follow your online auctions from Home Assistant."""

from __future__ import annotations

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_COOKIE,
    DOMAIN,
    SERVICE_BOOK,
    SERVICE_PLACE_BID,
    SERVICE_SET_MACBID,
    SERVICE_CANCEL,
    SERVICE_REFRESH,
    SERVICE_SET_AUTO,
    SERVICE_SET_BID_LIMIT,
)
from .const import (
    CONF_APP_ID,
    CONF_CERT_ID,
    CONF_EBAY_TOKENS,
    CONF_RUNAME,
    SITE_EBAY,
    CONF_AUTH,
    CONF_DEVICE_ID,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_SITE,
    SITE_MACBID,
)
from .coordinator import AuctionWatcherCoordinator, EbayCoordinator, MacBidCoordinator
from .cookies import parse_cookie_input
from .sites import ebay as eb
from .sites import macbid as mb
from .logic import AutoSettings, MacBidSettings
from .sites.base import SiteError
from .panel import async_register_panel, async_remove_panel
from .search_manager import DATA_KEY as SEARCHES_KEY
from .search_manager import SearchManager, async_register_ws
from . import global_settings, proxy_manager
from .sites import govauctions as ga

PLATFORMS = [Platform.SENSOR, Platform.SWITCH]
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

    async def _require_admin(call: ServiceCall) -> None:
        """Anything that spends points or changes limits is admin-only.
        Calls with no user (your own automations, scripts) are allowed."""
        user_id = call.context.user_id
        if user_id is None:
            return
        user = await hass.auth.async_get_user(user_id)
        if user is None or not user.is_admin:
            raise Unauthorized(context=call.context)

    async def set_bid_limit(call: ServiceCall) -> None:
        await _require_admin(call)
        auction_id = str(call.data["auction_id"]).strip()
        targets = [c for c in _coordinators() if c.data and auction_id in c.data]
        if not targets:
            raise ServiceValidationError(f"Auction {auction_id} isn't one you're following")
        for coord in targets:
            await coord.async_set_bid_limit(auction_id, int(call.data["limit"]))

    async def refresh(call: ServiceCall) -> None:
        site = call.data.get("site")
        for coord in _coordinators():
            if not site or coord.config_entry.data.get(CONF_SITE) == site:
                await coord.async_request_refresh()

    def _owner(auction_id: str) -> AuctionWatcherCoordinator:
        for coord in _coordinators():
            if coord.data and auction_id in coord.data:
                return coord
        raise ServiceValidationError(f"Auction {auction_id} isn't one you're following")

    async def book_bids(call: ServiceCall) -> ServiceResponse:
        await _require_admin(call)
        aid = str(call.data["auction_id"]).strip()
        try:
            booked = await _owner(aid).async_book(aid, call.data["count"], call.data.get("max_price"))
        except SiteError as err:
            raise HomeAssistantError(str(err)) from err
        return {"booked": booked}

    async def cancel_booked(call: ServiceCall) -> None:
        await _require_admin(call)
        aid = str(call.data["auction_id"]).strip()
        try:
            await _owner(aid).async_cancel(aid)
        except SiteError as err:
            raise HomeAssistantError(str(err)) from err

    async def set_auto(call: ServiceCall) -> None:
        await _require_admin(call)
        aid = str(call.data["auction_id"]).strip()
        coord = _owner(aid)
        current = vars(coord.auto_settings(aid))
        settings = AutoSettings.from_dict({**current, **{k: v for k, v in call.data.items() if k != "auction_id"}})
        try:
            await coord.async_set_auto(aid, settings)
        except SiteError as err:
            raise ServiceValidationError(str(err)) from err

    async def place_bid(call: ServiceCall) -> ServiceResponse:
        await _require_admin(call)
        aid = str(call.data["auction_id"]).strip()
        coord = _owner(aid)
        if not hasattr(coord, "async_place_bid"):
            raise ServiceValidationError("Max bids from Home Assistant are only for Mac.bid")
        try:
            amount = await coord.async_place_bid(aid, int(call.data["amount"]), bool(call.data.get("buyers_assurance", False)))
        except SiteError as err:
            raise HomeAssistantError(str(err)) from err
        return {"placed": amount}

    async def set_macbid(call: ServiceCall) -> None:
        await _require_admin(call)
        aid = str(call.data["auction_id"]).strip()
        coord = _owner(aid)
        if not hasattr(coord, "async_set_macbid"):
            raise ServiceValidationError("Auto-bidding settings are only for Mac.bid")
        current = vars(coord.macbid_settings(aid))
        settings = MacBidSettings.from_dict({**current, **{k: v for k, v in call.data.items() if k != "auction_id"}})
        try:
            await coord.async_set_macbid(aid, settings)
        except SiteError as err:
            raise ServiceValidationError(str(err)) from err

    hass.services.async_register(
        DOMAIN, SERVICE_PLACE_BID, place_bid,
        schema=vol.Schema({
            vol.Required("auction_id"): cv.string,
            vol.Required("amount"): vol.All(vol.Coerce(int), vol.Range(min=1, max=100000)),
            vol.Optional("buyers_assurance", default=False): cv.boolean,
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_MACBID, set_macbid,
        schema=vol.Schema({
            vol.Required("auction_id"): cv.string,
            vol.Optional("mode"): vol.In(["off", "now", "late"]),
            vol.Optional("late_seconds"): vol.All(vol.Coerce(int), vol.Range(min=30, max=3600)),
            vol.Optional("assurance"): cv.boolean,
        }),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_BOOK, book_bids,
        schema=vol.Schema({
            vol.Required("auction_id"): cv.string,
            vol.Required("count"): vol.All(vol.Coerce(int), vol.Range(min=1, max=10000)),
            vol.Optional("max_price"): vol.All(vol.Coerce(float), vol.Range(min=0)),
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CANCEL, cancel_booked,
        schema=vol.Schema({vol.Required("auction_id"): cv.string}),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_AUTO, set_auto,
        schema=vol.Schema({
            vol.Required("auction_id"): cv.string,
            vol.Optional("auto_cancel"): cv.boolean,
            vol.Optional("auto_rebook"): cv.boolean,
            vol.Optional("rebook_mode"): vol.In(["fixed", "remaining"]),
            vol.Optional("rebook_amount"): vol.All(vol.Coerce(int), vol.Range(min=1, max=10000)),
            vol.Optional("topup_at"): vol.All(vol.Coerce(int), vol.Range(min=0, max=1000)),
            vol.Optional("max_price"): vol.All(vol.Coerce(float), vol.Range(min=0)),
        }),
    )

    hass.services.async_register(
        DOMAIN, SERVICE_SET_BID_LIMIT, set_bid_limit,
        schema=vol.Schema({
            vol.Required("auction_id"): cv.string,
            vol.Required("limit"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100000)),
        }),
    )
    hass.services.async_register(DOMAIN, SERVICE_REFRESH, refresh, schema=vol.Schema({vol.Optional("site"): cv.string}))

    async def follow(call: ServiceCall) -> None:
        """Follow an auction you aren't in yet (GovAuctions bookmark / Mac.bid or eBay watchlist), e.g. a point
        pack, so it gets a full card with booking; optionally set its max at the same time."""
        await _require_admin(call)
        site = call.data.get("site", "govauctions")
        coord = next((e.runtime_data for e in hass.config_entries.async_entries(DOMAIN)
                      if e.state is ConfigEntryState.LOADED and e.data.get(CONF_SITE, "govauctions") == site), None)
        if coord is None or not hasattr(coord, "async_watch"):
            raise ServiceValidationError(f"No {site} account is set up")
        aid = str(call.data["auction_id"]).strip()
        try:
            await coord.async_watch(aid, True)
            if call.data.get("limit"):
                await coord.async_set_bid_limit(aid, int(call.data["limit"]))
        except SiteError as err:
            raise HomeAssistantError(str(err)) from err

    hass.services.async_register(
        DOMAIN, "follow", follow,
        schema=vol.Schema({
            vol.Required("auction_id"): cv.string,
            vol.Optional("site", default="govauctions"): vol.In(["govauctions", "macbid", "ebay"]),
            vol.Optional("limit"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100000)),
        }),
    )

    async def search_now(call: ServiceCall) -> None:
        """Run the saved searches now (they also run daily at the time set on the Auctions page)."""
        manager: SearchManager | None = hass.data.get(SEARCHES_KEY)
        if manager is None:
            raise ServiceValidationError("Auction Watcher isn't set up yet")
        await manager.async_run()

    hass.services.async_register(DOMAIN, "search_now", search_now)
    async_register_ws(hass)
    global_settings.async_register_ws(hass)
    proxy_manager.async_register_ws(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: AuctionWatcherConfigEntry) -> bool:
    # Alert targets are shared by all sites (Settings tab); older per-site targets are brought in once
    if global_settings.DATA_KEY not in hass.data:
        g = global_settings.GlobalSettings(hass)
        await g.async_load()
        hass.data.setdefault(global_settings.DATA_KEY, g)  # both accounts start together: keep the first
    await hass.data[global_settings.DATA_KEY].async_adopt_entry(entry)
    if proxy_manager.DATA_KEY not in hass.data:
        pm = proxy_manager.ProxyManager(hass)
        await pm.async_load()
        if hass.data.setdefault(proxy_manager.DATA_KEY, pm) is not pm:
            pm.stop()  # another account set it up meanwhile: one listener for Telegram, not two
    session = async_create_clientsession(hass, cookie_jar=aiohttp.DummyCookieJar())
    if entry.data.get(CONF_SITE) == SITE_EBAY:
        client_e = eb.EbayClient(
            session, entry.data[CONF_APP_ID], entry.data[CONF_CERT_ID],
            runame=entry.data.get(CONF_RUNAME), user_auth=entry.data.get(CONF_EBAY_TOKENS),
        )
        coordinator: AuctionWatcherCoordinator = EbayCoordinator(hass, entry, client_e)
    elif entry.data.get(CONF_SITE) == SITE_MACBID:
        client = mb.MacBidClient(
            session, entry.data.get(CONF_AUTH), entry.data[CONF_DEVICE_ID],
            email=entry.data.get(CONF_EMAIL), password=entry.data.get(CONF_PASSWORD),
        )
        # Save every new Mac.bid login the moment it arrives: the old one stops working right away
        client.on_auth_changed = lambda auth: hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_AUTH: auth})
        coordinator = MacBidCoordinator(hass, entry, client)
    else:
        cookies = parse_cookie_input(entry.data[CONF_COOKIE], domain=ga.COOKIE_DOMAIN)
        coordinator = AuctionWatcherCoordinator(hass, entry, ga.GovAuctionsClient(session, cookies))
    await coordinator.async_load()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    await async_register_panel(hass)
    if SEARCHES_KEY not in hass.data:
        manager = SearchManager(hass)
        await manager.async_load()  # read the saved searches and results first...
        if SEARCHES_KEY not in hass.data:  # ...then make it available (another account may have set up meanwhile)
            hass.data[SEARCHES_KEY] = manager
        else:
            manager.stop()
    return True


async def _async_entry_updated(hass: HomeAssistant, entry: AuctionWatcherConfigEntry) -> None:
    # Only reload when the user changed options, not when we saved refreshed cookies
    if dict(entry.options) != entry.runtime_data.options_snapshot:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: AuctionWatcherConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    others_loaded = any(
        e.state is ConfigEntryState.LOADED
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.entry_id != entry.entry_id
    )
    if unloaded and not others_loaded:
        async_remove_panel(hass)
        manager = hass.data.pop(SEARCHES_KEY, None)
        if manager is not None:
            manager.stop()
        hass.data.pop(global_settings.DATA_KEY, None)
        pm = hass.data.pop(proxy_manager.DATA_KEY, None)
        if pm is not None:
            pm.stop()
    return unloaded
