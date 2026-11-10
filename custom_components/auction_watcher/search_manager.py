"""Saved searches in Home Assistant: daily run, alerts, Mac.bid keyword sync, page API.

The searching itself is each site's own search (what its search box does), once a day at
your chosen time (default 8:00) or when you tap "Search now". New matches are alerted once.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .alerts import build_calls
from .const import (
    CONF_ALERT_KINDS,
    CONF_NOTIFY_ENTITIES,
    CONF_NOTIFY_SERVICES,
    CONF_SITE,
    DEFAULT_ALERT_KINDS,
    DOMAIN,
    EVENT_SEARCH,
    SITE_EBAY,
    SITE_GOVAUCTIONS,
    SITE_MACBID,
    STORAGE_VERSION,
)
from .searches import SITE_ORDER, Hit, SavedSearch, add_search, filter_hits, hit_dict, new_matches, to_dict
from .sites import macbid as mb
from .sites.base import SiteError

_LOGGER = logging.getLogger(__name__)

DATA_KEY = f"{DOMAIN}_searches"
SITE_LABELS = {SITE_GOVAUCTIONS: "GovAuctions", SITE_MACBID: "Mac.bid", SITE_EBAY: "eBay"}
DEFAULT_RUN_TIME = "08:00"
IMPORT_EVERY = 3600          # re-read your Mac.bid keywords at most hourly when the page opens
BUILDINGS_EVERY = 86400


def _parse_time(value: str) -> tuple[int, int]:
    h, _, m = (value or DEFAULT_RUN_TIME).partition(":")
    hour, minute = int(h), int(m or 0)
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(value)
    return hour, minute


class SearchManager:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.searches")
        self.searches: list[SavedSearch] = []
        self.seen: dict[str, list[str]] = {}
        self.results: dict[str, list[dict[str, Any]]] = {}
        self.excluded: dict[str, int] = {}
        self.errors: dict[str, str] = {}
        self.last_run: float | None = None
        self.run_time = DEFAULT_RUN_TIME
        self.mb_choice: dict[str, bool] | None = None   # building name -> on; None = use my Mac.bid account's list
        self.mb_all: list[dict[str, Any]] = []
        self.mb_account: list[str] = []
        self._buildings_at = 0.0
        self._imported_at = 0.0
        self._unsub = None
        self._rerun = None
        self._lock = asyncio.Lock()
        self.running = False

    # ---------------- storage / schedule
    async def async_load(self) -> None:
        d = await self._store.async_load() or {}
        self.searches = [SavedSearch.from_dict(x) for x in d.get("searches", [])]
        self.seen = d.get("seen", {})
        self.results = d.get("results", {})
        self.excluded = d.get("excluded", {})
        self.errors = d.get("errors", {})
        self.last_run = d.get("last_run")
        self.run_time = d.get("run_time", DEFAULT_RUN_TIME)
        self.mb_choice = d.get("mb_choice")
        self._schedule()

    async def _save(self) -> None:
        await self._store.async_save({
            "searches": [to_dict(s) for s in self.searches], "seen": self.seen, "results": self.results,
            "excluded": self.excluded, "errors": self.errors, "last_run": self.last_run,
            "run_time": self.run_time, "mb_choice": self.mb_choice,
        })

    def _schedule(self) -> None:
        if self._unsub:
            self._unsub()
        hour, minute = _parse_time(self.run_time)

        async def _daily(_now: datetime) -> None:
            await self.async_run()

        self._unsub = async_track_time_change(self.hass, _daily, hour=hour, minute=minute, second=0)

    @callback
    def stop(self) -> None:
        for unsub in (self._unsub, self._rerun):
            if unsub:
                unsub()
        self._unsub = self._rerun = None

    def next_run(self) -> str:
        hour, minute = _parse_time(self.run_time)
        now = dt_util.now()
        nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        return nxt.isoformat()

    # ---------------- accounts
    def _coordinators(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for e in self.hass.config_entries.async_entries(DOMAIN):
            if e.state is ConfigEntryState.LOADED and getattr(e, "runtime_data", None) is not None:
                out.setdefault(e.data.get(CONF_SITE, SITE_GOVAUCTIONS), e.runtime_data)
        return out

    def _macbid(self) -> mb.MacBidClient | None:
        c = self._coordinators().get(SITE_MACBID)
        return c.client if c else None

    async def _refresh_buildings(self, force: bool = False) -> None:
        client = self._macbid()
        if client is None or (not force and time.time() - self._buildings_at < BUILDINGS_EVERY and self.mb_all):
            return
        self._buildings_at = time.time() - BUILDINGS_EVERY + 600  # if this fails, try again in 10 minutes
        try:
            self.mb_all = await client.buildings()
            mine = set(await client.my_building_ids())
            self.mb_account = [b["name"] for b in self.mb_all if b["id"] in mine]
            self._buildings_at = time.time()
        except SiteError as err:
            _LOGGER.debug("Mac.bid locations not loaded: %s", err)

    def enabled_buildings(self) -> list[str]:
        if self.mb_choice is None:
            return list(self.mb_account)
        return [name for name, on in self.mb_choice.items() if on]

    async def _import(self, force: bool = False) -> None:
        """Your Mac.bid keyword alerts become saved searches here (marked green)."""
        client = self._macbid()
        if client is None or (not force and time.time() - self._imported_at < IMPORT_EVERY):
            return
        self._imported_at = time.time()
        try:
            keywords = await client.keywords()
        except SiteError as err:
            self.errors[SITE_MACBID] = f"Couldn't read your Mac.bid keywords: {err}"
            return
        on_site = {k.strip().lower() for k in keywords}
        for s in self.searches:  # removed on mac.bid itself: no longer marked as from there
            if SITE_MACBID in s.sources and s.keyword.strip().lower() not in on_site:
                s.sources.pop(SITE_MACBID)
        for k in keywords:
            add_search(self.searches, k, source_site=SITE_MACBID, source_id=k)
        await self._save()

    # ---------------- run
    async def async_run(self, only: str | None = None) -> None:
        if self._lock.locked():
            return
        async with self._lock:
            self.running = True
            try:
                await self._run(only)
            finally:
                self.running = False

    async def _run(self, only: str | None) -> None:
        coords = self._coordinators()
        await self._import(force=only is None)
        await self._refresh_buildings()
        buildings = self.enabled_buildings()
        raw: dict[str, list[Hit]] = {}
        errors: dict[str, str] = {}
        for s in self.searches:
            if only and s.id != only:
                continue
            hits: list[Hit] = []
            for site in s.sites:
                coord = coords.get(site)
                if coord is None or not hasattr(coord.client, "search"):
                    continue
                try:
                    if site == SITE_MACBID:
                        found, self.excluded[s.id] = await coord.client.search(s.keyword, buildings or None)
                    else:
                        found = await coord.client.search(s.keyword)
                    hits += filter_hits([Hit(site=site, **h) for h in found], s.max_price)
                except SiteError as err:
                    errors[site] = str(err)
                    _LOGGER.warning("Saved search %r on %s failed: %s", s.keyword, site, err)
                await asyncio.sleep(0.5)  # gentle on the sites
            raw[s.id] = hits
        first = {sid for sid in raw if sid not in self.seen}
        fresh = new_matches(raw, self.seen, first)
        fresh_keys = {sid: {h.key for h in hs} for sid, hs in fresh.items()}
        for sid, hits in raw.items():
            self.results[sid] = [hit_dict(h, [sid], h.key in fresh_keys.get(sid, set())) for h in hits]
        if only is None:
            self.errors = errors
            self.last_run = time.time()
        else:
            self.errors.update(errors)
        await self._save()
        if fresh:
            self._alert(fresh)

    def _alert(self, fresh: dict[str, list[Hit]]) -> None:
        by_id = {s.id: s for s in self.searches}
        count = sum(len(v) for v in fresh.values())
        lines = []
        for sid, hits in fresh.items():
            per_site: dict[str, int] = {}
            for h in hits:
                per_site[h.site] = per_site.get(h.site, 0) + 1
            where = ", ".join(f"{n} {SITE_LABELS.get(k, k)}" for k, n in per_site.items())
            lines.append(f"{by_id[sid].keyword if sid in by_id else sid}: {where}")
        data = {"count": count, "lines": lines, "auction_id": "searches",
                "items": [hit_dict(h, [sid], True) for sid, hs in fresh.items() for h in hs][:25]}
        self.hass.bus.async_fire(EVENT_SEARCH, data)
        services: list[str] = []
        entities: list[str] = []
        for e in self.hass.config_entries.async_entries(DOMAIN):
            if e.state is not ConfigEntryState.LOADED or "search" not in e.options.get(CONF_ALERT_KINDS, DEFAULT_ALERT_KINDS):
                continue
            services += [x for x in e.options.get(CONF_NOTIFY_SERVICES, []) if x not in services]
            entities += [x for x in e.options.get(CONF_NOTIFY_ENTITIES, []) if x not in entities]
        for domain, service, service_data in build_calls("search", data, services, entities):
            self.hass.async_create_task(self._send(domain, service, service_data))

    async def _send(self, domain: str, service: str, data: dict[str, Any]) -> None:
        try:
            await self.hass.services.async_call(domain, service, data, blocking=True)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Couldn't send saved-search alert to %s: %s", data.get("entity_id") or f"{domain}.{service}", err)

    # ---------------- changes from the page
    async def async_add(self, keyword: str, sites: list[str] | None, max_price: float, save_to_macbid: bool) -> SavedSearch:
        s = add_search(self.searches, keyword, sites=sites or None, max_price=max_price)
        if save_to_macbid:
            client = self._macbid()
            if client is None:
                raise SiteError("No Mac.bid account is set up")
            current = await client.keywords()
            await client.save_keywords(mb.keywords_with(current, s.keyword))
            s.sources[SITE_MACBID] = s.keyword
        await self._save()
        self.hass.async_create_task(self.async_run(only=s.id))
        return s

    async def async_remove(self, search_id: str) -> None:
        """Delete here, and on Mac.bid too when it came from there (same as its trash can)."""
        s = next((x for x in self.searches if x.id == search_id), None)
        if s is None:
            return
        if SITE_MACBID in s.sources:
            client = self._macbid()
            if client is not None:
                current = await client.keywords()
                remaining = mb.keywords_without(current, s.keyword)
                if remaining != current:
                    await client.save_keywords(remaining)
        self.searches.remove(s)
        for d in (self.seen, self.results, self.excluded):
            d.pop(search_id, None)
        await self._save()

    async def async_update(self, search_id: str, sites: list[str] | None, max_price: float | None) -> None:
        s = next((x for x in self.searches if x.id == search_id), None)
        if s is None:
            return
        if sites is not None:
            s.sites = [x for x in SITE_ORDER if x in sites]
        if max_price is not None:
            s.max_price = float(max_price)
        await self._save()
        self.hass.async_create_task(self.async_run(only=s.id))

    async def async_settings(self, run_time: str | None = None, building: str | None = None,
                             on: bool | None = None, use_account: bool = False) -> None:
        if run_time:
            _parse_time(run_time)
            self.run_time = run_time
            self._schedule()
        if use_account:
            self.mb_choice = None
            self._queue_rerun()
        elif building is not None:
            if self.mb_choice is None:
                self.mb_choice = {name: True for name in self.mb_account}
            self.mb_choice[building] = bool(on)
            self._queue_rerun()
        await self._save()

    def _queue_rerun(self) -> None:
        """Location toggles: search Mac.bid again a few seconds after the last change."""
        if self._rerun:
            self._rerun()

        @callback
        def _go(_now: Any) -> None:
            self._rerun = None
            self.hass.async_create_task(self.async_run())

        self._rerun = async_call_later(self.hass, 4, _go)

    # ---------------- what the page shows
    async def async_view(self) -> dict[str, Any]:
        await self._refresh_buildings()
        await self._import()
        coords = self._coordinators()
        merged: dict[str, dict[str, Any]] = {}
        for s in self.searches:
            for h in self.results.get(s.id, []):
                cur = merged.get(h["key"])
                if cur is None:
                    merged[h["key"]] = {**h, "searches": [s.id]}
                else:
                    cur["searches"].append(s.id)
                    cur["new"] = cur["new"] or h["new"]
        enabled = set(self.enabled_buildings())
        account = set(self.mb_account)
        buildings = sorted(
            ({"name": b["name"], "on": b["name"] in enabled, "account": b["name"] in account, "outpost": b["outpost"]}
             for b in self.mb_all),
            key=lambda b: (not b["account"], not b["on"], b["name"]))
        return {
            "searches": [{**to_dict(s), "count": len(self.results.get(s.id, [])),
                          "new": sum(1 for h in self.results.get(s.id, []) if h.get("new")),
                          "excluded": self.excluded.get(s.id, 0)} for s in self.searches],
            "results": sorted(merged.values(), key=lambda h: h.get("ends_at") or "9999"),
            "errors": self.errors,
            "last_run": self.last_run,
            "next_run": self.next_run(),
            "run_time": self.run_time,
            "running": self.running,
            "sites": [x for x in SITE_ORDER if x in coords],
            "macbid_buildings": buildings,
            "macbid_from_account": self.mb_choice is None,
            "excluded_total": sum(self.excluded.get(s.id, 0) for s in self.searches if SITE_MACBID in s.sites),
        }


# ---------------- websocket commands for the Auctions page (admin only)
def _manager(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> SearchManager | None:
    m = hass.data.get(DATA_KEY)
    if m is None:
        connection.send_error(msg["id"], "not_loaded", "Auction Watcher isn't set up yet")
    return m


@websocket_api.websocket_command({vol.Required("type"): "auction_watcher/searches"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_view(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    if (m := _manager(hass, connection, msg)) is not None:
        connection.send_result(msg["id"], await m.async_view())


@websocket_api.websocket_command({
    vol.Required("type"): "auction_watcher/searches/change",
    vol.Required("action"): vol.In(["add", "remove", "update", "run", "settings"]),
    vol.Optional("keyword"): str,
    vol.Optional("search_id"): str,
    vol.Optional("sites"): [vol.In(list(SITE_ORDER))],
    vol.Optional("max_price"): vol.All(vol.Coerce(float), vol.Range(min=0)),
    vol.Optional("save_to_macbid", default=False): bool,
    vol.Optional("run_time"): str,
    vol.Optional("building"): str,
    vol.Optional("on"): bool,
    vol.Optional("use_account", default=False): bool,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_change(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    if (m := _manager(hass, connection, msg)) is None:
        return
    try:
        action = msg["action"]
        if action == "add":
            if not (msg.get("keyword") or "").strip():
                raise ValueError("Type a keyword")
            await m.async_add(msg["keyword"], msg.get("sites"), msg.get("max_price") or 0.0, msg["save_to_macbid"])
        elif action == "remove":
            await m.async_remove(msg.get("search_id", ""))
        elif action == "update":
            await m.async_update(msg.get("search_id", ""), msg.get("sites"), msg.get("max_price"))
        elif action == "run":
            await m.async_run(only=msg.get("search_id"))
        else:
            await m.async_settings(msg.get("run_time"), msg.get("building"), msg.get("on"), msg["use_account"])
    except (SiteError, ValueError) as err:
        connection.send_error(msg["id"], "failed", str(err) or "That time isn't valid (use HH:MM)")
        return
    connection.send_result(msg["id"], await m.async_view())


@callback
def async_register_ws(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_view)
    websocket_api.async_register_command(hass, ws_change)
