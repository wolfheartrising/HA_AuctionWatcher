"""Set up an auction site account by pasting its cookies."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    FileSelector,
    FileSelectorConfig,
    SelectSelectorMode,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_MACBID_BUDGET,
    CONF_WEEKLY_BUDGET,
    CONF_PACKS_ENABLED,
    CONF_APP_ID,
    CONF_CERT_ID,
    CONF_EBAY_TOKENS,
    CONF_EBAY_USER,
    CONF_RUNAME,
    SITE_EBAY,
    CONF_KEEP_ENDED_DAYS,
    DEFAULT_KEEP_ENDED_DAYS,
    CONF_ENDING_MINUTES,
    CONF_PICKUP_HOURS,
    DEFAULT_ENDING_MINUTES,
    DEFAULT_PICKUP_HOURS,
    CONF_AUTH,
    CONF_DEVICE_ID,
    CONF_EMAIL,
    CONF_PASSWORD,
    SITE_MACBID,
    CONF_ACTIVE_WINDOW,
    CONF_STABLE_SECONDS,
    DEFAULT_ACTIVE_WINDOW,
    DEFAULT_STABLE_SECONDS,
    CONF_ALERT_KINDS,
    CONF_ALERT_MAX_BIDDERS,
    CONF_NOTIFY_ENTITIES,
    CONF_NOTIFY_SERVICES,
    DEFAULT_ALERT_KINDS,
    CONF_ALERT_MAX_PRICE,
    CONF_COOKIE,
    CONF_DEFAULT_BID_LIMIT,
    CONF_EXTRA_IDS,
    CONF_FAST_INTERVAL,
    CONF_IDLE_INTERVAL,
    CONF_POINT_COST,
    CONF_SITE,
    DEFAULT_ALERT_MAX_BIDDERS,
    DEFAULT_ALERT_MAX_PRICE,
    DEFAULT_BID_LIMIT,
    DEFAULT_FAST_INTERVAL,
    DEFAULT_IDLE_INTERVAL,
    DEFAULT_POINT_COST,
    DOMAIN,
    SITE_GOVAUCTIONS,
    SITES,
)
from .cookies import cookie_header, parse_cookie_input
from .sites import govauctions as ga
from .sites import ebay as eb
from .sites import macbid as mb
from .sites.base import AuthError, SiteError

CONF_COOKIE_FILE = "cookie_file"
COOKIE_SELECTOR = TextSelector(TextSelectorConfig(multiline=True))
COOKIE_FILE_SELECTOR = FileSelector(FileSelectorConfig(accept=".txt,text/plain"))


def _cookie_fields() -> dict:
    """Upload a cookies.txt file, or paste cookies. Either one is enough."""
    return {
        vol.Optional(CONF_COOKIE_FILE): COOKIE_FILE_SELECTOR,
        vol.Optional(CONF_COOKIE): COOKIE_SELECTOR,
    }


async def _cookie_text(hass: HomeAssistant, user_input: dict[str, Any]) -> str:
    """The uploaded file's contents if one was uploaded, else the pasted text."""
    file_id = user_input.get(CONF_COOKIE_FILE)
    if file_id:
        def _read() -> str:
            with process_uploaded_file(hass, file_id) as path:
                return path.read_text(encoding="utf-8-sig", errors="replace")
        return await hass.async_add_executor_job(_read)
    return user_input.get(CONF_COOKIE, "")


async def _validate(cookie_text: str) -> tuple[str, dict[str, str]]:
    """Return (error key or "", cookies)."""
    if not cookie_text.strip():
        return "no_input", {}
    cookies = parse_cookie_input(cookie_text, domain=ga.COOKIE_DOMAIN)
    if not cookies:
        return "no_cookies", {}
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as session:
        client = ga.GovAuctionsClient(session, cookies)
        try:
            await client.live([])
        except AuthError:
            return "invalid_auth", {}
        except SiteError:
            return "cannot_connect", {}
        return "", client.cookies


CONF_AUTH_TEXT = "saved_login"


