"""Settings that apply to every site: where alerts go and which alerts to send.

Kept in .storage/auction_watcher.global and edited on the Auctions page (Settings tab).
Until you save that page once, each account's old per-site alert targets are merged in,
so upgrading never silences alerts.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .alerts import build_calls
from .const import (
    CONF_ALERT_KINDS,
    CONF_NOTIFY_ENTITIES,
    CONF_NOTIFY_SERVICES,
    DEFAULT_ALERT_KINDS,
    DOMAIN,
    STORAGE_VERSION,
)
from .logic import SpendCap, period_start

_LOGGER = logging.getLogger(__name__)

DATA_KEY = f"{DOMAIN}_global"

# What each alert is, and which sites can send it (shown on the Settings tab)
ALERT_KINDS = [
    {"value": "opportunity", "label": "Few bidders left", "sites": ["govauctions"]},
    {"value": "bid_limit", "label": "Bid limit reached", "sites": ["govauctions"]},
    {"value": "booking", "label": "Bids booked, cancelled or placed (including automatic)", "sites": ["govauctions", "macbid"]},
    {"value": "budget", "label": "Spending cap or weekly budget reached", "sites": ["govauctions", "macbid"]},
    {"value": "pack", "label": "Good point pack open to join", "sites": ["govauctions"]},
    {"value": "outbid", "label": "Outbid", "sites": ["macbid", "ebay"]},
    {"value": "ending_soon", "label": "Ending soon", "sites": ["macbid", "ebay"]},
    {"value": "over_max", "label": "Price passed your max bid", "sites": ["macbid", "ebay"]},
    {"value": "pickup", "label": "Pickup deadline reminder", "sites": ["macbid"]},
    {"value": "won", "label": "Auction won", "sites": ["govauctions", "macbid", "ebay"]},
    {"value": "ended", "label": "Auction ended", "sites": ["govauctions", "macbid", "ebay"]},
    {"value": "search", "label": "New matches for saved searches", "sites": ["govauctions", "macbid", "ebay"]},
    {"value": "proxy", "label": "Bidding for others: their replies and agreements", "sites": ["macbid"]},
]


class GlobalSettings:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.global")
        self.notify_services: list[str] = []
        self.notify_entities: list[str] = []
        self.alert_kinds: list[str] = list(DEFAULT_ALERT_KINDS)
        self.saved = False          # True once you've saved the Settings tab
        self._merged: set[str] = set()
        self.cap = SpendCap()
        self._cap_alerted: float | None = None  # start of the period we last alerted for

    async def async_load(self) -> None:
        d = await self._store.async_load() or {}
        self.notify_services = list(d.get("notify_services", []))
        self.notify_entities = list(d.get("notify_entities", []))
        self.alert_kinds = list(d.get("alert_kinds", DEFAULT_ALERT_KINDS))
        self.saved = bool(d.get("saved"))
        self._merged = set(d.get("merged", []))
        c = d.get("cap") or {}
        self.cap = SpendCap(float(c.get("amount", 0) or 0), c.get("period", "week") if c.get("period") in ("week", "month") else "week",
                            c.get("mode", "block") if c.get("mode") in ("block", "warn") else "block")
        self._cap_alerted = d.get("cap_alerted")
        known = d.get("known_kinds")
        if known is not None:
            self.alert_kinds += [k["value"] for k in ALERT_KINDS if k["value"] not in known and k["value"] not in self.alert_kinds]

    async def _save(self) -> None:
        await self._store.async_save({
            "notify_services": self.notify_services, "notify_entities": self.notify_entities,
            "alert_kinds": self.alert_kinds, "saved": self.saved, "merged": sorted(self._merged),
            "cap": vars(self.cap), "cap_alerted": self._cap_alerted,
            "known_kinds": [k["value"] for k in ALERT_KINDS],
        })

    # ---------------- one spending cap across every site
    def _coordinators(self) -> list[Any]:
        return [e.runtime_data for e in self.hass.config_entries.async_entries(DOMAIN)
                if getattr(e, "runtime_data", None) is not None and hasattr(e.runtime_data, "dollars_used")]

    def period_start(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        return period_start(dt_util.as_local(dt_util.utc_from_timestamp(now)), self.cap.period)

    def used(self, now: float | None = None, exclude: tuple[Any, str] | None = None) -> dict[str, Any]:
        """Dollars counted toward the cap this period, per site. exclude = (coordinator, item id):
        leave that item's own max bid out (when checking a new max for it)."""
        since = self.period_start(now)
        by_site: dict[str, float] = {}
        for c in self._coordinators():
            skip = exclude[1] if exclude and exclude[0] is c else None
            site = c.config_entry.data.get("site", "govauctions")
            by_site[site] = round(by_site.get(site, 0.0) + c.dollars_used(since, skip), 2)
        return {"total": round(sum(by_site.values()), 2), "by_site": by_site, "since": since}

    def left(self, now: float | None = None, exclude: tuple[Any, str] | None = None) -> float | None:
        """Dollars still allowed under the cap; None = don't block anything (no cap, or warn only)."""
        if not self.cap.amount or self.cap.mode != "block":
            return None
        return self.cap.left(self.used(now, exclude)["total"])

    def summary(self, now: float | None = None) -> dict[str, Any] | None:
        if not self.cap.amount:
            return None
        u = self.used(now)
        return {**vars(self.cap), "used": u["total"], "by_site": u["by_site"],
                "left": round(self.cap.amount - u["total"], 2), "since": u["since"]}

    async def async_check(self, now: float | None = None) -> None:
        """Alert once per period when the cap is reached."""
        if not self.cap.amount:
            return
        u = self.used(now)
        if self.cap.reached(u["total"]) and self._cap_alerted != u["since"]:
            self._cap_alerted = u["since"]
            await self._save()
            self.send("spend_cap", {"auction_id": "spend_cap", "used": u["total"], **vars(self.cap)}, "budget")

    async def async_adopt_entry(self, entry: ConfigEntry) -> None:
        """Upgrade path: bring an account's old per-site alert targets in (once per account)."""
        if self.saved or entry.entry_id in self._merged:
            return
        o = entry.options
        if not self._merged:
            self.alert_kinds = []  # start from what the accounts had, not the defaults
        for key, target in ((CONF_NOTIFY_SERVICES, self.notify_services), (CONF_NOTIFY_ENTITIES, self.notify_entities)):
            for x in o.get(key, []):
                if x not in target:
                    target.append(x)
        for k in list(o.get(CONF_ALERT_KINDS, DEFAULT_ALERT_KINDS)) + ["search"]:
            if k not in self.alert_kinds:
                self.alert_kinds.append(k)
        self._merged.add(entry.entry_id)
        await self._save()

    def calls(self, kind: str, data: dict[str, Any], category: str | None = None) -> list[tuple[str, str, dict[str, Any]]]:
        """The notify calls for one alert, or none if that alert type is switched off.
        category = the alert type it's filed under on the Settings tab (e.g. "booked" -> "booking")."""
        if (category or kind) not in self.alert_kinds:
            return []
        return build_calls(kind, data, self.notify_services, self.notify_entities)

    @callback
    def send(self, kind: str, data: dict[str, Any], category: str | None = None) -> None:
        for domain, service, service_data in self.calls(kind, data, category):
            self.hass.async_create_task(self._send(domain, service, service_data))

    async def _send(self, domain: str, service: str, data: dict[str, Any]) -> None:
        """A broken target (phone removed, Alexa offline) never stops anything else."""
        try:
            await self.hass.services.async_call(domain, service, data, blocking=True)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Couldn't send auction alert to %s: %s", data.get("entity_id") or f"{domain}.{service}", err)

    def view(self) -> dict[str, Any]:
        available = sorted(s for s in self.hass.services.async_services_for_domain("notify") if s != "send_message")
        entities = sorted(s.entity_id for s in self.hass.states.async_all("notify"))
        return {
            "notify_services": self.notify_services, "notify_entities": self.notify_entities,
            "alert_kinds": self.alert_kinds, "saved": self.saved,
            "kind_options": ALERT_KINDS, "services_available": available, "entities_available": entities,
            "cap": vars(self.cap), "cap_summary": self.summary(),
        }


