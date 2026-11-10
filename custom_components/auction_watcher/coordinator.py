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
    DEFAULT_ALERT_MAX_BIDDERS,
    DEFAULT_ALERT_MAX_PRICE,
    DEFAULT_BID_LIMIT,
    DEFAULT_FAST_INTERVAL,
    DEFAULT_IDLE_INTERVAL,
    DEFAULT_POINT_COST,
    DOMAIN,
    EVENT_BID_LIMIT,
    EVENT_BOOKING,
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
from .logic import (
    AutoMemory,
    AutoSettings,
    BidderTracker,
    Rules,
    Snapshot,
    choose_interval,
    decide_auto,
    evaluate,
)
from .sites.base import STATUS_LIVE, STATUS_UPCOMING, AuctionState, AuthError, PriceHistory, SiteError
from .sites import govauctions as ga

_LOGGER = logging.getLogger(__name__)

EVENT_NAMES = {
    "opportunity": EVENT_OPPORTUNITY,
    "ended": EVENT_ENDED,
    "bid_limit": EVENT_BID_LIMIT,
    "won": EVENT_WON,
    "booked": EVENT_BOOKING,
    "cancelled": EVENT_BOOKING,
    "booking_failed": EVENT_BOOKING,
}
# Which "Which alerts to send" choice covers each event
ALERT_KIND_OF = {"booked": "booking", "cancelled": "booking", "booking_failed": "booking"}


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
        self._bidders = BidderTracker()
        self._auto: dict[str, AutoSettings] = {}
        self._mem: dict[str, AutoMemory] = {}

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
            stable_seconds=int(self._opt(e, CONF_STABLE_SECONDS, DEFAULT_STABLE_SECONDS)),
            limits=self._limits,
        )

    @property
    def opportunities(self) -> list[str]:
        return sorted(self._snapshot.opportunities)

    async def async_load(self) -> None:
        stored = await self._store.async_load() or {}
        self._limits = {str(k): int(v) for k, v in (stored.get("limits") or {}).items()}
        self._auto = {str(k): AutoSettings.from_dict(v) for k, v in (stored.get("auto") or {}).items()}
        self._mem = {str(k): AutoMemory(cancelled_with=int(v)) for k, v in (stored.get("cancelled_with") or {}).items()}

    async def _save(self) -> None:
        await self._store.async_save({
            "limits": self._limits,
            "auto": {k: vars(v) for k, v in self._auto.items()},
            "cancelled_with": {k: m.cancelled_with for k, m in self._mem.items() if m.cancelled_with},
        })

    async def async_set_bid_limit(self, auction_id: str, limit: int) -> None:
        if limit > 0:
            self._limits[str(auction_id)] = int(limit)
        else:
            self._limits.pop(str(auction_id), None)
        await self._save()
        self.async_update_listeners()

    def bid_limit(self, auction_id: str) -> int:
        return self.rules.bid_limit(auction_id)

    def auto_settings(self, auction_id: str) -> AutoSettings:
        return self._auto.get(str(auction_id), AutoSettings())

    def auto_status(self, auction_id: str) -> str:
        return self._mem.get(str(auction_id), AutoMemory()).status

    async def async_set_auto(self, auction_id: str, settings: AutoSettings) -> None:
        aid = str(auction_id)
        if settings.any_auto and not self.bid_limit(aid):
            raise SiteError("Set a bid limit for this auction before turning on auto-booking")
        if settings == AutoSettings():
            self._auto.pop(aid, None)
        else:
            self._auto[aid] = settings
        await self._save()
        self.async_update_listeners()

    # ---- booking (spends points)

    def _booking_guard(self, auction_id: str, count: int) -> int:
        """Clamp a manual booking to the bid limit and balance; raise if nothing is allowed."""
        s = (self.data or {}).get(auction_id)
        if s is None:
            raise SiteError(f"Auction {auction_id} isn't one you're following")
        limit = self.bid_limit(auction_id)
        if limit:
            room = limit - s.your_bids - s.your_booked_left
            if room <= 0:
                raise SiteError(f"Bid limit reached ({s.your_bids} used, {s.your_booked_left} booked, limit {limit})")
            count = min(count, room)
        if self.points is not None:
            count = min(count, self.points)
        if count < 1:
            raise SiteError("No points left to book")
        return count

    async def async_book(self, auction_id: str, count: int, max_price: float | None = None) -> int:
        aid = str(auction_id)
        count = self._booking_guard(aid, int(count))
        if max_price is None:
            max_price = self.auto_settings(aid).max_price or None
        await self.client.book(aid, count, max_price)
        self._emit("booked", self._booking_data(aid, count, "booked by you"))
        await self.async_request_refresh()
        return count

    async def async_cancel(self, auction_id: str) -> None:
        aid = str(auction_id)
        s = (self.data or {}).get(aid)
        if s is None:
            raise SiteError(f"Auction {aid} isn't one you're following")
        await self.client.cancel_booked(aid)
        self._emit("cancelled", self._booking_data(aid, s.your_booked_left, "cancelled by you"))
        await self.async_request_refresh()

    def _booking_data(self, aid: str, count: int, reason: str, state: AuctionState | None = None) -> dict[str, Any]:
        s = state or (self.data or {}).get(aid)
        return {
            "site": s.site if s else "",
            "auction_id": aid,
            "title": s.title if s else f"Auction {aid}",
            "url": s.url if s else "",
            "price": s.price if s else None,
            "active_bidders": s.active_bidders if s else None,
            "your_bids": s.your_bids if s else 0,
            "bid_limit": self.bid_limit(aid),
            "count": count,
            "reason": reason,
        }

    def _emit(self, kind: str, data: dict[str, Any]) -> None:
        e = self.config_entry
        payload = {**data, "account": e.title}
        if EVENT_NAMES[kind] == EVENT_BOOKING:
            payload["action"] = kind
        self.hass.bus.async_fire(EVENT_NAMES[kind], payload)
        if ALERT_KIND_OF.get(kind, kind) in set(self._opt(e, CONF_ALERT_KINDS, DEFAULT_ALERT_KINDS)):
            services = list(self._opt(e, CONF_NOTIFY_SERVICES, []))
            entities = list(self._opt(e, CONF_NOTIFY_ENTITIES, []))
            for domain, service, service_data in build_calls(kind, data, services, entities):
                self.hass.async_create_task(self._send(domain, service, service_data))

    async def _run_auto(self, states: dict[str, AuctionState], now: float) -> None:
        """Opt-in auto-cancel / auto-rebook, one decision per auction per check."""
        rules = self.rules
        for aid, settings in list(self._auto.items()):
            s = states.get(aid)
            if s is None:
                continue
            mem = self._mem.setdefault(aid, AutoMemory())
            action, count, reason = decide_auto(s, settings, mem, rules, self.points, now)
            mem.status = reason
            if not action:
                continue
            mem.last_attempt = now
            try:
                if action == "cancel":
                    await self.client.cancel_booked(aid)
                    mem.cancelled_with = count
                    mem.status = f"cancelled {count} booked bids ({reason}); will rebook when it thins out"
                    s.your_booked_left = 0
                    self._emit("cancelled", self._booking_data(aid, count, f"auto: {reason}", s))
                else:
                    await self.client.book(aid, count, settings.max_price or None)
                    mem.cancelled_with = 0
                    mem.status = f"booked {count} bids ({reason})"
                    s.your_booked_left = count
                    self._emit("booked", self._booking_data(aid, count, f"auto: {reason}", s))
                await self._save()
            except AuthError:
                raise
            except SiteError as err:
                mem.status = f"{action} failed: {err}"
                _LOGGER.warning("Auto-%s on auction %s failed: %s", action, aid, err)
                self._emit("booking_failed", self._booking_data(aid, count, str(err), s))

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
        window_min = float(self._opt(e, CONF_ACTIVE_WINDOW, DEFAULT_ACTIVE_WINDOW))
        states: dict[str, AuctionState] = {}
        for aid in tracked:
            static = self._static.get(aid)
            key = (static or {}).get("product_id") or aid
            hist = (self._history.get(key) or (0, None))[1]
            st = ga.build_state(aid, live.get(aid), static, hist, watching, point_cost, now)
            rows = (live.get(aid) or {}).get("bid_rows")
            if rows is not None:
                # Count everyone seen in the last few minutes, not just the last 9 bids
                self._bidders.add(aid, rows)
                active = self._bidders.active(aid, now, window_min * 60)
                st.active_bidders, st.recent_bidders = len(active), active
            st.extra["active_window_minutes"] = window_min
            states[aid] = st
        self._bidders.forget_except(set(tracked))

        events, self._snapshot = evaluate(states, self.rules, self._snapshot, self._lists["won"], now)
        for kind, data in events:
            self._emit(kind, data)

        try:
            await self._run_auto(states, now)
        except AuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err

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
