"""Alert rules and polling speed. Plain Python, no Home Assistant, so it's unit-tested."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .sites.base import STATUS_LIVE, STATUS_UPCOMING, AuctionState


@dataclass
class Rules:
    max_bidders: int = 2
    max_price: float = 0.0       # 0 = any price
    default_bid_limit: int = 0   # 0 = no limit
    limits: dict[str, int] = field(default_factory=dict)  # per-auction overrides

    def bid_limit(self, auction_id: str) -> int:
        return int(self.limits.get(auction_id, self.default_bid_limit) or 0)


@dataclass
class Snapshot:
    """What we'd already alerted about, so each alert fires once."""

    opportunities: set[str] = field(default_factory=set)
    live: set[str] = field(default_factory=set)
    limit_fired: set[tuple[str, int]] = field(default_factory=set)
    won: set[str] | None = None  # None until the first update (don't alert on old wins)


def is_opportunity(s: AuctionState, rules: Rules) -> bool:
    """Down to a few people, under your price, and you're allowed in."""
    if s.status != STATUS_LIVE or s.active_bidders is None:
        return False
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
) -> tuple[list[tuple[str, dict[str, Any]]], Snapshot]:
    """Return (events to fire as (kind, data)), new snapshot)."""
    events: list[tuple[str, dict[str, Any]]] = []

    opps = {aid for aid, s in states.items() if is_opportunity(s, rules)}
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

    return events, Snapshot(opportunities=opps, live=live, limit_fired=limit_fired, won=won)


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
