"""Alert rules and polling speed. Plain Python, no Home Assistant, so it's unit-tested."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .sites.base import STATUS_ENDED, STATUS_LIVE, STATUS_UPCOMING, AuctionState


@dataclass
class Rules:
    max_bidders: int = 2
    max_price: float = 0.0       # 0 = any price
    default_bid_limit: int = 0   # 0 = no limit
    stable_seconds: int = 0      # how long it must stay at <= max_bidders before alerting (0 = right away)
    limits: dict[str, int] = field(default_factory=dict)  # per-auction overrides

    def bid_limit(self, auction_id: str) -> int:
        return int(self.limits.get(auction_id, self.default_bid_limit) or 0)


@dataclass
class Snapshot:
    """What we'd already alerted about, so each alert fires once."""

    opportunities: set[str] = field(default_factory=set)
    candidate_since: dict[str, float] = field(default_factory=dict)
    live: set[str] = field(default_factory=set)
    limit_fired: set[tuple[str, int]] = field(default_factory=set)
    won: set[str] | None = None  # None until the first update (don't alert on old wins)


class BidderTracker:
    """Remembers who bid when, across polls.

    The site only shows the last 9 bids, so a bidder who pauses for a minute
    drops out of that list and would look gone. Counting everyone seen within
    a longer window (default 3 minutes) catches people who jump back in.
    """

    def __init__(self) -> None:
        self._seen: dict[str, dict[str, float]] = {}

    def add(self, auction_id: str, rows: list[tuple[float, str]]) -> None:
        seen = self._seen.setdefault(auction_id, {})
        for ts, user in rows:
            if ts > seen.get(user, 0):
                seen[user] = ts

    KEEP_SECONDS = 3600  # longer than any window the settings allow

    def active(self, auction_id: str, now: float, window_seconds: float) -> list[str]:
        """Bidders seen within the window, most recent first."""
        seen = self._seen.get(auction_id, {})
        for user in [u for u, ts in seen.items() if ts <= now - self.KEEP_SECONDS]:
            del seen[user]
        recent = [(u, ts) for u, ts in seen.items() if ts > now - window_seconds]
        return [u for u, _ in sorted(recent, key=lambda kv: -kv[1])]

    def forget_except(self, auction_ids: set[str]) -> None:
        for aid in [a for a in self._seen if a not in auction_ids]:
            del self._seen[aid]


# Penny auctions run a long first countdown (hours) before the real 5-6 second
# resets begin. "2 bidders" during that first countdown means nothing, so only
# alert once the clock is in the short-reset phase.
FINAL_PHASE_MAX_CLOCK = 15


def is_opportunity(s: AuctionState, rules: Rules) -> bool:
    """Down to a few people, in the real bidding phase, under your price, and you're allowed in."""
    if s.status != STATUS_LIVE or s.active_bidders is None:
        return False
    if s.clock_seconds is not None and s.clock_seconds > FINAL_PHASE_MAX_CLOCK:
        return False  # still in the first long countdown
    if not 1 <= s.active_bidders <= rules.max_bidders:
        return False
    if rules.max_price and (s.price is None or s.price >= rules.max_price):
        return False
    if s.closed_to_new and s.your_bids == 0:
        return False  # locked to existing bidders and you aren't one
    limit = rules.bid_limit(s.auction_id)
    if limit and s.your_bids >= limit:
        return False  # you've hit your own cap; don't tempt yourself
    return True


def evaluate(
    states: dict[str, AuctionState],
    rules: Rules,
    prev: Snapshot,
    won_ids: list[str],
    now: float = 0.0,
) -> tuple[list[tuple[str, dict[str, Any]]], Snapshot]:
    """Return (events to fire as (kind, data)), new snapshot)."""
    events: list[tuple[str, dict[str, Any]]] = []

    # Qualifies now; and has it stayed qualifying for stable_seconds?
    candidates = {aid for aid, s in states.items() if is_opportunity(s, rules)}
    since = {aid: prev.candidate_since.get(aid, now) for aid in candidates}
    opps = {aid for aid in candidates if now - since[aid] >= rules.stable_seconds}
    for aid in sorted(opps - prev.opportunities):
        events.append(("opportunity", _data(states[aid], rules)))

    live = {aid for aid, s in states.items() if s.status == STATUS_LIVE}
    for aid in sorted(prev.live - live):
        if aid in states:
            events.append(("ended", _data(states[aid], rules)))

    limit_fired = {(a, l) for (a, l) in prev.limit_fired if a in states}
    for aid, s in states.items():
        limit = rules.bid_limit(aid)
        if limit and s.your_bids >= limit and (aid, limit) not in limit_fired:
            limit_fired.add((aid, limit))
            events.append(("bid_limit", _data(s, rules)))

    won = set(won_ids)
    if prev.won is not None:
        for aid in sorted(won - prev.won):
            data = _data(states[aid], rules) if aid in states else {"auction_id": aid}
            events.append(("won", data))

    return events, Snapshot(opportunities=opps, candidate_since=since, live=live, limit_fired=limit_fired, won=won)