def get(hass: HomeAssistant) -> GlobalSettings | None:
    return hass.data.get(DATA_KEY)


@websocket_api.websocket_command({vol.Required("type"): "auction_watcher/settings"})
@websocket_api.require_admin
@callback
def ws_get(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    g = get(hass)
    if g is None:
        connection.send_error(msg["id"], "not_loaded", "Auction Watcher isn't set up yet")
        return
    connection.send_result(msg["id"], g.view())


@websocket_api.websocket_command({
    vol.Required("type"): "auction_watcher/settings/set",
    vol.Optional("notify_services"): [str],
    vol.Optional("notify_entities"): [str],
    vol.Optional("alert_kinds"): [vol.In([k["value"] for k in ALERT_KINDS])],
    vol.Optional("test", default=False): bool,
    vol.Optional("cap_amount"): vol.All(vol.Coerce(float), vol.Range(min=0, max=1000000)),
    vol.Optional("cap_period"): vol.In(["week", "month"]),
    vol.Optional("cap_mode"): vol.In(["block", "warn"]),
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_set(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    g = get(hass)
    if g is None:
        connection.send_error(msg["id"], "not_loaded", "Auction Watcher isn't set up yet")
        return
    if msg["test"]:
        calls = build_calls("test", {"auction_id": "test"}, g.notify_services, g.notify_entities)
        if not calls:
            connection.send_error(msg["id"], "no_targets", "Pick at least one phone, chat or speaker first")
            return
        for domain, service, data in calls:
            await g._send(domain, service, data)
        connection.send_result(msg["id"], g.view())
        return
    for key in ("notify_services", "notify_entities", "alert_kinds"):
        if key in msg:
            setattr(g, key, list(msg[key]))
    if "cap_amount" in msg:
        g.cap.amount = float(msg["cap_amount"])
    if "cap_period" in msg:
        g.cap.period = msg["cap_period"]
    if "cap_mode" in msg:
        g.cap.mode = msg["cap_mode"]
    g.saved = True
    await g._save()
    connection.send_result(msg["id"], g.view())


@callback
def async_register_ws(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_get)
    websocket_api.async_register_command(hass, ws_set)
