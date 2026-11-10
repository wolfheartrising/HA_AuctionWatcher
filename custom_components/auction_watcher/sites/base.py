"""Site-independent data model and errors.

Nothing in sites/ imports Home Assistant, so the site clients can be tested
and run on their own (see tools/live_check.py).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


class AuthError(Exception):
    """Login cookie missing, expired or rejected."""


class SiteError(Exception):
    """The site answered with something we couldn't use."""


CATEGORY_BIDDING = "bidding"
CATEGORY_WATCHING = "watching"

STATUS_LIVE = "live"
STATUS_UPCOMING = "upcoming"
STATUS_ENDED = "ended"
STATUS_PICKUP = "awaiting_pickup"  # won, waiting for you to collect it (Mac.bid)


@dataclass
class PriceHistory:
    """Past wins of the same product (GovAuctions: "won for as low as")."""

    wins: int
    period_days: int
    avg_final_price: float
    median_final_price: float
    low_final_price: float
    high_final_price: float
    avg_winner_bids: float
    median_winner_bids: float
    avg_winner_cost: float | None  # site's own estimate, if given


@dataclass
class AuctionState:
    """One auction, as every site handler reports it."""

    site: str
    auction_id: str
    title: str
    url: str
    category: str  # CATEGORY_*
    status: str  # STATUS_*
    price: float | None = None
    currency: str = "USD"
    image: str | None = None
    leader: str | None = None
    # Penny auctions: seconds on the reset clock at the last poll.
    clock_seconds: int | None = None
    # Timed auctions (eBay, GovDeals, ...): fixed end time.
    ends_at: datetime | None = None
    starts_at: datetime | None = None
    # When it was originally due to end; after that it's in overtime
    # (penny auctions: end of the first countdown; timed auctions: first end time seen)
    original_end: datetime | None = None
    active_bidders: int | None = None
    recent_bidders: list[str] = field(default_factory=list)
    closed_to_new: bool | None = None
    your_bids: int = 0
    your_booked_left: int = 0
    your_booked_total: int = 0
    your_points_cost: float = 0.0
    # What winning right now would cost: price you'd pay, and that plus your bids
    pay_if_won: float | None = None
    cost_if_won_now: float | None = None
    buy_it_now: float | None = None
    history: PriceHistory | None = None
    labels: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def as_attributes(self) -> dict[str, Any]:
        """Flat dict for Home Assistant state attributes."""
        data = asdict(self)
        hist = data.pop("history")
        for key in ("ends_at", "starts_at", "original_end"):
            if data[key] is not None:
                data[key] = data[key].isoformat()
        if hist:
            for key, value in hist.items():
                data[f"history_{key}"] = value
        extra = data.pop("extra")
        # Core fields always win: an extra with the same name (e.g. a site's own
        # "auction_id") must never overwrite the item's id the page and services use.
        for key, value in extra.items():
            data.setdefault(key, value)
        return data


def state_to_dict(s: AuctionState) -> dict[str, Any]:
    """For saving to disk. Price history is left out (it's re-fetched for live auctions)."""
    d = asdict(s)
    d["history"] = None
    for key in ("ends_at", "starts_at", "original_end"):
        if d.get(key) is not None:
            d[key] = d[key].isoformat()
    return d


def state_from_dict(d: dict[str, Any]) -> AuctionState:
    d = dict(d)
    for key in ("ends_at", "starts_at", "original_end"):
        if d.get(key):
            d[key] = datetime.fromisoformat(d[key])
    d["history"] = None
    known = AuctionState.__dataclass_fields__
    return AuctionState(**{k: v for k, v in d.items() if k in known})
