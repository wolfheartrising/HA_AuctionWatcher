"""Polls one site account and keeps every followed auction up to date."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .alerts import build_calls, human_time
from .const import (
    CONF_SITE,
    SITE_MACBID,
    CONF_MACBID_BUDGET,
    CONF_WEEKLY_BUDGET,
    CONF_PACKS_ENABLED,
    PACKS_LIVE_SECONDS,
    PACKS_SCAN_SECONDS,
    CONF_EBAY_TOKENS,
    CONF_EBAY_USER,
    CONF_KEEP_ENDED_DAYS,
    DEFAULT_KEEP_ENDED_DAYS,
    CONF_AUTH,
    CONF_ENDING_MINUTES,
    CONF_PICKUP_HOURS,
    DEFAULT_ENDING_MINUTES,
    DEFAULT_PICKUP_HOURS,
    PICKUP_REFRESH_SECONDS,
    WATCHLIST_REFRESH_SECONDS,
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
    EVENT_ENDING_SOON,
    EVENT_OUTBID,
    EVENT_OVER_MAX,
    EVENT_PICKUP,
    EVENT_PACK,
    EVENT_BUDGET,
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
    TimedSnapshot,
    choose_interval,
    choose_timed_interval,
    decide_auto,
    evaluate,
    evaluate_timed,
    MB_MODE_OFF,
    MacBidMemory,
    MacBidSettings,
    decide_macbid,
    keep_ended,
    macbid_committed,
    points_at_risk,
    record_spending,
    record_wins,
    spent_since,
    week_start,
    wins_since,
)
from .sites.base import (
    STATUS_ENDED,
    STATUS_LIVE,
    STATUS_UPCOMING,
    AuctionState,
    AuthError,
    PriceHistory,
    SiteError,
    state_from_dict,
    state_to_dict,
)
from . import global_settings, proxy_manager
from . import packs as packs_mod
from .sites import govauctions as ga
from .sites import ebay as eb
from .sites import macbid as mb

_LOGGER = logging.getLogger(__name__)

EVENT_NAMES = {
    "opportunity": EVENT_OPPORTUNITY,
    "ended": EVENT_ENDED,
    "bid_limit": EVENT_BID_LIMIT,
    "won": EVENT_WON,
    "booked": EVENT_BOOKING,
    "cancelled": EVENT_BOOKING,
    "booking_failed": EVENT_BOOKING,
    "outbid": EVENT_OUTBID,
    "ending_soon": EVENT_ENDING_SOON,
    "over_max": EVENT_OVER_MAX,
    "pickup": EVENT_PICKUP,
    "pack": EVENT_PACK,
    "budget": EVENT_BUDGET,
    "bid_placed": EVENT_BOOKING,
    "bid_failed": EVENT_BOOKING,
}
# Which "Which alerts to send" choice covers each event
ALERT_KIND_OF = {"booked": "booking", "cancelled": "booking", "booking_failed": "booking",
                 "bid_placed": "booking", "bid_failed": "booking"}


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
            # "Check now" requests (the open Auctions page asks every few seconds) at most every 2 s
            request_refresh_debouncer=Debouncer(hass, _LOGGER, cooldown=2.0, immediate=True),
        )
        self.client = client
        self.points: int | None = None
        self.username: str | None = None
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
        self._last_seen: dict[str, AuctionState] = {}
        self._ended: dict[str, tuple[float, AuctionState]] = {}
        self.expired_ids: list[str] = []  # ended long enough ago that their sensors can go
        self._original_end: dict[str, float] = {}  # Mac.bid lot -> first end time seen (saved)
        self._needs_save = False
        # Point-pack scanner (GovAuctions)
        self.packs: list[dict[str, Any]] = []
        self.packs_scanned_at: float | None = None
        self._packs_live_at = 0.0
        self._pack_static: dict[str, dict[str, Any]] = {}
        self._pack_hist: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._pack_alerted: set[str] = set()
        # Spending and the auto-booking switch (GovAuctions)
        self._ledger: list[list[float]] = []   # [time, bids placed, auction]
        self._last_bids: dict[str, int] = {}
        self.auto_paused = False
        self._budget_alerted_week: float | None = None
        self.spending: dict[str, Any] = {}

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
        self._ended = {}
        for k, (t, d) in (stored.get("ended") or {}).items():
            try:
                self._ended[str(k)] = (float(t), state_from_dict(d))
            except (TypeError, ValueError, KeyError):
                continue
        self._last_seen = {k: st for k, (_, st) in self._ended.items()}
        self._original_end = {str(k): float(v) for k, v in (stored.get("original_end") or {}).items()}
        self._ledger = [list(r) for r in (stored.get("ledger") or [])]
        self._last_bids = {str(k): int(v) for k, v in (stored.get("last_bids") or {}).items()}
        self.auto_paused = bool(stored.get("auto_paused", False))

    async def _save(self) -> None:
        await self._store.async_save({
            "limits": self._limits,
            "auto": {k: vars(v) for k, v in self._auto.items()},
            "cancelled_with": {k: m.cancelled_with for k, m in self._mem.items() if m.cancelled_with},
            "ended": {k: [t, state_to_dict(st)] for k, (t, st) in self._ended.items()},
            "original_end": self._original_end,
            "ledger": self._ledger,
            "last_bids": self._last_bids,
            "auto_paused": self.auto_paused,
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

    def _budget(self) -> int:
        return int(self._opt(self.config_entry, CONF_WEEKLY_BUDGET, 0) or 0)

    def _budget_left(self, now: float, states: dict[str, AuctionState] | None = None) -> int | None:
        """Points still allowed: the GovAuctions weekly budget (bids placed this week + bids already
        booked) and the spending cap across all sites, whichever is tighter. None = no limit."""
        left: int | None = None
        budget = self._budget()
        if budget:
            spent = spent_since(self._ledger, week_start(dt_util.as_local(dt_util.utc_from_timestamp(now))))
            booked = sum(s.your_booked_left for s in (states if states is not None else (self.data or {})).values()
                         if s.status in (STATUS_LIVE, STATUS_UPCOMING))
            left = budget - spent - booked
        g = global_settings.get(self.hass)
        cap_left = g.left(now) if g else None
        if cap_left is not None:
            cost = float(self._opt(self.config_entry, CONF_POINT_COST, DEFAULT_POINT_COST)) or DEFAULT_POINT_COST
            pts = int(max(cap_left, 0) // cost)
            left = pts if left is None else min(left, pts)
        return left

    async def async_watch(self, item_id: str, on: bool = True) -> None:
        """Bookmark on GovAuctions (shows as Watching)."""
        await self.client.bookmark(item_id, on)
        self._lists = None
        await self.async_request_refresh()

    def dollars_used(self, since: float, exclude: str | None = None) -> float:
        """Counted toward the spending cap: points used since `since` plus points held in booked bids."""
        cost = float(self._opt(self.config_entry, CONF_POINT_COST, DEFAULT_POINT_COST))
        booked = sum(s.your_booked_left for s in (self.data or {}).values() if s.status in (STATUS_LIVE, STATUS_UPCOMING))
        return round((spent_since(self._ledger, since) + booked) * cost, 2)

    async def async_set_auto_paused(self, paused: bool) -> None:
        self.auto_paused = paused
        await self._save()
        self.async_update_listeners()

    async def _track_spending(self, states: dict[str, AuctionState], now: float) -> None:
        """Ledger of bids placed (your_bids going up), weekly budget alert, and the at-risk tally."""
        if record_spending(self._ledger, self._last_bids, states, now):
            await self._save()
        local = dt_util.as_local(dt_util.utc_from_timestamp(now))
        week = week_start(local)
        today = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        budget = self._budget()
        spent_week = spent_since(self._ledger, week)
        risk = points_at_risk(states, self._auto, self.rules)
        cost = float(self._opt(self.config_entry, CONF_POINT_COST, DEFAULT_POINT_COST))
        self.spending = {
            "balance": self.points,
            "booked": risk["booked"],
            "earmarked": risk["earmarked"],
            "at_risk": risk["booked"] + risk["earmarked"],
            "at_risk_dollars": round((risk["booked"] + risk["earmarked"]) * cost, 2),
            "spent_today": spent_since(self._ledger, today),
            "spent_week": spent_week,
            "spent_week_dollars": round(spent_week * cost, 2),
            "weekly_budget": budget or None,
            "budget_left": (budget - spent_week - risk["booked"]) if budget else None,
            "auto_paused": self.auto_paused,
        }
        if (g := global_settings.get(self.hass)) is not None:
            self.spending["cap"] = g.summary(now)
            await g.async_check(now)
        if budget and spent_week >= budget and self._budget_alerted_week != week:
            self._budget_alerted_week = week
            self._emit("budget", {"site": "govauctions", "auction_id": "budget", "title": "Weekly budget",
                                  "url": ga.BASE_URL, "spent": spent_week, "budget": budget, "bid_limit": 0})

    async def _scan_packs(self, now: float) -> None:
        """Every hour: the site's Packs list, scored from each pack's past winners.
        Live price/entry state is refreshed every 10 minutes in between. Never breaks updates."""
        if not self._opt(self.config_entry, CONF_PACKS_ENABLED, True):
            self.packs = []
            return
        full = self.packs_scanned_at is None or now - self.packs_scanned_at >= PACKS_SCAN_SECONDS
        if not full and now - self._packs_live_at < PACKS_LIVE_SECONDS:
            return
        try:
            client: ga.GovAuctionsClient = self.client
            if full:
                ids = await client.browse_category(ga.PACKS_CATEGORY)
                missing = [i for i in ids if i not in self._pack_static]
                for i in range(0, len(missing), 50):
                    self._pack_static.update(await client.static(missing[i:i + 50]))
                self._pack_ids = [i for i in ids if packs_mod.parse_points((self._pack_static.get(i) or {}).get("title", ""))]
                fetched = 0
                for aid in self._pack_ids:
                    pid = self._pack_static[aid].get("product_id") or aid
                    cached = self._pack_hist.get(pid)
                    # Past winners per pack size, refreshed daily (~36 sizes, about 15 s in all)
                    if (cached is None or now - cached[0] > 86400) and fetched < 60:
                        self._pack_hist[pid] = (now, await client.previous_winners(aid))
                        fetched += 1
                        await asyncio.sleep(0.3)
                self.packs_scanned_at = now
            ids = getattr(self, "_pack_ids", [])
            live, _ = await client.live(ids, now) if ids else ({}, None)
            self._packs_live_at = now
        except (SiteError, AuthError) as err:
            _LOGGER.warning("Point-pack scan skipped: %s", err)
            return

        tz = dt_util.get_default_time_zone()
        cost = float(self._opt(self.config_entry, CONF_POINT_COST, DEFAULT_POINT_COST))
        rows = []
        for aid in ids:
            s = self._pack_static.get(aid) or {}
            points = packs_mod.parse_points(s.get("title", ""))
            l = live.get(aid) or {}
            start = s.get("starts_at").timestamp() if s.get("starts_at") else None
            if l and not l.get("live") and start and start < now:
                continue  # ended
            winners = (self._pack_hist.get(s.get("product_id") or aid) or (0, []))[1]
            stats = packs_mod.pack_stats(winners, points, tz)
            score = packs_mod.score_pack(points, stats, start, tz)
            suggestion = packs_mod.suggest_max(points, winners) or {}
            open_, to_lock = packs_mod.entry_open(l.get("price"), s.get("no_jumper_limit"), l.get("closed_to_new"))
            rows.append({
                "id": aid, "product_id": str(s.get("product_id") or aid), "title": s.get("title"), "points": points, "value": round(points * cost, 2),
                "url": f"{ga.BASE_URL}/auction/{aid}", "image": s.get("image"),
                "starts_at": s["starts_at"].isoformat() if s.get("starts_at") else None,
                "price": l.get("price"), "clock": l.get("clock_seconds"), "live": bool(l.get("live")),
                "bidders": l.get("active_bidders"), "your_bids": l.get("your_bids", 0),
                "open": open_, "bids_to_lock": to_lock, "entry_limit": s.get("no_jumper_limit"),
                "labels": s.get("labels", []), "grade": score["grade"], "ratio": score["ratio"],
                "expected_bids": score["expected_bids"], "block": score["block"], "cheapest_block": score.get("cheapest_block"),
                "past_wins": stats["wins"] if stats else 0, "median_bids": stats["median"] if stats else None,
                "lost_money": stats["lost_money"] if stats else None,
                "blocks": stats["blocks"] if stats else {},
                "suggested_max": suggestion.get("max"), "cover": suggestion.get("cover"),
                "net_win": suggestion.get("net_win"),
                "stars": packs_mod.stars_for(score["ratio"]) if score["grade"] != "unknown" else None,
                "affordable": bool(suggestion) and self.points is not None and suggestion["max"] <= self.points,
                "balance": self.points,
                "max_cost": round(suggestion["max"] * cost, 2) if suggestion else None,
                "bid_limit": self.bid_limit(aid) or None,
            })
        order = {"green": 0, "yellow": 1, "unknown": 2, "red": 3}
        rows.sort(key=lambda r: (not r["open"], not r["affordable"], -(r["stars"] or 0), order[r["grade"]], r["starts_at"] or ""))
        self.packs = rows
        # One alert per good pack that's still open to new bidders
        for r in rows:
            if (r["stars"] or 0) >= 3 and r["affordable"] and r["open"] and r["id"] not in self._pack_alerted:
                self._pack_alerted.add(r["id"])
                self._emit("pack", {"site": "govauctions", "auction_id": r["id"], "title": r["title"], "url": r["url"],
                                    "price": r["price"], "points": r["points"], "value": r["value"],
                                    "expected_bids": r["expected_bids"], "bids_to_lock": r["bids_to_lock"],
                                    "past_wins": r["past_wins"], "bid_limit": 0,
                                    "suggested_max": r["suggested_max"], "cover": r["cover"]})
        self._pack_alerted &= {r["id"] for r in rows}
        joined = [r for r in rows if r["your_bids"] and r["suggested_max"] and str(r["id"]) not in self._limits]
        for r in joined:
            self._limits[str(r["id"])] = int(r["suggested_max"])
            r["bid_limit"] = int(r["suggested_max"])
        if joined:
            await self._save()

    async def _apply_keep_ended(self, states: dict[str, AuctionState], now: float) -> dict[str, AuctionState]:
        """Keep ended (or dropped) auctions on the page for the configured number of days."""
        days = float(self._opt(self.config_entry, CONF_KEEP_ENDED_DAYS, DEFAULT_KEEP_ENDED_DAYS))
        for st in states.values():
            if st.status == STATUS_LIVE and st.original_end and now > st.original_end.timestamp():
                st.extra["overtime_seconds"] = int(now - st.original_end.timestamp())
        before = {k: t for k, (t, _) in self._ended.items()}
        shown, self._last_seen, self._ended, self.expired_ids = keep_ended(states, self._last_seen, self._ended, now, days)
        if before != {k: t for k, (t, _) in self._ended.items()} or self._needs_save:
            # forget original end times of lots that are gone for good
            self._original_end = {k: v for k, v in self._original_end.items() if k in shown or k in self._ended}
            self._needs_save = False
            await self._save()  # only write to disk when the ended list changes
        return shown

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
        left = self._budget_left(time.time())
        if left is not None:
            if left <= 0:
                raise SiteError("Your budget is used up: the GovAuctions weekly budget or the spending cap on the Settings tab")
            count = min(count, left)
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
        # Where alerts go and which ones: the Settings tab on the Auctions page (same for every site)
        if (g := global_settings.get(self.hass)) is not None:
            g.send(kind, data, ALERT_KIND_OF.get(kind, kind))
        # Bidding for someone: they hear how their item is going too
        if (p := proxy_manager.get(self.hass)) is not None:
            p.relay(e.data.get(CONF_SITE, "govauctions"), kind, data)

    async def _run_auto(self, states: dict[str, AuctionState], now: float) -> None:
        """Opt-in auto-cancel / auto-rebook, one decision per auction per check."""
        rules = self.rules
        # One shared balance: auctions booking in the same check draw from it in turn,
        # best opportunity (fewest other bidders) first, so they can't spend the same points twice.
        available = self.points
        budget_left = self._budget_left(now, states)
        order = sorted(self._auto.items(), key=lambda kv: (states[kv[0]].active_bidders if kv[0] in states
                                                            and states[kv[0]].active_bidders is not None else 99))
        for aid, settings in order:
            s = states.get(aid)
            if s is None:
                continue
            mem = self._mem.setdefault(aid, AutoMemory())
            action, count, reason = decide_auto(s, settings, mem, rules, available, now,
                                                budget_left=budget_left, paused=self.auto_paused)
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
                    result = await self.client.book(aid, count, settings.max_price or None, partial=True)
                    try:
                        booked = int(result.get("bookedBids") or count)
                    except (TypeError, ValueError):
                        booked = count
                    count = booked
                    if available is not None:
                        available -= count
                    if budget_left is not None:
                        budget_left -= count
                    mem.cancelled_with = 0
                    mem.status = f"booked {count} bids ({reason})"
                    s.your_booked_left = (s.your_booked_left or 0) + count
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

            if self.username is None:
                try:
                    self.username = (await self.client.me())["name"] or ""
                except SiteError:
                    self.username = None  # try again next time; counts include you until then

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
                # Count everyone seen in the last few minutes, not just the last 9 bids,
                # and leave you out: "2" always means two OTHER people.
                self._bidders.add(aid, rows)
                active = self._bidders.active(aid, now, window_min * 60)
                others = [u for u in active if u != self.username]
                st.active_bidders, st.recent_bidders = len(others), others
                st.extra["you_are_bidding"] = self.username in active
            if self.username:
                st.extra["winning"] = st.status == STATUS_LIVE and st.leader == self.username
            st.extra["active_window_minutes"] = window_min
            st.extra["point_cost"] = point_cost
            states[aid] = st
        self._bidders.forget_except(set(tracked))
        states = await self._apply_keep_ended(states, now)
        await self._scan_packs(now)
        await self._track_spending(states, now)

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


class MacBidCoordinator(AuctionWatcherCoordinator):
    """Mac.bid account: watchlist + recent bids, live lot data, and won items awaiting pickup.

    Shares alerts, max-bid storage and notifications with the GovAuctions coordinator;
    no booking or auto-bidding here.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: mb.MacBidClient) -> None:
        super().__init__(hass, entry, client)  # type: ignore[arg-type]
        self._watch: dict[str, dict[str, Any]] = {}
        self._watch_at = 0.0
        self._pickups: dict[str, AuctionState] = {}
        self._pickups_at = 0.0
        self._timed = TimedSnapshot()
        self._known_lots: set[str] = set()
        self._lots: dict[str, tuple[float, dict[str, Any]]] = {}  # lot_id -> (fetched at, data)
        self._locs: dict[str, dict[str, Any]] = {}
        self._locs_at = 0.0
        self._details: dict[str, tuple[float, dict[str, Any]]] = {}  # lot_id -> (fetched at, parsed /lot/{id})
        self._mb: dict[str, MacBidSettings] = {}
        self._mbmem: dict[str, MacBidMemory] = {}
        self._mb_wins: dict[str, list[float]] = {}  # lot -> [first seen as won, price]: counts toward the spending cap
        self._backoff_until = 0.0

    async def async_load(self) -> None:
        await super().async_load()
        stored = await self._store.async_load() or {}
        self._mb = {str(k): MacBidSettings.from_dict(v) for k, v in (stored.get("macbid_bids") or {}).items()}
        self._mbmem = {str(k): MacBidMemory(placed=int(v)) for k, v in (stored.get("macbid_placed") or {}).items()}
        self._mb_wins = {str(k): list(v) for k, v in (stored.get("macbid_wins") or {}).items()}

    async def _save(self) -> None:
        await super()._save()
        data = await self._store.async_load() or {}
        data["macbid_bids"] = {k: vars(v) for k, v in self._mb.items() if v.mode != MB_MODE_OFF}
        data["macbid_placed"] = {k: m.placed for k, m in self._mbmem.items() if m.placed}
        cutoff = time.time() - 40 * 86400  # wins only matter for this week / month
        data["macbid_wins"] = {k: v for k, v in getattr(self, "_mb_wins", {}).items() if v[0] >= cutoff}
        await self._store.async_save(data)

    def macbid_settings(self, lot_id: str) -> MacBidSettings:
        return self._mb.get(str(lot_id), MacBidSettings())

    def macbid_memory(self, lot_id: str) -> MacBidMemory:
        return self._mbmem.get(str(lot_id), MacBidMemory())

    async def async_set_macbid(self, lot_id: str, settings: MacBidSettings) -> None:
        aid = str(lot_id)
        if settings.mode != MB_MODE_OFF and not self.bid_limit(aid):
            raise SiteError("Set a max bid for this item first")
        self._mb[aid] = settings
        await self._save()
        self.async_update_listeners()

    def _macbid_budget_left(self, states: dict[str, AuctionState], exclude: str | None = None) -> int | None:
        """Dollars a new max on `exclude` may use: the Mac.bid budget and the spending cap across
        all sites, whichever is tighter (that item's own current max isn't counted). None = no limit."""
        left: int | None = None
        budget = int(self._opt(self.config_entry, CONF_MACBID_BUDGET, 0) or 0)
        if budget:
            others = {k: v for k, v in states.items() if k != exclude}
            c = macbid_committed(others, {k: m.placed for k, m in self._mbmem.items()}, self._limits,
                                 {k: v.mode for k, v in self._mb.items()})
            left = budget - c["placed"] - c["scheduled"]
        g = global_settings.get(self.hass)
        cap_left = g.left(time.time(), exclude=(self, exclude or "")) if g else None
        if cap_left is not None:
            left = int(max(cap_left, 0)) if left is None else min(left, int(max(cap_left, 0)))
        return left

    async def async_watch(self, item_id: str, on: bool = True) -> None:
        """Add to (or remove from) your Mac.bid watchlist."""
        await self.client.watch(item_id, on)
        self._watch_at = 0.0
        await self.async_request_refresh()

    def dollars_used(self, since: float, exclude: str | None = None) -> float:
        """Counted toward the spending cap: max bids tied up on live items (and scheduled late bids)
        plus items won since `since` (at their winning price)."""
        states = {k: v for k, v in (self.data or {}).items() if k != exclude}
        c = macbid_committed(states, {k: m.placed for k, m in self._mbmem.items()}, self._limits,
                             {k: v.mode for k, v in self._mb.items() if k != exclude})
        return round(c["placed"] + c["scheduled"] + wins_since(self._mb_wins, since), 2)

    async def async_place_bid(self, lot_id: str, amount: int, assurance: bool = False) -> int:
        """Manual max bid (the page's button / the place_bid service)."""
        aid = str(lot_id)
        s = (self.data or {}).get(aid)
        if s is None or s.status != STATUS_LIVE:
            raise SiteError("That item isn't live")
        now = await self.async_lot_now(aid)  # the price may have moved since the last check
        if amount < now["min_next"]:
            raise SiteError(f"The current bid is ${now['price']:.0f}; Mac.bid's minimum next bid is ${now['min_next']}"
                            + ("" if now["min_next_known"] else " or more (its raise steps grow with the price)"))
        left = self._macbid_budget_left(self.data or {}, exclude=aid)
        if left is not None and amount > left:
            raise SiteError(f"A ${amount} max would go past your budget (${max(left, 0)} left: Mac.bid budget or the spending cap)")
        await self._place(s, int(amount), "placed by you", assurance)
        await self.async_request_refresh()
        return int(amount)

    def _in_play(self, states: dict[str, AuctionState] | None = None) -> set[str]:
        """Items you're bidding on (or for someone), so they're checked often: you have a bid in,
        you're winning, a bid is being confirmed, a late bid is scheduled, or someone's request is open."""
        states = (self.data or {}) if states is None else states
        ids = {k for k, s in states.items() if s.extra.get("you_are_bidding") or s.extra.get("winning")}
        ids |= {k for k, m in self._mbmem.items() if m.placed or m.pending}
        ids |= {k for k, v in self._mb.items() if v.mode != MB_MODE_OFF}
        if (p := proxy_manager.get(self.hass)) is not None and p.enabled:
            ids |= {r.lot_id for r in p.requests.values() if r.site == SITE_MACBID and r.state in ("asked", "confirming", "confirmed", "watching")}
        return ids

    async def async_lot_now(self, lot_id: str) -> dict[str, Any]:
        """Read one item from Mac.bid right now (and keep it for the next update)."""
        client: mb.MacBidClient = self.client  # type: ignore[assignment]
        lot = await client.lot(lot_id)
        lot = lot if isinstance(lot, dict) else {}
        self._lots[lot_id] = (time.time(), lot)
        if not getattr(self, "_logged_lot_fields", False):
            self._logged_lot_fields = True
            _LOGGER.info("Mac.bid lot fields (for the minimum-bid check): %s",
                         {k: v for k, v in lot.items() if not isinstance(v, (dict, list))})
        return mb.lot_now(lot, client.user_id)

    async def _place(self, s: AuctionState, amount: int, reason: str, assurance: bool = False) -> None:
        client: mb.MacBidClient = self.client  # type: ignore[assignment]
        mem = self._mbmem.setdefault(s.auction_id, MacBidMemory())
        mem.last_attempt = time.time()
        answer = await client.place_bid(s.auction_id, s.extra.get("lot_number") or "", s.extra.get("mb_auction_id") or "", amount)
        mem.answer = mb.short_answer(answer)
        _LOGGER.info("Mac.bid answered a $%s max bid on lot %s: %s", amount, s.auction_id, mem.answer)
        # Mac.bid accepts the request first and processes it after, so it only counts
        # once its own lot data shows you as a bidder (checked on the next updates).
        mem.pending, mem.pending_since = amount, time.time()
        mem.status = f"sent a ${amount} max ({reason}); checking that Mac.bid recorded it… (answer: {mem.answer})"
        if assurance and not s.extra.get("has_assurance"):
            try:
                await client.set_assurance(s.auction_id, True)
                mem.status += f"; Buyer's Assurance added (${s.extra.get('assurance_cost') or '?'})"
            except SiteError as err:
                mem.status += f"; Buyer's Assurance not added: {err}"
        self._lots.pop(s.auction_id, None)  # re-read this lot on the next check
        self._pending_reason = getattr(self, "_pending_reason", {})
        self._pending_reason[s.auction_id] = reason
        await self.async_request_refresh()

    async def _verify_pending(self, states: dict[str, AuctionState], now: float) -> None:
        """Confirm sent bids from Mac.bid's own data (you show up as a bidder, or as the winner)."""
        for aid, mem in self._mbmem.items():
            if not mem.pending:
                continue
            s = states.get(aid)
            reason = getattr(self, "_pending_reason", {}).get(aid, "")
            if s is not None and (s.extra.get("you_are_bidding") or s.extra.get("winning")):
                sent = mem.pending
                lot = (self._lots.get(aid) or (0, {}))[1] or {}
                recorded = mb.lot_now(lot, self.client.user_id).get("your_max")
                mem.placed, mem.pending = int(recorded) if recorded else sent, 0
                raised = bool(recorded and recorded > sent)
                mem.status = (f"max bid ${mem.placed} confirmed: "
                              + ("you're winning" if s.extra.get("winning") else "someone's max is higher")
                              + "; Mac.bid raises for you up to your max")
                if raised:
                    mem.status = (f"Mac.bid recorded a ${int(recorded)} max, not the ${sent} sent "
                                  "(it rounded up to its minimum bid)")
                await self._save()
                self._emit("bid_placed", {**self._booking_data(aid, mem.placed, reason or "confirmed", s),
                                          "amount": mem.placed, "sent": sent, "raised": raised})
            elif now - mem.pending_since > 45:
                amount, mem.pending = mem.pending, 0
                mem.status = (f"Mac.bid accepted the request for a ${amount} max but no bid shows up. "
                              f"Its answer: {mem.answer}")
                _LOGGER.warning("Mac.bid bid on lot %s not recorded. Answer was: %s", aid, mem.answer)
                if s is not None:
                    self._emit("bid_failed", {**self._booking_data(aid, amount, mem.status, s), "amount": amount})
            else:
                self._lots.pop(aid, None)  # keep re-reading it until confirmed

    async def _run_macbid(self, states: dict[str, AuctionState], now: float) -> None:
        if record_wins(self._mb_wins, states, now):
            await self._save()
        await self._verify_pending(states, now)
        for aid, settings in list(self._mb.items()):
            s = states.get(aid)
            if s is None or settings.mode == MB_MODE_OFF:
                continue
            mem = self._mbmem.setdefault(aid, MacBidMemory())
            if mem.pending:
                continue
            action, amount, reason = decide_macbid(
                s, settings, mem, self.bid_limit(aid), now,
                budget_left=self._macbid_budget_left(states, exclude=aid), paused=self.auto_paused)
            if not action:
                mem.status = reason
                continue
            try:
                await self._place(s, amount, f"auto: {reason}", settings.assurance)
            except AuthError:
                raise
            except SiteError as err:
                mem.status = f"bid failed: {err}"
                _LOGGER.warning("Mac.bid auto-bid on %s failed: %s", aid, err)
                self._emit("bid_failed", {**self._booking_data(aid, amount, str(err), s), "amount": amount})
        # the money tally on the page
        c = macbid_committed(states, {k: m.placed for k, m in self._mbmem.items()}, self._limits,
                             {k: v.mode for k, v in self._mb.items()})
        budget = int(self._opt(self.config_entry, CONF_MACBID_BUDGET, 0) or 0)
        self.spending = {
            "site": "macbid", "committed": c["placed"], "scheduled": c["scheduled"],
            "budget": budget or None, "budget_left": (budget - c["placed"] - c["scheduled"]) if budget else None,
            "won_awaiting_pickup": round(sum(s.price or 0 for s in states.values() if s.status == "awaiting_pickup"), 2),
            "auto_paused": self.auto_paused,
        }
        if (g := global_settings.get(self.hass)) is not None:
            self.spending["cap"] = g.summary(now)
            await g.async_check(now)

    @staticmethod
    def _seconds_left(lot: dict[str, Any], now: float) -> float | None:
        end = mb._dt(lot.get("end_time"))
        return end.timestamp() - now if end else None

    @property
    def opportunities(self) -> list[str]:
        return []

    async def async_book(self, auction_id: str, count: int, max_price: float | None = None) -> int:
        raise SiteError("Booking isn't available for Mac.bid")

    async def async_cancel(self, auction_id: str) -> None:
        raise SiteError("Booking isn't available for Mac.bid")

    async def _async_update_data(self) -> dict[str, AuctionState]:
        e = self.config_entry
        now = time.time()
        client: mb.MacBidClient = self.client  # type: ignore[assignment]
        try:
            if not self._watch_at or now - self._watch_at >= WATCHLIST_REFRESH_SECONDS:
                self._watch = await client.watchlist()
                self._known_lots |= set(await client.active_ids())
                self._watch_at = now
            if not self._locs_at or now - self._locs_at >= 86400:
                try:
                    self._locs = await client.locations()
                    self._locs_at = now
                except SiteError as err:
                    _LOGGER.debug("Mac.bid locations not loaded: %s", err)
            if not self._pickups_at or now - self._pickups_at >= PICKUP_REFRESH_SECONDS:
                self._pickups = await client.pickups(now)
                self._pickups_at = now
            extra = [x.strip() for x in str(self._opt(e, CONF_EXTRA_IDS, "")).replace(",", " ").split() if x.strip().isdigit()]
            tracked = sorted(set(self._watch) | self._known_lots | set(extra))
            # Each lot is checked only as often as it needs (often near its end), spaced out,
            # and a "too many requests" answer just keeps the last known data.
            first = True
            playing = self._in_play()
            for lot_id in tracked:
                cached = self._lots.get(lot_id)
                if cached and now - cached[0] < mb.lot_refresh_seconds(self._seconds_left(cached[1], now), lot_id in playing):
                    continue
                if now < self._backoff_until:
                    break
                if not first:
                    await asyncio.sleep(0.5)
                first = False
                try:
                    self._lots[lot_id] = (now, await client.lot(lot_id))
                except mb.RateLimited:
                    self._backoff_until = now + 60
                    _LOGGER.info("Mac.bid asked to slow down; using last known data for a minute")
                    break
        except AuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SiteError as err:
            raise UpdateFailed(str(err)) from err
        finally:
            # Mac.bid hands out a new refresh token on every renewal: always keep the latest
            if client.auth_changed:
                client.auth_changed = False
                self.hass.config_entries.async_update_entry(e, data={**e.data, CONF_AUTH: client.auth})

        # Item details (condition notes, description): public, once per item, refreshed daily.
        # Items never loaded go first so new cards fill in on the next check; up to 20 per check.
        fetched = 0
        order = sorted(tracked, key=lambda i: (i in self._details, (self._details.get(i) or (0,))[0]))
        for lot_id in order:
            cached = self._details.get(lot_id)
            if (cached is None or now - cached[0] > 86400) and fetched < 20 and now >= self._backoff_until:
                try:
                    if fetched:
                        await asyncio.sleep(0.3)
                    self._details[lot_id] = (now, await client.lot_details(lot_id))
                    fetched += 1
                except mb.RateLimited:
                    self._backoff_until = now + 60
                    break
                except SiteError as err:
                    _LOGGER.info("Mac.bid details (condition notes) for lot %s not loaded: %s", lot_id, err)
        self._details = {k: v for k, v in self._details.items() if k in tracked}

        states: dict[str, AuctionState] = {}
        for lot_id in tracked:
            lot = (self._lots.get(lot_id) or (0, None))[1]
            details = (self._details.get(lot_id) or (0, {}))[1]
            # The watchlist knows most of this already; details fill gaps for lots you only bid on
            static = {**{k: v for k, v in details.items() if v not in (None, "")}, **(self._watch.get(lot_id) or {})}
            st = mb.build_state(lot_id, lot, static, client.user_id, set(self._watch), now)
            st.extra["condition_notes"] = details.get("condition_notes", [])
            st.extra["description"] = details.get("description") or None
            st.extra["model"] = details.get("model")
            if not st.extra.get("condition_explained"):
                st.extra["condition_explained"] = mb.condition_explained(details.get("condition"))
            if "DAMAGED" in str(details.get("condition") or "").upper():
                st.extra["assurance_cost"] = None
            info = mb.location_info(st.extra.get("location_id"), self._locs, st.extra.get("lot_transferable"))
            st.extra.update(info)
            if info.get("sales_tax") is not None:
                st.cost_if_won_now = mb.win_cost(st.price, info["sales_tax"])
            mbs, mem = self.macbid_settings(lot_id), self.macbid_memory(lot_id)
            st.extra.update({"mb_mode": mbs.mode, "mb_late_seconds": mbs.late_seconds, "mb_assurance": mbs.assurance,
                             "mb_placed": mem.placed or None, "mb_status": mem.status})
            if st.ends_at:
                if lot_id not in self._original_end:
                    self._original_end[lot_id] = st.ends_at.timestamp()
                    self._needs_save = True
                first = self._original_end[lot_id]
                if first < st.ends_at.timestamp() - 1:
                    st.original_end = dt_util.utc_from_timestamp(first)
                else:
                    st.original_end = st.ends_at
            # Forget lots you only bid on once they've closed and dropped off your bid history
            if st.status == STATUS_ENDED and lot_id not in self._watch and lot_id in self._known_lots:
                self._known_lots.discard(lot_id)
            states[lot_id] = st
        states.update(self._pickups)
        states = await self._apply_keep_ended(states, now)

        events, self._timed = evaluate_timed(
            states, self.rules, self._timed, now,
            ending_minutes=int(self._opt(e, CONF_ENDING_MINUTES, DEFAULT_ENDING_MINUTES)),
            pickup_hours=int(self._opt(e, CONF_PICKUP_HOURS, DEFAULT_PICKUP_HOURS)),
        )
        for kind, data in events:
            if data.get("ends_at"):
                data["ends_local"] = human_time(dt_util.as_local(dt_util.parse_datetime(data["ends_at"])))
            self._emit(kind, data)
        try:
            await self._run_macbid(states, now)
        except AuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err

        playing = self._in_play(states)
        seconds = choose_timed_interval(
            states, now,
            fast=int(self._opt(e, CONF_FAST_INTERVAL, DEFAULT_FAST_INTERVAL)),
            idle_minutes=int(self._opt(e, CONF_IDLE_INTERVAL, DEFAULT_IDLE_INTERVAL)),
            in_play=playing,
        )
        fast_now = seconds <= int(self._opt(e, CONF_FAST_INTERVAL, DEFAULT_FAST_INTERVAL))
        self.mode = "fast" if fast_now else ("bidding" if playing and seconds <= 120 else
                                             "watching" if seconds <= 15 * 60 else "idle")
        self.update_interval = timedelta(seconds=seconds)
        return states


