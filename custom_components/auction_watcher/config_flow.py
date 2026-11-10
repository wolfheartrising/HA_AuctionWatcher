"""Set up an auction site account by pasting its cookies."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    FileSelector,
    FileSelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
)

from .const import (
    CONF_ALERT_MAX_BIDDERS,
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


class AuctionWatcherConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            site = user_input[CONF_SITE]
            await self.async_set_unique_id(site)
            self._abort_if_unique_id_configured()
            error, cookies = await _validate(await _cookie_text(self.hass, user_input))
            if not error:
                return self.async_create_entry(
                    title=SITES[site],
                    data={CONF_SITE: site, CONF_COOKIE: cookie_header(cookies)},
                )
            errors["base"] = error
        schema = vol.Schema({
            vol.Required(CONF_SITE, default=SITE_GOVAUCTIONS): SelectSelector(
                SelectSelectorConfig(options=[{"value": k, "label": v} for k, v in SITES.items()])
            ),
            **_cookie_fields(),
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            error, cookies = await _validate(await _cookie_text(self.hass, user_input))
            if not error:
                return self.async_update_reload_and_abort(
                    entry, data={**entry.data, CONF_COOKIE: cookie_header(cookies)}
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(_cookie_fields()),
            errors=errors,
            description_placeholders={"site": entry.title},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return AuctionWatcherOptionsFlow()


def _num(min_: float, max_: float, step: float, unit: str | None = None) -> NumberSelector:
    return NumberSelector(NumberSelectorConfig(min=min_, max=max_, step=step, mode=NumberSelectorMode.BOX, unit_of_measurement=unit))


class AuctionWatcherOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        o = self.config_entry.options
        schema = vol.Schema({
            vol.Required(CONF_ALERT_MAX_BIDDERS, default=o.get(CONF_ALERT_MAX_BIDDERS, DEFAULT_ALERT_MAX_BIDDERS)): _num(1, 9, 1),
            vol.Required(CONF_ALERT_MAX_PRICE, default=o.get(CONF_ALERT_MAX_PRICE, DEFAULT_ALERT_MAX_PRICE)): _num(0, 100000, 0.01, "$"),
            vol.Required(CONF_DEFAULT_BID_LIMIT, default=o.get(CONF_DEFAULT_BID_LIMIT, DEFAULT_BID_LIMIT)): _num(0, 100000, 1, "bids"),
            vol.Required(CONF_POINT_COST, default=o.get(CONF_POINT_COST, DEFAULT_POINT_COST)): _num(0, 5, 0.01, "$"),
            vol.Required(CONF_FAST_INTERVAL, default=o.get(CONF_FAST_INTERVAL, DEFAULT_FAST_INTERVAL)): _num(5, 120, 1, "s"),
            vol.Required(CONF_IDLE_INTERVAL, default=o.get(CONF_IDLE_INTERVAL, DEFAULT_IDLE_INTERVAL)): _num(5, 240, 1, "min"),
            vol.Optional(CONF_EXTRA_IDS, default=o.get(CONF_EXTRA_IDS, "")): TextSelector(),
        })
        return self.async_show_form(step_id="init", data_schema=schema)