def choose_interval(states: dict[str, AuctionState], now: float, fast: int, idle_minutes: int, upcoming: int, window: int) -> int:
    """Fast while something you follow is live, medium just before one starts, slow otherwise."""
    if any(s.status == STATUS_LIVE for s in states.values()):
        return fast
    for s in states.values():
        if s.status == STATUS_UPCOMING and s.starts_at and s.starts_at.timestamp() - now <= window:
            return upcoming
    return idle_minutes * 60


def _data(s: AuctionState, rules: Rules) -> dict[str, Any]:
    return {
        "site": s.site,
        "auction_id": s.auction_id,
        "title": s.title,
        "url": s.url,
        "price": s.price,
        "leader": s.leader,
        "active_bidders": s.active_bidders,
        "recent_bidders": s.recent_bidders,
        "your_bids": s.your_bids,
        "your_points_cost": s.your_points_cost,
        "your_booked_left": s.your_booked_left,
        "bid_limit": rules.bid_limit(s.auction_id),
        "buy_it_now": s.buy_it_now,
        "median_final_price": s.history.median_final_price if s.history else None,
        "median_winner_bids": s.history.median_winner_bids if s.history else None,
    }


# ---------------------------------------------------------------- auto booking
#
# Per-auction, opt-in. Spends real points, so every rule errs on the side of
# doing nothing:
#   * nothing happens without a bid limit for that auction
#   * only in the real bidding phase (short clock), never in the first countdown
#   * never auto-cancels where cancelling would lock you out
#     (No Re-Entry auctions, or closed auctions you haven't bid in yet)
#   * never books past the bid limit or your point balance
#   * waits between attempts on the same auction

REBOOK_FIXED = "fixed"
REBOOK_REMAINING = "remaining"
AUTO_RETRY_SECONDS = 30


@dataclass
class AutoSettings:
    auto_cancel: bool = False
    auto_rebook: bool = False
    rebook_mode: str = REBOOK_FIXED      # "fixed" -> rebook_amount; "remaining" -> what was left when cancelled
    rebook_amount: int = 10
    max_price: float = 0.0               # passed to the site's own price cap (0 = none)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "AutoSettings":
        d = d or {}
        mode = d.get("rebook_mode", REBOOK_FIXED)
        return cls(
            auto_cancel=bool(d.get("auto_cancel", False)),
            auto_rebook=bool(d.get("auto_rebook", False)),
            rebook_mode=mode if mode in (REBOOK_FIXED, REBOOK_REMAINING) else REBOOK_FIXED,
            rebook_amount=max(0, int(d.get("rebook_amount", 10) or 0)),
            max_price=max(0.0, float(d.get("max_price", 0) or 0)),
        )

    @property
    def any_auto(self) -> bool:
        return self.auto_cancel or self.auto_rebook


@dataclass
class AutoMemory:
    cancelled_with: int = 0        # booked bids left when we last auto-cancelled
    last_attempt: float = 0.0
    status: str = ""               # shown on the Auctions page


def can_cancel_safely(s: AuctionState) -> tuple[bool, str]:
    labels = " ".join(s.labels).lower()
    if "no re-entry" in labels:
        return False, "No Re-Entry auction: cancelling could lock you out"
    if s.closed_to_new and s.your_bids == 0:
        return False, "closed to new bidders and you haven't bid yet"
    return True, ""


