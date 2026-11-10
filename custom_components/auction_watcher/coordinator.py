"""Polls one site account and keeps every followed auction up to date."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .alerts import build_calls
from .const import (
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
    DEFAULT_ALERT_MAX_BIDDERS,
    DEFAULT_ALERT_MAX_PRICE,
    DEFAULT_BID_LIMIT,
    DEFAULT_FAST_INTERVAL,
    DEFAULT_IDLE_INTERVAL,
    DEFAULT_POINT_COST,
    DOMAIN,
    EVENT_BID_LIMIT,
    EVENT_ENDED,
    EVENT_OPPORTUNITY,
    EVENT_WON,
    HISTORY_FETCHES_PER_UPDATE,
    HISTORY_REFRESH_SECONDS,
    LISTS_REFRESH_SECONDS,
    STORAGE_VERSION,
    UPCOMING_INTERVAL,
    UPCOMING_WINDOW,
)
from .cookies import cookie_header
from .logic import Rules, Snapshot, choose_interval, evaluate
from .sites.base import STATUS_LIVE, STATUS_UPCOMING, AuctionState, AuthError, PriceHistory, SiteError
from .sites import govauctions as ga

_LOGGER = logging.getLogger(__name__)

EVENT_NAMES = {
    "opportunity": EVENT_OPPORTUNITY,
    "ended": EVENT_ENDED,
    "bid_limit": EVENT_BID_LIMIT,
    "won": EVENT_WON,
}


class AuctionWatcherCoordinator(DataUpdateCoordinator[dict[str, AuctionState]]):
    """GovAuctions account coordinator (other sites get their own client)."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: ga.GovAuctionsClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(seconds=self._opt(entry, CONF_FAST_INTERVAL, DEFAULT_FAST_INTERVAL)),
        )
        self.client = client
        self.points: int | None = None
        self.mode = "starting"
        self.options_snapshot = dict(entry.options)
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._limits: dict[str, int] = {}
        self._lists: dict[str, list[str]] | None = None
        self._lists_at = 0.0
        self._static: dict[str, dict[str, Any]] = {}
        self._history: dict[str, tuple[float, PriceHistory | None]] = {}
        self._snapshot = Snapshot()

    @staticmethod
    def _opt(entry: ConfigEntry, key: str, default: Any) -> Any:
        return entry.options.get(key, default)

    @property
    def rules(self) -> Rules:
        e = self.config_entry
        return Rules(
            max_bidders=int(self._opt(e, CONF_ALERT_MAX_BIDDERS, DEFAULT_ALERT_MAX_BIDDERS)),
            max_price=float(self._opt(e, CONF_ALERT_MAX_PRICE, DEFAULT_ALERT_MAX_PRICE)),
            default_bid_limit=int(self._opt(e, CONF_DEFAULT_BID_LIMIT, DEFAULT_BID_LIMIT)),
            limits=self._limits,
        )

    @property
    def opportunities(self) -> list[str]:
        return sorted(self._snapshot.opportunities)

    async def async_load(self) -> None:
        stored = await self._store.async_load() or {}
        self._limits = {str(k): int(v) for k, v in (stored.get("limits") or {}).items()}

    async def async_set_bid_limit(self, auction_id: str, limit: int) -> None:
        if limit > 0:
            self._limits[str(auction_id)] = int(limit)
        else:
            self._limits.pop(str(auction_id), None)
        await self._store.async_save({"limits": self._limits})
        self.async_update_listeners()

    def bid_limit(self, auction_id: str) -> int:
        return self.rules.bid_limit(auction_id)

    async def _send(self, domain: str, service: str, data: dict[str, Any]) -> None:
        """Send one alert. A broken target (phone removed, Alexa offline) never stops polling."""
        try:
            await self.hass.services.async_call(domain, service, data, blocking=True)
        except Exception as err:  # noqa: BLE001
            target = data.get("entity_id") or f"{domain}.{service}"
            _LOGGER.warning("Couldn't send auction alert to %s: %s", target, err)

    async def _async_update_data(self) -> dict[str, AuctionState]:
        e = self.config_entry
        now = time.time()
        fast_mode = self.mode == "fast"
        try:
            if self._lists is None or not fast_mode or now - self._lists_at >= LISTS_REFRESH_SECONDS:
                self._lists = await self.client.lists()
                self._lists_at = now
            extra = [x.strip() for x in str(self._opt(e, CONF_EXTRA_IDS, "")).replace(",", " ").split() if x.strip().isdigit()]
            tracked = sorted(set(self._lists["all"]) | set(self._lists["watching"]) | set(extra))

            # One request covers every followed auction, and keeps the login fresh when idle
            live, self.points = await self.client.live(tracked, now)

            missing = [i for i in tracked if i not in self._static]
            if missing:
                self._static.update(await self.client.static(missing))

            fetched = 0
            for aid in tracked:
                if fetched >= HISTORY_FETCHES_PER_UPDATE:
                    break
                if not live.get(aid, {}).get("live") and aid not in self._lists["watching"]:
                    continue
                key = (self._static.get(aid) or {}).get("product_id") or aid
                cached = self._history.get(key)
                if cached is None or now - cached[0] > HISTORY_REFRESH_SECONDS:
                    self._history[key] = (now, await self.client.history(aid))
                    fetched += 1
        except AuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SiteError as err:
            raise UpdateFailed(str(err)) from err

        point_cost = float(self._opt(e, CONF_POINT_COST, DEFAULT_POINT_COST))
        watching = set(self._lists["watching"])
        states: dict[str, AuctionState] = {}
        for aid in tracked:
            static = self._static.get(aid)
            key = (static or {}).get("product_id") or aid
            hist = (self._history.get(key) or (0, None))[1]
            states[aid] = ga.build_state(aid, live.get(aid), static, hist, watching, point_cost, now)

        events, self._snapshot = evaluate(states, self.rules, self._snapshot, self._lists["won"])
        kinds = set(self._opt(e, CONF_ALERT_KINDS, DEFAULT_ALERT_KINDS))
        services = list(self._opt(e, CONF_NOTIFY_SERVICES, []))
        entities = list(self._opt(e, CONF_NOTIFY_ENTITIES, []))
        for kind, data in events:
            self.hass.bus.async_fire(EVENT_NAMES[kind], {**data, "account": e.title})
            if kind in kinds:
                for domain, service, service_data in build_calls(kind, data, services, entities):
                    self.hass.async_create_task(self._send(domain, service, service_data))

        seconds = choose_interval(
            states, now,
            fast=int(self._opt(e, CONF_FAST_INTERVAL, DEFAULT_FAST_INTERVAL)),
            idle_minutes=int(self._opt(e, CONF_IDLE_INTERVAL, DEFAULT_IDLE_INTERVAL)),
            upcoming=UPCOMING_INTERVAL, window=UPCOMING_WINDOW,
        )
        self.mode = "fast" if any(s.status == STATUS_LIVE for s in states.values()) else (
            "upcoming" if any(s.status == STATUS_UPCOMING for s in states.values()) and seconds == UPCOMING_INTERVAL else "idle")
        self.update_interval = timedelta(seconds=seconds)

        # Save cookies the site refreshed, so the login keeps renewing itself
        if self.client.cookies_changed:
            self.client.cookies_changed = False
            self.hass.config_entries.async_update_entry(
                e, data={**e.data, CONF_COOKIE: cookie_header(self.client.cookies)}
            )
        return states
