"""Mac.bid (timed liquidation auctions: you pay what you bid, plus fees).

Endpoints, all found in Mac.bid's own web app (api.macdiscount.com):
  POST /token                                  -> log in with email + password
  PUT  /auth/refresh-token                     -> trade the refresh token for a new 30-minute key
                                                  (a NEW refresh token comes back every time)
  GET  /auctions/customer/{id}/active-auctions -> your watchlist
  GET  /user/{id}/active                       -> items you're active in
  GET  /map-bid/ddb/lot/{lot_id}               -> live: current bid, end time, extension window,
                                                  who's winning, bidders, watchers

The 30-minute key goes in the Authorization header as-is (no "Bearer").
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import aiohttp

from .base import (
    CATEGORY_BIDDING,
    CATEGORY_WATCHING,
    STATUS_ENDED,
    STATUS_LIVE,
    STATUS_PICKUP,
    STATUS_UPCOMING,
    AuctionState,
    AuthError,
    SiteError,
)

SITE = "macbid"
API = "https://api.macdiscount.com"
WEB = "https://www.mac.bid"
# No User-Agent of our own: requests carry Home Assistant's standard web client identity
# (HomeAssistant/<version> aiohttp/<version> Python/<version>). Never a browser's.
RENEW_EARLY_SECONDS = 120  # renew the 30-minute key this long before it expires

# Mac.bid's search box queries a separate search server with this public, search-only key
# (it's in the mac.bid web page for every visitor). Used once a day per saved search, with
# the owner's approval, exactly like typing in the search box.
SEARCH_URL = "https://search.mac.bid/multi_search"
SEARCH_KEY = "jxX8RU6YVOkm9esgd9buaYjulIWv6N52"
SEARCH_COLLECTION = "prod_macdiscount_alias"


class RateLimited(SiteError):
    """Mac.bid answered 429: slow down."""


class CodeRequired(AuthError):
    """Mac.bid sent a verification code (new device) that has to be typed in."""

    def __init__(self, where: str, last4: str = "", new_password: bool = False) -> None:
        self.where = where
        self.last4 = last4
        self.new_password = new_password
        to = f"your {where}" + (f" ending {last4}" if last4 else "")
        super().__init__(f"Mac.bid sent a verification code to {to}")


IN_PLAY_SECONDS = 120         # items you're bidding on: every 2 minutes (outbid alerts arrive quickly)
IN_PLAY_LATE_SECONDS = 60     # ...and every minute in their last 2 hours


def lot_refresh_seconds(seconds_left: float | None, in_play: bool = False) -> int:
    """How often one lot needs checking: items you're bidding on often; watched items often only near their end."""
    if seconds_left is not None and seconds_left <= 15 * 60:
        return 0 if in_play else 60   # yours: every check; only watching: once a minute (lots close in batches)
    if in_play:
        return IN_PLAY_LATE_SECONDS if seconds_left is not None and seconds_left <= 2 * 3600 else IN_PLAY_SECONDS
    if seconds_left is None:
        return 900
    if seconds_left <= 2 * 3600:
        return 120
    return 900


def new_device_id() -> str:
    return str(uuid.uuid4())


def parse_auth(text_or_dict: Any) -> dict[str, Any]:
    """Accept the browser's mac_auths value (JSON text) or a dict; keep only what's needed."""
    import json
    d = text_or_dict
    if isinstance(d, str):
        d = json.loads(d.strip())
        if isinstance(d, str):  # some browsers copy it double-encoded
            d = json.loads(d)
    if not isinstance(d, dict):
        raise SiteError("That doesn't look like Mac.bid's saved login (mac_auths)")
    out = {
        "access_token": d.get("access_token") or d.get("token"),
        "refresh_token": d.get("refresh_token"),
        "expires": _num(d.get("expires")),
        "expiration_refresh": _num(d.get("expiration_refresh")),
        "user_id": str(d.get("user_id") or ""),
    }
    if not out["refresh_token"] or not out["user_id"]:
        raise SiteError("Mac.bid's saved login is missing its refresh token or user id")
    return out


# ---------------------------------------------------------------- parsing