def decide_auto(
    s: AuctionState,
    settings: AutoSettings,
    mem: AutoMemory,
    rules: Rules,
    points: int | None,
    now: float,
    budget_left: int | None = None,
    paused: bool = False,
) -> tuple[str, int, str]:
    """Return (action, count, reason). action is "", "cancel" or "book".

    points      = what's free to spend right now (site balance minus what other
                  auctions already took this round)
    budget_left = what's left of the weekly budget (None = no budget)
    paused      = the auto-booking switch is off: cancelling still works, booking doesn't
    """
    if not settings.any_auto:
        return "", 0, ""
    limit = rules.bid_limit(s.auction_id)
    if not limit:
        return "", 0, "auto is off until you set a bid limit for this auction"
    if s.status != STATUS_LIVE or s.active_bidders is None:
        return "", 0, "waiting for the auction to go live"
    if s.clock_seconds is not None and s.clock_seconds > FINAL_PHASE_MAX_CLOCK:
        return "", 0, "waiting for the first countdown to end"
    if now - mem.last_attempt < AUTO_RETRY_SECONDS:
        return "", 0, mem.status

    crowded = s.active_bidders > rules.max_bidders
    if crowded:
        if settings.auto_cancel and s.your_booked_left > 0:
            ok, why = can_cancel_safely(s)
            if not ok:
                return "", 0, f"not cancelling: {why}"
            return "cancel", s.your_booked_left, f"{s.active_bidders} bidders > {rules.max_bidders}"
        return "", 0, f"crowded ({s.active_bidders} bidders), holding off"

    if settings.auto_rebook and s.your_booked_left == 0 and s.active_bidders >= 1:
        if paused:
            return "", 0, "auto-booking is switched off"
        if budget_left is not None and budget_left <= 0:
            return "", 0, "weekly budget used up"
        if s.closed_to_new and s.your_bids == 0:
            return "", 0, "closed to new bidders and you haven't bid yet"
        want = mem.cancelled_with if settings.rebook_mode == REBOOK_REMAINING and mem.cancelled_with else settings.rebook_amount
        room = limit - s.your_bids
        if room <= 0:
            return "", 0, "bid limit reached"
        count = min(want, room, points if points is not None else want,
                    budget_left if budget_left is not None else want)
        if count < 1:
            return "", 0, "no points left to book"
        return "book", count, f"down to {s.active_bidders} bidder{'' if s.active_bidders == 1 else 's'}"
    return "", 0, mem.status or "watching"


# ---------------------------------------------------------------- timed auctions (Mac.bid)
#
# Fixed end times (pushed back by late bids), pay what you bid. Alerts:
#   outbid       you were winning, now someone else is
#   ending_soon  N minutes before the end (once per auction)
#   over_max     the price passed your max bid (once per max)
#   won / ended  when it closes
#   pickup       N hours before a won item's pickup deadline (once)

@dataclass
class TimedSnapshot:
    winning: set[str] = field(default_factory=set)
    live: set[str] = field(default_factory=set)
    ending_fired: set[str] = field(default_factory=set)
    over_fired: set[tuple[str, int]] = field(default_factory=set)
    pickup_fired: set[str] = field(default_factory=set)
    first: bool = True  # don't alert about things that were already true at startup


def evaluate_timed(
    states: dict[str, AuctionState],
    rules: Rules,
    prev: TimedSnapshot,
    now: float,
    ending_minutes: int = 10,
    pickup_hours: int = 24,
) -> tuple[list[tuple[str, dict[str, Any]]], TimedSnapshot]:
    events: list[tuple[str, dict[str, Any]]] = []
    snap = TimedSnapshot(ending_fired=set(prev.ending_fired), over_fired=set(prev.over_fired),
                         pickup_fired=set(prev.pickup_fired), first=False)
    for aid, s in states.items():
        winning = bool(s.extra.get("winning"))
        left = s.ends_at.timestamp() - now if s.ends_at else None
        d = {**_data(s, rules), "minutes_left": round(left / 60) if left is not None else None,
             "ends_at": s.ends_at.isoformat() if s.ends_at else None, "winning": winning}

        if s.status == "awaiting_pickup":
            if left is not None and left <= pickup_hours * 3600 and aid not in prev.pickup_fired:
                snap.pickup_fired.add(aid)
                events.append(("pickup", d))
            continue

        if s.status == STATUS_LIVE:
            snap.live.add(aid)
            if winning:
                snap.winning.add(aid)
            elif aid in prev.winning and not prev.first:
                events.append(("outbid", d))
            if ending_minutes and left is not None and 0 < left <= ending_minutes * 60 and aid not in prev.ending_fired:
                snap.ending_fired.add(aid)
                events.append(("ending_soon", d))
            limit = rules.bid_limit(aid)  # on Mac.bid: your max bid in dollars
            if limit and s.price is not None and s.price > limit and not winning and (aid, limit) not in prev.over_fired:
                snap.over_fired.add((aid, limit))
                events.append(("over_max", d))
        elif aid in prev.live and s.status == STATUS_ENDED:
            events.append(("won" if winning or aid in prev.winning else "ended", d))
    return events, snap