def _macbid_fields() -> dict:
    """Email + password (recommended), or the browser's saved login + device id."""
    return {
        vol.Optional(CONF_EMAIL): TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")),
        vol.Optional(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")),
        vol.Optional(CONF_AUTH_TEXT): TextSelector(TextSelectorConfig(multiline=True)),
        vol.Optional(CONF_DEVICE_ID): TextSelector(),
    }


async def _validate_macbid(user_input: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return (error key or "", entry data for Mac.bid)."""
    email = (user_input.get(CONF_EMAIL) or "").strip()
    password = user_input.get(CONF_PASSWORD) or ""
    saved = (user_input.get(CONF_AUTH_TEXT) or "").strip()
    device = (user_input.get(CONF_DEVICE_ID) or "").strip().strip('"')
    if not (email and password) and not saved:
        return "macbid_no_input", {}
    auth: dict[str, Any] | None = None
    if not (email and password):
        if not device:
            return "macbid_need_device", {}
        try:
            auth = mb.parse_auth(saved)
        except (SiteError, ValueError):
            return "macbid_bad_saved_login", {}
    device = device or mb.new_device_id()
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as session:
        client = mb.MacBidClient(session, auth, device, email=email or None, password=password or None)
        try:
            if email and password:
                await client.login()
            await client.watchlist()
        except mb.CodeRequired as need:
            # New device for Mac.bid: it sent a code. The flow asks for it next.
            return "code", {"email": email, "password": password, "device": device,
                            "where": need.where, "last4": need.last4}
        except AuthError:
            return "invalid_auth", {}
        except SiteError:
            return "cannot_connect", {}
    data: dict[str, Any] = {CONF_SITE: SITE_MACBID, CONF_AUTH: client.auth, CONF_DEVICE_ID: device}
    if email and password:
        data.update({CONF_EMAIL: email, CONF_PASSWORD: password})
    return "", data


async def _finish_macbid_code(pending: dict[str, Any], code: str) -> tuple[str, dict[str, Any]]:
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as session:
        client = mb.MacBidClient(session, None, pending["device"], email=pending["email"], password=pending["password"])
        try:
            await client.submit_code(code)
            await client.watchlist()
        except AuthError:
            return "macbid_bad_code", {}
        except SiteError:
            return "cannot_connect", {}
    return "", {CONF_SITE: SITE_MACBID, CONF_AUTH: client.auth, CONF_DEVICE_ID: pending["device"],
                CONF_EMAIL: pending["email"], CONF_PASSWORD: pending["password"]}


def _ebay_fields(d: Mapping[str, Any] | None = None) -> dict:
    d = d or {}
    return {
        vol.Required(CONF_APP_ID, default=d.get(CONF_APP_ID, vol.UNDEFINED)): TextSelector(),
        vol.Required(CONF_CERT_ID, default=d.get(CONF_CERT_ID, vol.UNDEFINED)): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        vol.Optional(CONF_RUNAME, default=d.get(CONF_RUNAME, vol.UNDEFINED)): TextSelector(),
        vol.Optional(CONF_EXTRA_IDS): TextSelector(TextSelectorConfig(multiline=True)),
    }


async def _check_ebay_keys(app_id: str, cert_id: str) -> str:
    async with aiohttp.ClientSession() as session:
        try:
            await eb.EbayClient(session, app_id, cert_id).app_token()
        except AuthError:
            return "ebay_bad_keys"
        except SiteError:
            return "cannot_connect"
    return ""


class AuctionWatcherConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1
    _pending_code: dict[str, Any] | None = None
    _ebay: dict[str, Any] | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 1: which site."""
        if user_input is not None:
            site = user_input[CONF_SITE]
            await self.async_set_unique_id(site)
            self._abort_if_unique_id_configured()
            if site == SITE_EBAY:
                return await self.async_step_ebay()
            return await (self.async_step_macbid() if site == SITE_MACBID else self.async_step_govauctions())
        schema = vol.Schema({
            vol.Required(CONF_SITE, default=SITE_GOVAUCTIONS): SelectSelector(
                SelectSelectorConfig(options=[{"value": k, "label": v} for k, v in SITES.items()])
            ),
        })
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_govauctions(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            error, cookies = await _validate(await _cookie_text(self.hass, user_input))
            if not error:
                return self.async_create_entry(
                    title=SITES[SITE_GOVAUCTIONS],
                    data={CONF_SITE: SITE_GOVAUCTIONS, CONF_COOKIE: cookie_header(cookies)},
                )
            errors["base"] = error
        return self.async_show_form(step_id="govauctions", data_schema=vol.Schema(_cookie_fields()), errors=errors)

    async def async_step_macbid(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            error, data = await _validate_macbid(user_input)
            if error == "code":
                self._pending_code = data
                return await self.async_step_macbid_code()
            if not error:
                return self.async_create_entry(title=SITES[SITE_MACBID], data=data)
            errors["base"] = error
        return self.async_show_form(step_id="macbid", data_schema=vol.Schema(_macbid_fields()), errors=errors)

    async def async_step_ebay(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """eBay developer keys; with a RuName, the next page signs in to your eBay account."""
        errors: dict[str, str] = {}
        if user_input is not None:
            app_id, cert_id = user_input[CONF_APP_ID].strip(), user_input[CONF_CERT_ID].strip()
            error = await _check_ebay_keys(app_id, cert_id)
            if not error:
                self._ebay = {CONF_SITE: SITE_EBAY, CONF_APP_ID: app_id, CONF_CERT_ID: cert_id,
                              CONF_RUNAME: (user_input.get(CONF_RUNAME) or "").strip(),
                              "extra": (user_input.get(CONF_EXTRA_IDS) or "").strip()}
                if self._ebay[CONF_RUNAME]:
                    return await self.async_step_ebay_signin()
                return self._finish_ebay()
            errors["base"] = error
        return self.async_show_form(step_id="ebay", data_schema=vol.Schema(_ebay_fields(user_input)), errors=errors)

    async def async_step_ebay_signin(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        d = self._ebay or {}
        if user_input is not None:
            async with aiohttp.ClientSession() as session:
                client = eb.EbayClient(session, d[CONF_APP_ID], d[CONF_CERT_ID], runame=d[CONF_RUNAME])
                try:
                    await client.exchange_code(eb.code_from_pasted(user_input["signed_in_address"]))
                    d[CONF_EBAY_USER] = await client.my_user_id()
                    d[CONF_EBAY_TOKENS] = client.user_auth
                except (AuthError, SiteError):
                    errors["base"] = "ebay_bad_signin"
            if not errors:
                return self._finish_ebay()
        return self.async_show_form(
            step_id="ebay_signin",
            data_schema=vol.Schema({vol.Required("signed_in_address"): TextSelector(TextSelectorConfig(multiline=True))}),
            errors=errors,
            description_placeholders={"link": eb.consent_url(d.get(CONF_APP_ID, ""), d.get(CONF_RUNAME, ""))},
        )

    def _finish_ebay(self) -> ConfigFlowResult:
        d = dict(self._ebay or {})
        extra = d.pop("extra", "")
        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data=d)
        return self.async_create_entry(title=SITES[SITE_EBAY], data=d, options={CONF_EXTRA_IDS: extra} if extra else {})

    async def async_step_macbid_code(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Mac.bid texted/emailed a code because Home Assistant is a new device for it."""
        errors: dict[str, str] = {}
        pending = self._pending_code or {}
        if user_input is not None:
            error, data = await _finish_macbid_code(pending, user_input["code"])
            if not error:
                if self.source == SOURCE_REAUTH:
                    return self.async_update_reload_and_abort(self._get_reauth_entry(), data=data)
                return self.async_create_entry(title=SITES[SITE_MACBID], data=data)
            errors["base"] = error
        where = f"your {pending.get('where', 'phone')}" + (f" ending {pending['last4']}" if pending.get("last4") else "")
        return self.async_show_form(
            step_id="macbid_code",
            data_schema=vol.Schema({vol.Required("code"): TextSelector()}),
            errors=errors,
            description_placeholders={"where": where},
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if entry.data.get(CONF_SITE) == SITE_EBAY:
            self._ebay = dict(entry.data)
            if self._ebay.get(CONF_RUNAME):
                return await self.async_step_ebay_signin()
            return await self.async_step_ebay()
        is_macbid = entry.data.get(CONF_SITE) == SITE_MACBID
        if user_input is not None:
            if is_macbid:
                error, data = await _validate_macbid(user_input)
                if error == "code":
                    self._pending_code = data
                    return await self.async_step_macbid_code()
                if not error:
                    return self.async_update_reload_and_abort(entry, data=data)
            else:
                error, cookies = await _validate(await _cookie_text(self.hass, user_input))
                if not error:
                    return self.async_update_reload_and_abort(
                        entry, data={**entry.data, CONF_COOKIE: cookie_header(cookies)}
                    )
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(_macbid_fields() if is_macbid else _cookie_fields()),
            errors=errors,
            description_placeholders={"site": entry.title},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return AuctionWatcherOptionsFlow()


def _num(min_: float, max_: float, step: float, unit: str | None = None) -> NumberSelector:
    # Leave unit_of_measurement out entirely when there's none: Home Assistant
    # rejects a None unit, which shows up as "Config flow could not be loaded: 400".
    config = NumberSelectorConfig(min=min_, max=max_, step=step, mode=NumberSelectorMode.BOX)
    if unit:
        config["unit_of_measurement"] = unit
    return NumberSelector(config)


ALERT_KIND_OPTIONS = [
    {"value": "opportunity", "label": "Few bidders left"},
    {"value": "bid_limit", "label": "Bid limit reached"},
    {"value": "ended", "label": "Auction ended"},
    {"value": "won", "label": "Auction won"},
    {"value": "booking", "label": "Bids booked or cancelled (including automatic)"},
    {"value": "outbid", "label": "Outbid (Mac.bid)"},
    {"value": "ending_soon", "label": "Ending soon (Mac.bid)"},
    {"value": "over_max", "label": "Price passed your max bid (Mac.bid)"},
    {"value": "pickup", "label": "Pickup deadline reminder (Mac.bid)"},
    {"value": "pack", "label": "Good point pack open to join (GovAuctions)"},
    {"value": "budget", "label": "Weekly budget used up (GovAuctions)"},
]


GOV_KINDS = {"opportunity", "bid_limit", "ended", "won", "booking", "pack", "budget"}
MACBID_KINDS = {"outbid", "ending_soon", "over_max", "won", "ended", "pickup", "booking"}
EBAY_KINDS = {"outbid", "ending_soon", "over_max", "won", "ended"}


class AuctionWatcherOptionsFlow(OptionsFlow):
    """One page of settings, showing only what applies to this account's site."""

    def _alert_fields(self, kinds: set[str]) -> dict:
        o = self.config_entry.options
        # Legacy notify services: phones (mobile_app_*), Telegram, Alexa Media Player, email, ...
        available = sorted(s for s in self.hass.services.async_services_for_domain("notify") if s != "send_message")
        service_options = [{"value": s, "label": _service_label(s)} for s in available]
        saved_services = [s for s in o.get(CONF_NOTIFY_SERVICES, []) if s in available]
        kind_options = [k for k in ALERT_KIND_OPTIONS if k["value"] in kinds]
        saved_kinds = [k for k in o.get(CONF_ALERT_KINDS, DEFAULT_ALERT_KINDS) if k in kinds]
        return {
            vol.Optional(CONF_NOTIFY_SERVICES, default=saved_services): SelectSelector(
                SelectSelectorConfig(options=service_options, multiple=True, mode=SelectSelectorMode.DROPDOWN)
            ),
            vol.Optional(CONF_NOTIFY_ENTITIES, default=o.get(CONF_NOTIFY_ENTITIES, [])): EntitySelector(
                EntitySelectorConfig(domain="notify", multiple=True)
            ),
            vol.Optional(CONF_ALERT_KINDS, default=saved_kinds): SelectSelector(
                SelectSelectorConfig(options=kind_options, multiple=True)
            ),
        }

    def _save(self, user_input: dict[str, Any]) -> ConfigFlowResult:
        # Keep settings that aren't on this page (e.g. from an older version)
        return self.async_create_entry(data={**self.config_entry.options, **user_input})

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self.config_entry.data.get(CONF_SITE) == SITE_MACBID:
            return await self.async_step_macbid_options(user_input)
        if self.config_entry.data.get(CONF_SITE) == SITE_EBAY:
            return await self.async_step_ebay_options(user_input)
        if user_input is not None:
            return self._save(user_input)
        o = self.config_entry.options
        schema = vol.Schema({
            # When to alert
            vol.Required(CONF_ALERT_MAX_BIDDERS, default=o.get(CONF_ALERT_MAX_BIDDERS, DEFAULT_ALERT_MAX_BIDDERS)): _num(1, 9, 1),
            vol.Required(CONF_PACKS_ENABLED, default=o.get(CONF_PACKS_ENABLED, True)): BooleanSelector(),
            vol.Required(CONF_ACTIVE_WINDOW, default=o.get(CONF_ACTIVE_WINDOW, DEFAULT_ACTIVE_WINDOW)): _num(1, 30, 1, "min"),
            vol.Required(CONF_STABLE_SECONDS, default=o.get(CONF_STABLE_SECONDS, DEFAULT_STABLE_SECONDS)): _num(0, 600, 1, "s"),
            vol.Required(CONF_ALERT_MAX_PRICE, default=o.get(CONF_ALERT_MAX_PRICE, DEFAULT_ALERT_MAX_PRICE)): _num(0, 100000, 0.01, "$"),
            vol.Required(CONF_DEFAULT_BID_LIMIT, default=o.get(CONF_DEFAULT_BID_LIMIT, DEFAULT_BID_LIMIT)): _num(0, 100000, 1, "bids"),
            vol.Required(CONF_WEEKLY_BUDGET, default=o.get(CONF_WEEKLY_BUDGET, 0)): _num(0, 1000000, 1, "points"),
            # Where alerts go
            **self._alert_fields(GOV_KINDS),
            # Costs, timing, page
            vol.Required(CONF_POINT_COST, default=o.get(CONF_POINT_COST, DEFAULT_POINT_COST)): _num(0, 5, 0.01, "$"),
            vol.Required(CONF_FAST_INTERVAL, default=o.get(CONF_FAST_INTERVAL, DEFAULT_FAST_INTERVAL)): _num(5, 120, 1, "s"),
            vol.Required(CONF_IDLE_INTERVAL, default=o.get(CONF_IDLE_INTERVAL, DEFAULT_IDLE_INTERVAL)): _num(5, 240, 1, "min"),
            vol.Required(CONF_KEEP_ENDED_DAYS, default=o.get(CONF_KEEP_ENDED_DAYS, DEFAULT_KEEP_ENDED_DAYS)): _num(0, 60, 1, "days"),
            vol.Optional(CONF_EXTRA_IDS, default=o.get(CONF_EXTRA_IDS, "")): TextSelector(),
        })
        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_ebay_options(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(user_input)
        o = self.config_entry.options
        schema = vol.Schema({
            vol.Required(CONF_ENDING_MINUTES, default=o.get(CONF_ENDING_MINUTES, DEFAULT_ENDING_MINUTES)): _num(0, 240, 1, "min"),
            vol.Required(CONF_DEFAULT_BID_LIMIT, default=o.get(CONF_DEFAULT_BID_LIMIT, DEFAULT_BID_LIMIT)): _num(0, 100000, 1, "$"),
            **self._alert_fields(EBAY_KINDS),
            vol.Required(CONF_FAST_INTERVAL, default=o.get(CONF_FAST_INTERVAL, DEFAULT_FAST_INTERVAL)): _num(5, 120, 1, "s"),
            vol.Required(CONF_IDLE_INTERVAL, default=o.get(CONF_IDLE_INTERVAL, DEFAULT_IDLE_INTERVAL)): _num(5, 240, 1, "min"),
            vol.Required(CONF_KEEP_ENDED_DAYS, default=o.get(CONF_KEEP_ENDED_DAYS, DEFAULT_KEEP_ENDED_DAYS)): _num(0, 60, 1, "days"),
            vol.Optional(CONF_EXTRA_IDS, default=o.get(CONF_EXTRA_IDS, "")): TextSelector(TextSelectorConfig(multiline=True)),
        })
        return self.async_show_form(step_id="ebay_options", data_schema=schema)

    async def async_step_macbid_options(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(user_input)
        o = self.config_entry.options
        schema = vol.Schema({
            # When to alert
            vol.Required(CONF_ENDING_MINUTES, default=o.get(CONF_ENDING_MINUTES, DEFAULT_ENDING_MINUTES)): _num(0, 240, 1, "min"),
            vol.Required(CONF_PICKUP_HOURS, default=o.get(CONF_PICKUP_HOURS, DEFAULT_PICKUP_HOURS)): _num(1, 168, 1, "h"),
            vol.Required(CONF_DEFAULT_BID_LIMIT, default=o.get(CONF_DEFAULT_BID_LIMIT, DEFAULT_BID_LIMIT)): _num(0, 100000, 1, "$"),
            vol.Required(CONF_MACBID_BUDGET, default=o.get(CONF_MACBID_BUDGET, 0)): _num(0, 1000000, 1, "$"),
            # Where alerts go
            **self._alert_fields(MACBID_KINDS),
            # Timing, page
            vol.Required(CONF_FAST_INTERVAL, default=o.get(CONF_FAST_INTERVAL, DEFAULT_FAST_INTERVAL)): _num(5, 120, 1, "s"),
            vol.Required(CONF_IDLE_INTERVAL, default=o.get(CONF_IDLE_INTERVAL, DEFAULT_IDLE_INTERVAL)): _num(5, 240, 1, "min"),
            vol.Required(CONF_KEEP_ENDED_DAYS, default=o.get(CONF_KEEP_ENDED_DAYS, DEFAULT_KEEP_ENDED_DAYS)): _num(0, 60, 1, "days"),
            vol.Optional(CONF_EXTRA_IDS, default=o.get(CONF_EXTRA_IDS, "")): TextSelector(),
        })
        return self.async_show_form(step_id="macbid_options", data_schema=schema)


def _service_label(service: str) -> str:
    if service.startswith("mobile_app_"):
        return f"Phone push: {service.removeprefix('mobile_app_').replace('_', ' ')}"
    if service.startswith("alexa_media"):
        return f"Alexa announcement: {service.removeprefix('alexa_media').strip('_').replace('_', ' ') or 'all'}"
    if "telegram" in service:
        return f"Telegram: {service}"
    if service == "persistent_notification":
        return "Home Assistant notification (bell icon)"
    return service