def parse_watchlist(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """{lot_id: static info} from active-auctions' watchlist_full."""
    out: dict[str, dict[str, Any]] = {}
    for w in (data or {}).get("watchlist_full") or []:
        lot_id = str(w.get("id") or w.get("auction_lot_id") or "")
        if not lot_id:
            continue
        out[lot_id] = {
            "title": w.get("title") or w.get("product_name") or f"Mac.bid lot {lot_id}",
            "image": w.get("image_url"),
            "retail_price": _num(w.get("retail_price")),
            "condition": w.get("condition_name"),
            "category_name": w.get("category"),
            "lot_number": w.get("lot_number"),
            "auction_number": w.get("auction_number"),
            "buyers_assurance_cost": _num(w.get("buyers_assurance_cost")),
            "location_id": w.get("current_location_id"),
            "auction_id": w.get("auction_id"),
            "is_transferrable": bool(w.get("is_transferrable")) if w.get("is_transferrable") is not None else None,
            "auction_type": w.get("auction_type"),
        }
    return out


def parse_active_ids(data: Any) -> list[str]:
    """Lot ids from /user/{id}/active or /user/{id}/bid-history (read defensively:
    both were empty when this was written, so their item shape is a best guess)."""
    ids: list[str] = []
    items = data if isinstance(data, list) else (data or {}).get("data") or (data or {}).get("items") or []
    for item in items:
        if isinstance(item, dict):
            lot = item.get("lot_id") or item.get("auction_lot_id") or item.get("id")
            if lot:
                ids.append(str(lot))
        elif isinstance(item, (int, str)) and str(item).isdigit():
            ids.append(str(item))
    return ids


# Mac.bid's own defaults when an auction doesn't override them
BUYERS_PREMIUM = 0.15
LOT_FEE = 3.0


def win_cost(bid: float | None, tax: float | None = None) -> float | None:
    """What winning at `bid` costs: bid + 15% buyer's premium + $3 lot fee, plus sales tax if known."""
    if bid is None:
        return None
    return round((bid * (1 + BUYERS_PREMIUM) + LOT_FEE) * (1 + (tax or 0)), 2)


def parse_locations(locations: list[dict[str, Any]], buildings: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Mac.bid pickup locations (public /locations + /buildings): name, address, hours,
    whether items there can be transferred, where to, and the building's sales tax."""
    tax = {str(b.get("id")): _num(b.get("sales_tax")) for b in buildings or []}
    outpost = {str(b.get("id")): str(b.get("is_pickup_outpost") or "0") not in ("0", "", "False", "false")
               for b in buildings or []}
    out: dict[str, dict[str, Any]] = {}
    for loc in locations or []:
        lid = str(loc.get("id") or "")
        if not lid:
            continue
        dests = [d.strip() for d in str(loc.get("transfer_destinations") or "").split(",") if d.strip()]
        out[lid] = {
            "name": loc.get("name"),
            "city_state": loc.get("city_state"),
            "address": loc.get("address"),
            "hours": _strip_html(loc.get("hours") or ""),
            "can_transfer": bool(loc.get("can_transfer")),
            "transfer_destinations": dests,
            "sales_tax": tax.get(str(loc.get("building_id"))),
            "outpost": outpost.get(str(loc.get("building_id")), False),
        }
    return out


def location_info(loc_id: Any, locations: dict[str, dict[str, Any]], lot_transferable: bool | None) -> dict[str, Any]:
    """What the card shows: pickup location, and where it can be transferred (names)."""
    loc = locations.get(str(loc_id or "")) or {}
    can = bool(loc.get("can_transfer")) and lot_transferable is not False
    return {
        "location_id": str(loc_id or "") or None,
        "location": loc.get("name"),
        "location_city": loc.get("city_state"),
        "location_address": loc.get("address"),
        "location_hours": loc.get("hours"),
        "transferable": can,
        "transfer_to": sorted(
            f"{locations[d]['name']} ({locations[d]['city_state']})"
            + (" · free (pickup outpost)" if locations[d].get("outpost") else " · $10")
            for d in loc.get("transfer_destinations", []) if d in locations) if can else [],
        "sales_tax": loc.get("sales_tax"),
    }


def _strip_html(text: str) -> str:
    import re
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def parse_pickups(data: Any, now: float) -> dict[str, AuctionState]:
    """Won items from /user/{id}/active. Fields as in Mac.bid's own code:
    product_name, lot_id, lot_number, price, item_price, cover_image, status,
    invoice_id, date_paid, auction_abandon_date (the pickup deadline)."""
    items = data if isinstance(data, list) else (data or {}).get("data") or []
    out: dict[str, AuctionState] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        lot_id = str(it.get("lot_id") or it.get("id") or "")
        if not lot_id:
            continue
        deadline = _dt(it.get("auction_abandon_date") or it.get("auction_lot_deadline") or it.get("pickup_deadline"))
        out["pickup-" + lot_id] = AuctionState(
            site=SITE,
            auction_id="pickup-" + lot_id,
            title=it.get("product_name") or f"Mac.bid lot {lot_id}",
            url=f"{WEB}/account/awaiting-pickup",
            category=CATEGORY_BIDDING,
            status=STATUS_PICKUP,
            price=_num(it.get("price")),
            image=it.get("cover_image"),
            ends_at=deadline,
            leader="you",
            labels=[x for x in [it.get("status")] if x],
            extra={
                "pickup_deadline": deadline.isoformat() if deadline else None,
                "paid": bool(it.get("date_paid")),
                "invoice_id": it.get("invoice_id"),
                "lot_number": it.get("lot_number"),
                "item_status": it.get("status"),
                "overdue": bool(deadline and deadline.timestamp() < now),
            },
        )
    return out


BOILERPLATE = ("We make no guarantees", "All items are sold")

# Mac.bid's FAQ, shortened: what each condition label means
CONDITION_NOTES = {
    "LIKE NEW": "Appears new or unused; not tested. Can't be inspected until pickup: check it there and report problems before leaving.",
    "OPEN BOX": "Returned and maybe used; may be scratched, incomplete or not working unless the notes say otherwise. Sold as-is.",
    "DAMAGED": "May be incomplete and isn't tested. Sold as-is, no refunds, and Buyer's Assurance isn't available.",
}


def condition_explained(condition: str | None) -> str:
    return CONDITION_NOTES.get(str(condition or "").strip().upper(), "")


def parse_details(d: dict[str, Any]) -> dict[str, Any]:
    """From the public /lot/{id}: the colored condition notes, the description, and a
    few facts the watchlist doesn't always carry."""
    notes = []
    tested_note = (d.get("tested_note") or "").strip()
    if str(d.get("is_tested") or "0") not in ("0", "", "False", "false") or tested_note:
        notes.append({"kind": "tested", "label": "TESTED", "note": tested_note})
    if (d.get("damaged_note") or "").strip():
        notes.append({"kind": "damaged", "label": "DAMAGED", "note": d["damaged_note"].strip()})
    if (d.get("partial_note") or "").strip():
        notes.append({"kind": "partial", "label": "PARTIAL", "note": d["partial_note"].strip()})
    desc = "\n".join(line for line in (d.get("description") or "").splitlines()
                     if line.strip() and not line.strip().startswith(BOILERPLATE)).strip()
    tr = d.get("is_transferrable")
    return {
        "condition_notes": notes,
        "description": desc,
        "condition": d.get("condition_name"),
        "model": d.get("model") or None,
        "title": d.get("title") or d.get("product_name"),
        "image": d.get("image_url") or d.get("stock_image_url"),
        "retail_price": _num(d.get("retail_price") or d.get("product_retail_price")),
        "location_id": str(d.get("current_location_id") or "") or None,
        "is_transferrable": None if tr is None else str(tr) not in ("0", "False", "false", ""),
        "auction_id": d.get("auction_id"),
        "lot_number": d.get("lot_number"),
        "buyers_assurance_cost": _num(d.get("buyers_assurance_cost")),
        "auction_type": d.get("auction_type"),
    }


# The amounts Mac.bid's own "Set your max bid" dropdown offers (from its item page code):
# every $1 up to $100, $5 to $500, $10 to $1,000, $50 to $50,000; only amounts above the current bid.
BID_STEPS = ((1, 100, 1), (105, 500, 5), (510, 1000, 10), (1050, 50000, 50))


def lowering_to_price(amount: float, now: dict[str, Any]) -> bool:
    """Mac.bid lets the leader lower their max to the current price (its FAQ; how you "pull back" a max
    while winning). That amount needn't be one of the dropdown steps."""
    return bool(now.get("winning")) and int(amount) == int(now.get("price") or 0) and int(amount) > 0


def is_valid_bid(amount: int) -> bool:
    return any(lo <= amount <= hi and (amount - lo) % step == 0 for lo, hi, step in BID_STEPS)


def round_down_bid(amount: float) -> int:
    """The largest amount the dropdown offers that's <= amount (never rounds up)."""
    a = int(amount)
    for lo, hi, step in reversed(BID_STEPS):
        if a >= lo:
            return min(hi, lo + (a - lo) // step * step)
    return 0


def next_valid_bid(current: float | None) -> int:
    """The first amount the dropdown offers above the current bid."""
    c = float(current or 0)
    for lo, hi, step in BID_STEPS:
        if c < hi:
            first = lo if c < lo else lo + (int(c - lo) // step + 1) * step
            if first <= hi:
                return first
    return BID_STEPS[-1][1]


def valid_bids(current: float | None, count: int = 40) -> list[int]:
    """The next `count` amounts the dropdown offers above the current bid."""
    out, a = [], next_valid_bid(current)
    while len(out) < count and a <= BID_STEPS[-1][1]:
        out.append(a)
        a = next_valid_bid(a)
        if out and a == out[-1]:
            break
    return out


NEXT_BID_KEYS = ("next_bid", "next_min_bid", "minimum_next_bid", "min_next_bid", "min_bid", "minimum_bid", "next_bid_amount")
INCREMENT_KEYS = ("bid_increment", "increment", "next_increment", "bid_delta")


def lot_now(lot: dict[str, Any], my_id: str) -> dict[str, Any]:
    """Mac.bid's live item, read right before a bid: current price, the smallest bid it will accept
    (Mac.bid's raise steps grow with the price, e.g. $5 above $100; if its data gives the next minimum
    or the step, that's used, else current + $1), whether you're winning, and your max as it has it."""
    price = _num(lot.get("current_bid")) or 0.0
    nxt = next((_num(lot.get(k)) for k in NEXT_BID_KEYS if _num(lot.get(k))), None)
    if nxt is None:
        step = next((_num(lot.get(k)) for k in INCREMENT_KEYS if _num(lot.get(k))), None)
        nxt = price + step if step else None
    return {
        "price": price,
        "min_next": int(round(nxt)) if nxt is not None else next_valid_bid(price),
        "min_next_known": True,
        "winning": bool(my_id) and str(lot.get("winning_bidder_id") or "") == str(my_id),
        "your_max": _num(lot.get("max_bid")) or _num(lot.get("user_max_bid")),
    }


def search_hits(data: dict[str, Any], buildings: list[str] | None = None) -> list[dict[str, Any]]:
    """Search-server results -> plain dicts (turned into Hit objects by the search manager).
    With your buildings given, items sitting elsewhere but transferable to you say where from."""
    mine = set(buildings or [])
    out = []
    for res in (data or {}).get("results") or []:
        for h in res.get("hits") or []:
            d = h.get("document") or {}
            lot_id = str(d.get("lot_id") or d.get("id") or "")
            if not lot_id:
                continue
            out.append({
                "item_id": lot_id,
                "title": d.get("product_name") or d.get("auction_title") or f"Mac.bid lot {lot_id}",
                "url": item_url(d.get("auction_id"), d.get("lot_number")),
                "price": _num(d.get("current_bid")),
                "ends_at": _closing_iso(d.get("expected_closing_utc") or d.get("expected_close_date")),
                "location": d.get("auction_location"),
                "image": d.get("image_url"),
                "condition": d.get("condition"),
                "bids": _int(d.get("total_bids")),
                "transfer": (d.get("auction_location") if mine and d.get("auction_location") not in mine else None),
            })
    return out


def search_total(data: dict[str, Any]) -> int:
    return int((((data or {}).get("results") or [{}])[0]).get("found") or 0)


def keywords_from_profile(data: Any) -> list[str]:
    """Your keyword alerts live on your profile as one comma-separated text ("greenhouse,shed")."""
    user = data.get("user") if isinstance(data, dict) and isinstance(data.get("user"), dict) else data
    raw = (user or {}).get("keywords") if isinstance(user, dict) else None
    return [k.strip() for k in str(raw or "").split(",") if k.strip()]


def keywords_without(current: list[str], keyword: str) -> list[str]:
    k = keyword.strip().lower()
    return [x for x in current if x.strip().lower() != k]


def keywords_with(current: list[str], keyword: str) -> list[str]:
    return current if any(x.strip().lower() == keyword.strip().lower() for x in current) else [*current, keyword.strip()]


def _closing_iso(v: Any) -> str | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000 if v > 1e12 else v, timezone.utc).isoformat()
    d = _dt(v)
    return d.isoformat() if d else str(v)


def item_url(auction_id: Any, lot_number: Any) -> str:
    """The item's page on mac.bid (the same link the site uses: ?aid=<auction>&lid=<lot number>)."""
    if auction_id and lot_number:
        return f"{WEB}/?aid={auction_id}&lid={lot_number}"
    return f"{WEB}/account/watchlist"


def short_answer(data: Any, limit: int = 160) -> str:
    """Mac.bid's reply to a bid, shortened for the card and the log (it holds no secrets)."""
    import json
    text = json.dumps(data, separators=(",", ":"), default=str) if not isinstance(data, str) else data
    return text if len(text) <= limit else text[:limit] + "…"


def build_state(lot_id: str, lot: dict[str, Any] | None, static: dict[str, Any] | None,
                my_id: str, watching_ids: set[str], now: float) -> AuctionState:
    lot = lot or {}
    static = static or {}
    bidders = [str(b) for b in lot.get("unique_bidders") or []]
    you_in = my_id in bidders
    winner = str(lot.get("winning_bidder_id") or "")
    winning = bool(winner) and winner == my_id
    ends_at = _dt(lot.get("end_time"))
    ext = _dt(lot.get("extension_window"))
    is_open = bool(lot.get("is_open")) and str(lot.get("status") or "LIVE").upper() == "LIVE"
    launched = _dt(lot.get("date_launched"))
    if is_open and launched and launched.timestamp() > now:
        status = STATUS_UPCOMING
    elif is_open and (not ends_at or ends_at.timestamp() > now - 5):
        status = STATUS_LIVE
    else:
        status = STATUS_ENDED
    title = static.get("title") or lot.get("title") or f"Mac.bid lot {lot_id}"
    others = [b for b in bidders if b != my_id]
    return AuctionState(
        site=SITE,
        auction_id=lot_id,
        title=title,
        url=item_url(lot.get("auction_id") or static.get("auction_id"), lot.get("lot_number") or static.get("lot_number")),
        category=CATEGORY_BIDDING if you_in else CATEGORY_WATCHING,
        status=status,
        price=_num(lot.get("current_bid")),
        image=static.get("image") or lot.get("stock_image_url"),
        leader="you" if winning else ("someone else" if winner else None),
        ends_at=ends_at,
        starts_at=launched if status == STATUS_UPCOMING else None,
        active_bidders=len(others),
        recent_bidders=[],  # Mac.bid only gives bidder numbers, not names
        your_bids=0,
        pay_if_won=_num(lot.get("current_bid")),
        cost_if_won_now=win_cost(_num(lot.get("current_bid"))),
        buy_it_now=_num(lot.get("instant_win_price")),
        labels=[x for x in [static.get("condition")] if x],
        extra={
            "winning": winning,
            "you_are_bidding": you_in,
            "total_bids": _int(lot.get("total_bids")),
            "watchers": _int(lot.get("watchers_count")),
            "retail_price": static.get("retail_price"),
            "extension_window": ext.isoformat() if ext else None,
            "in_extension": bool(ext and ends_at and ext.timestamp() <= now < ends_at.timestamp()),
            "lot_number": lot.get("lot_number") or static.get("lot_number"),
            "auction_number": static.get("auction_number"),
            "category_name": static.get("category_name"),
            "buyers_assurance_cost": static.get("buyers_assurance_cost"),
            "on_watchlist": lot_id in watching_ids,
            "location_id": str(lot.get("location_id") or static.get("location_id") or "") or None,
            "lot_transferable": static.get("is_transferrable"),
            # Mac.bid's sale-event id (needed to bid). Not "auction_id": that's the item's own id.
            "mb_auction_id": str(lot.get("auction_id") or static.get("auction_id") or "") or None,
            "sale_type": static.get("auction_type") or lot.get("auction_type"),
            # Buyer's Assurance is only for Open Box and Like New (FAQ), never Damaged
            "assurance_cost": None if "DAMAGED" in str(static.get("condition") or "").upper()
            else _num(static.get("buyers_assurance_cost")),
            "condition_explained": condition_explained(static.get("condition")),
            "has_assurance": bool(lot.get("has_buyer_assurance")),
            "min_next_bid": lot_now(lot, my_id)["min_next"],
            "min_next_known": lot_now(lot, my_id)["min_next_known"],
        },
    )


# ---------------------------------------------------------------- client

class MacBidClient:
    """Keeps itself logged in: renews the 30-minute key with the refresh token,
    and (if it has your email + password) logs in again when the refresh token is gone."""

    def __init__(self, session: aiohttp.ClientSession, auth: dict[str, Any] | None, device_id: str,
                 email: str | None = None, password: str | None = None) -> None:
        self._session = session
        self.auth = dict(auth or {})
        self.device_id = device_id
        self._email = email
        self._password = password
        self.auth_changed = False
        # Mac.bid replaces the refresh token on every renewal and the old one stops working, so:
        # one renewal at a time, and the new login is handed over to be saved the moment it arrives.
        self._renew_lock = asyncio.Lock()
        self.on_auth_changed: Callable[[dict[str, Any]], None] | None = None

    @property
    def user_id(self) -> str:
        return str(self.auth.get("user_id") or "")

    def _headers(self, with_auth: bool = True) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json",
             "Origin": WEB, "Referer": WEB + "/"}
        if with_auth and self.auth.get("access_token"):
            h["Authorization"] = self.auth["access_token"]
        return h

    async def _call(self, method: str, path: str, body: Any = None, auth: bool = True, any_status: bool = False) -> Any:
        try:
            async with self._session.request(method, API + path, json=body, headers=self._headers(auth),
                                             timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if any_status and resp.status != 429:
                    # Sign-in answers 4xx with a useful JSON body (wrong password, code needed, ...)
                    try:
                        data = await resp.json(content_type=None)
                    except ValueError:
                        data = None
                    if isinstance(data, dict):
                        return {**data, "_status": resp.status}
                    raise SiteError(f"Mac.bid answered {resp.status} for {path}")
                if resp.status in (401, 403):
                    raise AuthError(f"Mac.bid refused the login ({resp.status})")
                if resp.status == 429:
                    raise RateLimited(f"Mac.bid says too many requests ({path})")
                if resp.status >= 400:
                    raise SiteError(f"Mac.bid answered {resp.status} for {path}")
                try:
                    return await resp.json(content_type=None)
                except ValueError as err:
                    raise SiteError(f"Mac.bid sent something that isn't JSON for {path}") from err
        except aiohttp.ClientError as err:
            raise SiteError(f"Couldn't reach Mac.bid: {err}") from err

    def _store(self, data: dict[str, Any]) -> None:
        if not data or not data.get("access_token"):
            raise AuthError(f"Mac.bid didn't return a login: {data.get('error') if isinstance(data, dict) else data}")
        self.auth = {**self.auth, **parse_auth({**self.auth, **data})}
        if not data.get("expires"):  # never keep the old expiry for a new key (that would renew on every call)
            self.auth["expires"] = time.time() + float(data.get("expires_in") or 1500)
        self.auth_changed = True
        if self.on_auth_changed:
            self.on_auth_changed(self.auth)

    async def login(self) -> None:
        """Sign in like the site does. On a device Mac.bid doesn't know yet it texts or
        emails a code first: then CodeRequired is raised and submit_code() finishes."""
        if not (self._email and self._password):
            raise AuthError("Mac.bid needs logging in again")
        data = await self._call("POST", "/auth/auth-validation", {
            "email": self._email, "password": self._password,
            "device_id": self.device_id, "remember_me": True,
        }, auth=False, any_status=True)
        if data.get("access_token"):
            self._store(data)
            return
        if "is_expired" in data or data.get("type") in ("phone", "email"):
            raise CodeRequired(
                where=data.get("type") or "phone or email",
                last4=str(data.get("last4") or ""),
                new_password=bool(data.get("is_expired")),
            )
        if data.get("email_verified") is False:
            raise AuthError("Mac.bid says this email isn't verified yet; finish that on mac.bid first")
        raise AuthError(str(data.get("error") or data.get("message") or "Mac.bid didn't accept that email and password"))

    async def submit_code(self, code: str) -> None:
        """Finish signing in with the code Mac.bid sent (same call as the site's code form)."""
        data = await self._call("PUT", "/auth/validate-access-code", {
            "code": str(code).strip().replace(" ", "").replace("·", ""),
            "new_password": self._password,
            "device_id": self.device_id, "remember_me": True,
        }, auth=False, any_status=True)
        if not data.get("access_token"):
            raise AuthError(str(data.get("error") or data.get("message") or "Mac.bid didn't accept that code"))
        self._store(data)

    async def renew(self, force: bool = False) -> None:
        async with self._renew_lock:
            # Someone else may have renewed while we waited: don't spend the new token twice
            if not force and self._fresh():
                return
            now = time.time()
            refresh_ok = self.auth.get("refresh_token") and (self.auth.get("expiration_refresh") or 0) > now
            if refresh_ok:
                data = await self._call("PUT", "/auth/refresh-token",
                                        {"device_id": self.device_id, "token": self.auth["refresh_token"], "remember_me": True},
                                        auth=False, any_status=True)
                if data.get("access_token"):
                    self._store(data)
                    return
                # Refused (expired, replaced, or signed out elsewhere): Mac.bid answers 4xx here
                if not (self._email and self._password):
                    raise AuthError(f"Mac.bid needs you to sign in again (it answered {data.get('_status')} to renewing the login)")
            await self.login()

    def _fresh(self) -> bool:
        return bool(self.auth.get("access_token")) and (self.auth.get("expires") or 0) >= time.time() + RENEW_EARLY_SECONDS

    async def ensure_fresh(self) -> None:
        if not self._fresh():
            await self.renew()

    async def get(self, path: str) -> Any:
        await self.ensure_fresh()
        try:
            return await self._call("GET", path)
        except AuthError:
            await self.renew(force=True)  # key revoked early: renew once and retry
            return await self._call("GET", path)

    async def watchlist(self) -> dict[str, dict[str, Any]]:
        return parse_watchlist(await self.get(f"/auctions/customer/{self.user_id}/active-auctions"))

    async def active_ids(self) -> list[str]:
        """Lots you've bid on recently (first page of your bid history)."""
        try:
            return list(dict.fromkeys(parse_active_ids(await self.get(f"/user/{self.user_id}/bid-history?pg=1"))))
        except SiteError:
            return []

    async def pickups(self, now: float | None = None) -> dict[str, AuctionState]:
        """Won items waiting for you to pick them up, with the pickup deadline."""
        return parse_pickups(await self.get(f"/user/{self.user_id}/active"), time.time() if now is None else now)

    async def locations(self) -> dict[str, dict[str, Any]]:
        """Public: all pickup locations with transfer destinations and sales tax."""
        locs = await self._call("GET", "/locations", auth=False)
        blds = await self._call("GET", "/buildings", auth=False)
        return parse_locations(locs if isinstance(locs, list) else [], blds if isinstance(blds, list) else [])

    async def place_bid(self, lot_id: str, lot_number: str, auction_id: str, amount: int) -> dict[str, Any]:
        """Place a max bid (whole dollars), same call as the site's Bid button.
        Mac.bid raises your bid for you as others bid, up to this amount. Spends money if you win."""
        if amount < 1:
            raise SiteError("Bid must be at least $1")
        await self.ensure_fresh()
        data = await self._call("POST", "/map-bid/bid", {
            "bidder_id": int(self.user_id) if self.user_id.isdigit() else self.user_id,
            "lot_id": int(lot_id) if str(lot_id).isdigit() else lot_id,
            "lot_number": lot_number, "auction_id": int(auction_id) if str(auction_id).isdigit() else auction_id,
            "bid_amount": int(amount), "platform": "web", "device_fingerprint": "test",
        }, any_status=True)
        status = data.pop("_status", 200) if isinstance(data, dict) else 200
        if status >= 400 or (isinstance(data, dict) and (data.get("error") or data.get("success") is False)):
            msg = (data.get("error") or data.get("message") or f"status {status}") if isinstance(data, dict) else status
            raise SiteError(f"Mac.bid refused the bid: {msg}")
        return data if isinstance(data, dict) else {}

    async def lot_details(self, lot_id: str) -> dict[str, Any]:
        """Public item details (what the item page shows): condition notes, description, location."""
        data = await self._call("GET", f"/lot/{lot_id}", auth=False)
        return parse_details(data if isinstance(data, dict) else {})

    async def set_assurance(self, lot_id: str, on: bool = True) -> None:
        """Buyer's Assurance (5-business-day return guarantee) on a lot you've bid on."""
        await self.ensure_fresh()
        data = await self._call("POST", "/map-bid/toggle-buyer-assurance", {
            "lot_id": int(lot_id) if str(lot_id).isdigit() else lot_id,
            "platform": "web", "device_fingerprint": "test", "ba": bool(on),
        }, any_status=True)
        status = data.pop("_status", 200) if isinstance(data, dict) else 200
        if status >= 400:
            raise SiteError(f"Mac.bid refused Buyer's Assurance: {short_answer(data)}")

    async def _search_call(self, query: str, filter_by: str, per_page: int, page: int = 1) -> dict[str, Any]:
        body = {"searches": [{"collection": SEARCH_COLLECTION, "q": query,
                              "query_by": "product_name,description,keywords,auction_title",
                              "filter_by": filter_by, "per_page": per_page, "page": page}]}
        try:
            async with self._session.post(SEARCH_URL, params={"x-typesense-api-key": SEARCH_KEY}, json=body,
                                          headers={"Origin": WEB, "Content-Type": "application/json"},
                                          timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 429:
                    raise RateLimited("Mac.bid search says too many requests")
                if resp.status >= 400:
                    raise SiteError(f"Mac.bid search answered {resp.status}")
                return await resp.json(content_type=None) or {}
        except aiohttp.ClientError as err:
            raise SiteError(f"Couldn't reach Mac.bid search: {err}") from err

    async def search(self, query: str, buildings: list[str] | None = None, pages: int = 2) -> tuple[list[dict[str, Any]], int]:
        """Search live Mac.bid lots like the site's search box. With buildings (e.g. ["Akron"]),
        only items at them or transferable to them, like the site's location filter.
        Returns (hits, how many live lots the location filter left out)."""
        live = "lot_status:!=[BUFFER,CLOSED]"
        flt = live
        if buildings:
            names = ",".join("`" + n.replace("`", "") + "`" for n in buildings)
            flt = f"{live} && (auction_location:=[{names}] || transfer_locations:=[{names}])"
        hits: list[dict[str, Any]] = []
        found = 0
        for page in range(1, pages + 1):
            data = await self._search_call(query, flt, 100, page)
            got = search_hits(data, buildings)
            hits += got
            found = search_total(data)
            if len(got) < 100 or len(hits) >= found:
                break
        if not buildings:
            return hits, 0
        nationwide = search_total(await self._search_call(query, live, 1))
        return hits, max(0, nationwide - found)

    async def buildings(self) -> list[dict[str, Any]]:
        """Public list of Mac.bid buildings (pickup sites): [{"id", "name", "outpost"}]."""
        blds = await self._call("GET", "/buildings", auth=False)
        return [{"id": str(b.get("id")), "name": b.get("name"),
                 "outpost": str(b.get("is_pickup_outpost") or "0") not in ("0", "", "False", "false")}
                for b in blds or [] if isinstance(b, dict) and b.get("name")]

    async def my_building_ids(self) -> list[str]:
        """The pickup locations chosen on your account (the site's location picker)."""
        data = await self.get(f"/user/{self.user_id}/firebase/locations")
        if isinstance(data, dict):
            data = data.get("locations") or data.get("hits") or []
        return [str(x) for x in data or []]

    async def keywords(self) -> list[str]:
        """Your keyword alerts (Account -> Notifications)."""
        return keywords_from_profile(await self.get("/user/me?updated=true"))

    async def save_keywords(self, keywords: list[str]) -> None:
        """Save the keyword list: the same profile update the site's Notifications page sends
        when you add a keyword or tap the trash can."""
        await self.ensure_fresh()
        await self._call("PUT", f"/user/{self.user_id}",
                         {"keywords": ",".join(k.strip() for k in keywords if k.strip()), "verify_phone_enabled": True})

    async def watch(self, lot_id: str, on: bool = True) -> None:
        """Add (or remove) a lot on your Mac.bid watchlist: the site's own watch button."""
        await self.ensure_fresh()
        if on:
            await self._call("POST", f"/user/{self.user_id}/watchlist",
                             {"auction_lot_id": int(lot_id) if str(lot_id).isdigit() else lot_id})
        else:
            await self._call("DELETE", f"/user/{self.user_id}/watchlist/{lot_id}")

    async def lot(self, lot_id: str) -> dict[str, Any]:
        return await self.get(f"/map-bid/ddb/lot/{lot_id}")


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _dt(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None