def choose_timed_interval(states: dict[str, AuctionState], now: float, fast: int, idle_minutes: int) -> int:
    """Poll fast only near an end time; Mac.bid auctions run for days."""
    lefts = [s.ends_at.timestamp() - now for s in states.values() if s.status == STATUS_LIVE and s.ends_at]
    soonest = min(lefts) if lefts else None
    if soonest is None:
        return idle_minutes * 60
    if soonest <= 15 * 60:
        return fast
    if soonest <= 2 * 3600:
        return 120
    return min(idle_minutes * 60, 15 * 60)


# ---------------------------------------------------------------- keeping ended auctions

KEEP_STATUSES = (STATUS_LIVE, STATUS_UPCOMING, STATUS_ENDED)


def keep_ended(
    states: dict[str, AuctionState],
    last_seen: dict[str, AuctionState],
    ended: dict[str, tuple[float, AuctionState]],
    now: float,
    keep_days: float,
) -> tuple[dict[str, AuctionState], dict[str, AuctionState], dict[str, tuple[float, AuctionState]], list[str]]:
    """Keep ended auctions on the page for keep_days.

    Sites drop auctions from their lists soon after they end (or when you
    un-watch them). Anything that was live and disappears is kept as ended,
    marked "no longer followed". Returns (states to show, new last_seen,
    new ended, auction ids whose time is up and whose sensors can go).
    """
    out = dict(states)
    ended = dict(ended)
    for aid, s in states.items():
        if s.status == STATUS_ENDED:
            ended[aid] = (ended.get(aid, (now, s))[0], s)
    for aid, prev in last_seen.items():
        if aid not in states and prev.status in KEEP_STATUSES and aid not in ended:
            gone = AuctionState(**{**prev.__dict__})
            if gone.status != STATUS_ENDED:
                gone.status = STATUS_ENDED
                gone.extra = {**gone.extra, "dropped": True}
            ended[aid] = (now, gone)
    expired = [aid for aid, (t, _) in ended.items() if now - t > keep_days * 86400]
    for aid in expired:
        del ended[aid]
    for aid, (t, s) in ended.items():
        if aid not in out:
            out[aid] = s
        out[aid].extra = {**out[aid].extra, "ended_at": t, "remove_after": t + keep_days * 86400}
    new_last = {aid: s for aid, s in states.items() if s.status in KEEP_STATUSES}
    return out, new_last, ended, expired


# ---------------------------------------------------------------- spending and points at risk