class EbayCoordinator(AuctionWatcherCoordinator):
    """eBay via the official APIs: your watchlist + bids (if signed in) and any item numbers you add."""

    def dollars_used(self, since: float, exclude: str | None = None) -> float:
        return 0.0  # no bidding on eBay from here, so nothing counts toward the spending cap

    async def async_watch(self, item_id: str, on: bool = True) -> None:
        """Add to (or remove from) your eBay watch list (needs eBay sign-in)."""
        client: eb.EbayClient = self.client  # type: ignore[assignment]
        if not client.signed_in:
            raise SiteError("Sign in to eBay in its settings first (watching needs your eBay account)")
        await client.watch(item_id, on)
        self._mine_at = 0.0
        await self.async_request_refresh()

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: eb.EbayClient) -> None:
        super().__init__(hass, entry, client)  # type: ignore[arg-type]
        self._mine: dict[str, AuctionState] = {}
        self._mine_at = 0.0
        self._items: dict[str, tuple[float, AuctionState]] = {}
        self._timed = TimedSnapshot()
        self._backoff_until = 0.0
        self._ebay_user = entry.data.get(CONF_EBAY_USER) or ""

    @property
    def opportunities(self) -> list[str]:
        return []

    async def async_book(self, auction_id: str, count: int, max_price: float | None = None) -> int:
        raise SiteError("Bidding isn't available for eBay (use eBay's own max bid)")

    async def async_cancel(self, auction_id: str) -> None:
        raise SiteError("Bidding isn't available for eBay")

    def _mine_due(self, now: float) -> bool:
        lefts = [s.ends_at.timestamp() - now for s in self._mine.values() if s.status == STATUS_LIVE and s.ends_at]
        return not self._mine_at or now - self._mine_at >= mb.lot_refresh_seconds(min(lefts) if lefts else None)

    async def _async_update_data(self) -> dict[str, AuctionState]:
        e = self.config_entry
        now = time.time()
        client: eb.EbayClient = self.client  # type: ignore[assignment]
        extra = [i for i in (eb.parse_item_id(x) for x in str(self._opt(e, CONF_EXTRA_IDS, "")).replace(",", " ").split()) if i]
        try:
            if client.signed_in and now >= self._backoff_until and self._mine_due(now):
                if not self._ebay_user:
                    self._ebay_user = await client.my_user_id()
                self._mine = await client.my_ebay(self._ebay_user, now)
                self._mine_at = now
            for item_id in extra:
                if item_id in self._mine or now < self._backoff_until:
                    continue
                cached = self._items.get(item_id)
                left = cached[1].ends_at.timestamp() - now if cached and cached[1].ends_at else None
                if cached and now - cached[0] < mb.lot_refresh_seconds(left):
                    continue
                try:
                    self._items[item_id] = (now, eb.state_from_browse(await client.item(item_id), False, now))
                except eb.RateLimited:
                    raise
                except SiteError as err:
                    _LOGGER.warning("eBay item %s: %s", item_id, err)
        except eb.RateLimited:
            self._backoff_until = now + 120
            _LOGGER.info("eBay asked to slow down; using last known data for 2 minutes")
        except AuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SiteError as err:
            raise UpdateFailed(str(err)) from err
        finally:
            if client.user_auth_changed:
                client.user_auth_changed = False
                self.hass.config_entries.async_update_entry(
                    e, data={**e.data, CONF_EBAY_TOKENS: client.user_auth, CONF_EBAY_USER: self._ebay_user})

        states: dict[str, AuctionState] = dict(self._mine)
        for item_id, (_, st) in self._items.items():
            if item_id not in states and item_id in extra:
                if st.ends_at:
                    st.status = STATUS_LIVE if st.ends_at.timestamp() > now else STATUS_ENDED
                states[item_id] = st
        states = await self._apply_keep_ended(states, now)

        events, self._timed = evaluate_timed(
            states, self.rules, self._timed, now,
            ending_minutes=int(self._opt(e, CONF_ENDING_MINUTES, DEFAULT_ENDING_MINUTES)),
        )
        for kind, data in events:
            if data.get("ends_at"):
                data["ends_local"] = human_time(dt_util.as_local(dt_util.parse_datetime(data["ends_at"])))
            self._emit(kind, data)

        seconds = choose_timed_interval(
            states, now,
            fast=int(self._opt(e, CONF_FAST_INTERVAL, DEFAULT_FAST_INTERVAL)),
            idle_minutes=int(self._opt(e, CONF_IDLE_INTERVAL, DEFAULT_IDLE_INTERVAL)),
        )
        self.mode = "fast" if seconds <= 60 else ("watching" if seconds <= 15 * 60 else "idle")
        self.update_interval = timedelta(seconds=seconds)
        return states