def week_start(local_now: Any) -> float:
    """Monday 00:00 of the current week (local time, a datetime), as a timestamp."""
    from datetime import timedelta
    monday = (local_now - timedelta(days=local_now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return monday.timestamp()


def record_spending(ledger: list[list[float]], last_bids: dict[str, int], states: dict[str, AuctionState],
                    now: float) -> bool:
    """Add newly placed bids (your_bids going up) to the ledger as [time, bids, auction]. Returns True if changed."""
    changed = False
    for aid, s in states.items():
        if s.status == "awaiting_pickup":
            continue
        prev = last_bids.get(aid)
        if prev is not None and s.your_bids > prev:
            ledger.append([now, s.your_bids - prev, aid])
            changed = True
        last_bids[aid] = s.your_bids
    cutoff = now - 35 * 86400
    if ledger and ledger[0][0] < cutoff:
        ledger[:] = [row for row in ledger if row[0] >= cutoff]
        changed = True
    return changed


def spent_since(ledger: list[list[float]], since: float) -> int:
    return int(sum(row[1] for row in ledger if row[0] >= since))


def points_at_risk(states: dict[str, AuctionState], auto: dict[str, "AutoSettings"], rules: Rules) -> dict[str, Any]:
    """booked = held by the site in your booked bids (already out of your balance);
    earmarked = what auto-rebook could still book under each auction's max bids."""
    booked = earmarked = 0
    per: dict[str, int] = {}
    for aid, s in states.items():
        if s.status != STATUS_LIVE and s.status != STATUS_UPCOMING:
            continue
        booked += s.your_booked_left
        settings = auto.get(aid)
        limit = rules.bid_limit(aid)
        if settings and settings.auto_rebook and limit:
            room = max(0, limit - s.your_bids - s.your_booked_left)
            earmarked += room
            per[aid] = room
    return {"booked": booked, "earmarked": earmarked, "earmarked_by_auction": per}


# ---------------------------------------------------------------- Mac.bid max bids
#
# On Mac.bid a bid is a MAXIMUM: the site raises your bid for you as others bid,
# up to that amount. So "auto raise" = placing your max, either right away or
# late (a few minutes before the end, so you don't start a bidding war early).
# Every rule here errs on the side of not bidding.

MB_MODE_OFF = "off"
MB_MODE_NOW = "now"
MB_MODE_LATE = "late"
MB_RETRY_SECONDS = 30


@dataclass
class MacBidSettings:
    mode: str = MB_MODE_OFF
    late_seconds: int = 180  # "late": place the max this long before the end

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "MacBidSettings":
        d = d or {}
        mode = d.get("mode", MB_MODE_OFF)
        return cls(mode=mode if mode in (MB_MODE_OFF, MB_MODE_NOW, MB_MODE_LATE) else MB_MODE_OFF,
                   late_seconds=max(30, int(d.get("late_seconds", 180) or 180)))


@dataclass
class MacBidMemory:
    placed: int = 0           # the max we last placed on this lot
    last_attempt: float = 0.0
    status: str = ""


def decide_macbid(
    s: AuctionState,
    settings: MacBidSettings,
    mem: MacBidMemory,
    max_bid: int,
    now: float,
    budget_left: int | None = None,
    paused: bool = False,
) -> tuple[str, int, str]:
    """Return ("place", amount, reason) or ("", 0, reason)."""
    if settings.mode == MB_MODE_OFF:
        return "", 0, ""
    if not max_bid:
        return "", 0, "set a max bid first"
    if s.status != STATUS_LIVE:
        return "", 0, "not live"
    if s.extra.get("winning"):
        return "", 0, f"you're winning (your max ${max(mem.placed, max_bid)})"
    if s.price is not None and s.price >= max_bid:
        return "", 0, f"price ${s.price:.0f} is at or past your max ${max_bid}"
    if mem.placed >= max_bid:
        return "", 0, f"outbid: someone's max is above your ${mem.placed}"
    left = s.ends_at.timestamp() - now if s.ends_at else None
    if settings.mode == MB_MODE_LATE and (left is None or left > settings.late_seconds):
        mins = settings.late_seconds // 60
        return "", 0, f"will bid ${max_bid} about {mins} min before the end"
    if paused:
        return "", 0, "auto-bidding is switched off"
    if budget_left is not None and max_bid > budget_left:
        return "", 0, f"a ${max_bid} max would go past your Mac.bid budget (${max(budget_left, 0)} left)"
    if now - mem.last_attempt < MB_RETRY_SECONDS:
        return "", 0, mem.status
    return "place", int(max_bid), "late bid" if settings.mode == MB_MODE_LATE else "max bid"


def macbid_committed(states: dict[str, AuctionState], placed: dict[str, int], limits: dict[str, int],
                     modes: dict[str, str]) -> dict[str, int]:
    """Dollars tied up: maxes already placed on live lots you're winning or still in,
    plus maxes scheduled as late bids."""
    placed_total = scheduled = 0
    for aid, s in states.items():
        if s.status != STATUS_LIVE:
            continue
        p = placed.get(aid, 0)
        if p and (s.extra.get("winning") or (s.price or 0) < p):
            placed_total += p
        elif modes.get(aid) == MB_MODE_LATE and limits.get(aid):
            scheduled += limits[aid]
    return {"placed": placed_total, "scheduled": scheduled}
