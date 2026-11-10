"""GovAuctions (pay-per-bid penny auctions).

Endpoints, all read-only, all found in the site's own web app:
  /api/v1/auctionFeed/my-auctions/{all,favorites,wins}  -> lists of auction ids
  /gonzales.php?idlist=..&auctionDetailsIds=..          -> live price, clock, last 9 bids, your credits
  /api/v1/auction/staticData?auctionIds[]=..            -> titles, Buy It Now, labels, start time
  /api/v2/auction/extraStaticData?auctionId=..          -> previous winners of the same product

Live feed fields: i id, s 1=live, r price, w leader, t clock seconds,
noNewBidders, x = bids you've placed in it, bb = your booked bids {a: booked, c: left},
p.bc = your point balance, history rows = [price, unix time, username, ...].
"""

from __future__ import annotations

import re
import statistics
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp

from ..cookies import cookie_header
from .base import (
    CATEGORY_BIDDING,
    CATEGORY_WATCHING,
    STATUS_ENDED,
    STATUS_LIVE,
    STATUS_UPCOMING,
    AuctionState,
    AuthError,
    PriceHistory,
    SiteError,
)

SITE = "govauctions"
PACKS_CATEGORY = 7  # the site's "Packs" category filter
BASE_URL = "https://www.govauctions.com"
COOKIE_DOMAIN = "govauctions.com"
ACTIVE_WINDOW_SECONDS = 60
USER_AGENT = "Mozilla/5.0 (compatible; HA-AuctionWatcher/0.1; +https://github.com/wolfheartrising/HA_AuctionWatcher)"


# ---------------------------------------------------------------- parsing

def parse_feed_ids(data: dict[str, Any]) -> list[str]:
    """Ids from an /api/v1/auctionFeed/... response."""
    for item in data.get("feed") or []:
        ids = item.get("data")
        if isinstance(ids, list):
            return [str(i) for i in ids]
    return []


def parse_live(data: dict[str, Any], now: float) -> tuple[dict[str, dict[str, Any]], int | None]:
    """Return ({id: live fields}, point balance). Raises AuthError when logged out."""
    account = data.get("p") or {}
    if account.get("s") != 1 or "bc" not in account:
        raise AuthError("GovAuctions says you're not logged in; the cookie has expired or is wrong")

    histories = {str(d.get("auctionId")): d.get("history") or [] for d in data.get("auctionsDetails") or []}
    out: dict[str, dict[str, Any]] = {}
    for a in data.get("auctions") or []:
        aid = str(a.get("i"))
        recent: list[str] = []
        rows: list[tuple[float, str]] = []
        for row in histories.get(aid, []):
            try:
                ts, user = float(row[1]), str(row[2])
            except (IndexError, TypeError, ValueError):
                continue
            rows.append((ts, user))
            if ts > now - ACTIVE_WINDOW_SECONDS and user not in recent:
                recent.append(user)
        booked = a.get("bb") or {}
        out[aid] = {
            "live": a.get("s") == 1,
            "price": _float(a.get("r")),
            "leader": a.get("w"),
            "clock_seconds": _int(a.get("t")),
            "closed_to_new": bool(a.get("noNewBidders")),
            "your_bids": _int(a.get("x")) or 0,
            "your_booked_total": _int(booked.get("a")) or 0,
            "your_booked_left": _int(booked.get("c")) or 0,
            "recent_bidders": recent,
            # Only meaningful if we got the bid history for this auction
            "active_bidders": len(recent) if aid in histories else None,
            # Every bid the site showed us: [(unix time, username)], for the
            # coordinator's longer-window bidder count
            "bid_rows": rows if aid in histories else None,
        }
    return out, _int(account.get("bc"))


# Short notes for the special auction types (from GovAuctions' own rules pages).
# The site's own icon text is used when it sends one; these fill the gaps.
LABEL_NOTES = {
    "1¢ auction": "Winner pays 1¢, whatever the final price. Your cost is your bids.",
    "win-win": "Winner gets half of the points they used back.",
    "spoils": "Winner also gets free points equal to the Buy It Now dollar amount.",
    "runner-up": "Second place gets 50% off Buy It Now (and points back if used).",
    "leaderboard": "Top 3 by time as highest bidder get extra points at the end.",
    "no re-entry": "After the entry limit, anyone without active booked bids can't bid again. Don't run out of booked bids.",
    "duel": "After the entry limit, if two bidders place the last four bids, a 60 s countdown can lock it to just those two.",
    "early bird": "Booked bids can only be set up until 10 s before the start.",
    "overload": "At most 100 bids booked at a time.",
    "triple-booked": "You can have booked bids in at most three Triple-Booked auctions at once.",
    "splitsies": "Several identical auctions start together; you may join only one.",
    "reveal": "Usernames are hidden until it ends.",
    "one per user": "Once you've won this item, you can't bid on it again.",
    "mystery": "Item revealed 5 minutes before the start; bids can be booked only after that.",
    "no exchange": "Can't be exchanged for points if you win.",
    "free-to-bid": "No points needed: set a max bid and it bids for you, like a classic auction.",
}


def label_note(label: str, site_message: str | None = None) -> str:
    if site_message:
        return site_message
    key = label.lower()
    for name, note in LABEL_NOTES.items():
        if name in key:
            return note
    m = re.search(r"(\d{1,3})\s*%", key)
    if m:
        return f"Winner's final price is discounted {m.group(1)}%."
    return ""


def parse_static(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for aid, s in (data.get("static") or {}).items():
        images = s.get("images") or {}
        labels = [i.get("title") for i in s.get("icons") or [] if i.get("title")]
        site_msgs = {i.get("title"): i.get("message") for i in s.get("icons") or [] if i.get("title")}
        for flag, label in (("onePerUser", "One Per User"), ("noReEntry", "No Re-Entry")):
            if s.get(flag):
                labels.append(label)
        if s.get("exchangeable") is False:
            labels.append("No Exchange")
        start = _int(s.get("unixStarts") or s.get("startTime"))
        out[str(aid)] = {
            "title": s.get("name") or f"GovAuctions {aid}",
            "image": images.get("frontpageImage") or (images.get("thumbnails") or [None])[0],
            "buy_it_now": _float(s.get("buyItNowPrice")),
            "product_id": str(s.get("productId") or ""),
            "no_jumper_limit": _float(s.get("noJumperLimit")),
            "starts_at": datetime.fromtimestamp(start, timezone.utc) if start else None,
            "labels": labels,
            "label_notes": {l: label_note(l, site_msgs.get(l)) for l in labels},
            "category_name": s.get("categoryName"),
        }
    return out


def parse_history(data: dict[str, Any], auction_id: str) -> PriceHistory | None:
    block = data.get(str(auction_id)) or {}
    wins = block.get("previous_winners") or []
    prices, bids, costs = [], [], []
    for w in wins:
        p, b = _float(w.get("price")), _int(w.get("bids_placed"))
        if p is None or b is None:
            continue
        prices.append(p)
        bids.append(b)
        c = _float(w.get("estimated_total_cost"))
        if c is not None:
            costs.append(c)
    if not prices:
        return None
    unit = str(block.get("pastWinnersIntervalUnit") or "day").lower()
    interval = _int(block.get("pastWinnersInterval")) or 30
    period_days = interval * {"week": 7, "month": 30}.get(unit.rstrip("s"), 1)
    return PriceHistory(
        wins=len(prices),
        period_days=period_days,
        avg_final_price=round(statistics.mean(prices), 2),
        median_final_price=round(statistics.median(prices), 2),
        low_final_price=min(prices),
        high_final_price=max(prices),
        avg_winner_bids=round(statistics.mean(bids)),
        median_winner_bids=round(statistics.median(bids)),
        avg_winner_cost=round(statistics.mean(costs), 2) if costs else None,
    )


def bundle_points(title: str) -> int | None:
    """'DJI Drone ... + 2300 Points' -> 2300 (bonus points that come with an item)."""
    m = re.search(r"\+\s*([\d,]+)\s*(?:points|pts)\b", title or "", re.I)
    return int(m.group(1).replace(",", "")) if m else None


def win_terms(labels: list[str], price: float | None, your_bids: int, point_cost: float) -> tuple[float | None, float | None]:
    """(price you'd pay if you won now, that plus what your bids cost).

    From the site's rules: "1¢" auctions -> winner pays 1 cent; "NN% off" discounts
    the final price; "Win-Win" gives back half the points the winner used.
    """
    if price is None:
        return None, None
    text = " ".join(labels).lower()
    pay = price
    if "1¢" in text or "1c auction" in text or "one cent" in text:
        pay = 0.01
    else:
        m = re.search(r"(\d{1,3})\s*%\s*off", text)
        if m:
            pay = round(price * (1 - int(m.group(1)) / 100), 2)
    points = your_bids * point_cost
    if "win-win" in text:
        points /= 2
    return pay, round(pay + points, 2)


def build_state(
    auction_id: str,
    live: dict[str, Any] | None,
    static: dict[str, Any] | None,
    history: PriceHistory | None,
    watching_ids: set[str],
    point_cost: float,
    now: float,
) -> AuctionState:
    live = live or {}
    static = static or {}
    starts_at = static.get("starts_at")
    if live.get("live"):
        status = STATUS_LIVE
    elif starts_at and starts_at.timestamp() > now:
        status = STATUS_UPCOMING
    else:
        status = STATUS_ENDED
    your_bids = live.get("your_bids", 0)
    bidding = your_bids > 0 or live.get("your_booked_left", 0) > 0
    labels = static.get("labels", [])
    pay_if_won, cost_if_won_now = win_terms(labels, live.get("price"), your_bids, point_cost)
    bonus = bundle_points(static.get("title") or "")
    return AuctionState(
        site=SITE,
        auction_id=auction_id,
        title=static.get("title") or f"GovAuctions {auction_id}",
        url=f"{BASE_URL}/auction/{auction_id}",
        category=CATEGORY_BIDDING if bidding else CATEGORY_WATCHING,
        status=status,
        price=live.get("price"),
        image=static.get("image"),
        leader=live.get("leader"),
        clock_seconds=live.get("clock_seconds"),
        starts_at=starts_at,
        # unixStarts is when the first long countdown runs out (checked against the live clock)
        original_end=starts_at,
        active_bidders=live.get("active_bidders"),
        recent_bidders=live.get("recent_bidders", []),
        closed_to_new=live.get("closed_to_new"),
        your_bids=your_bids,
        your_booked_left=live.get("your_booked_left", 0),
        your_booked_total=live.get("your_booked_total", 0),
        your_points_cost=round(your_bids * point_cost, 2),
        pay_if_won=pay_if_won,
        cost_if_won_now=cost_if_won_now,
        buy_it_now=static.get("buy_it_now"),
        history=history,
        labels=labels,
        extra={
            "no_jumper_limit": static.get("no_jumper_limit"),
            "label_notes": static.get("label_notes", {}),
            # Item + points bundles: the points' value comes off what winning costs you
            "bundle_points": bonus,
            "bundle_value": round(bonus * point_cost, 2) if bonus else None,
            "net_cost_if_won_now": (round(cost_if_won_now - bonus * point_cost, 2)
                                    if bonus and cost_if_won_now is not None else None),
            "product_id": static.get("product_id"),
            "category_name": static.get("category_name"),
        },
    )


# ---------------------------------------------------------------- client

class GovAuctionsClient:
    """Thin async client. Keeps the cookies the site refreshes."""

    def __init__(self, session: aiohttp.ClientSession, cookies: dict[str, str]) -> None:
        self._session = session
        self.cookies = dict(cookies)
        self.cookies_changed = False

    async def _get(self, path: str, params: Any = None) -> Any:
        return await self._request("GET", path, params=params)

    async def _request(self, method: str, path: str, params: Any = None, json_body: Any = None) -> Any:
        headers = {
            "Cookie": cookie_header(self.cookies),
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{BASE_URL}/my-bids",
            "User-Agent": USER_AGENT,
        }
        if json_body is not None:
            headers["Origin"] = BASE_URL
        try:
            async with self._session.request(
                method, BASE_URL + path, params=params, json=json_body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=10), allow_redirects=False,
            ) as resp:
                self._keep_cookies(resp)
                if resp.status in (401, 403):
                    raise AuthError(f"GovAuctions refused the login ({resp.status})")
                if resp.status in (301, 302, 303, 307, 308):
                    raise AuthError("GovAuctions redirected to its login page")
                if resp.status != 200:
                    raise SiteError(f"GovAuctions answered {resp.status} for {path}")
                try:
                    return await resp.json(content_type=None)
                except ValueError as err:
                    raise SiteError(f"GovAuctions sent a non-JSON page for {path}") from err
        except aiohttp.ClientError as err:
            raise SiteError(f"Couldn't reach GovAuctions: {err}") from err

    def _keep_cookies(self, resp: aiohttp.ClientResponse) -> None:
        for name, morsel in resp.cookies.items():
            value = morsel.value
            if not value or value == "deleted" or morsel.get("max-age") == "0":
                continue
            if self.cookies.get(name) != value:
                self.cookies[name] = value
                self.cookies_changed = True

    async def lists(self) -> dict[str, list[str]]:
        out = {}
        for key, feed in (("all", "all"), ("watching", "favorites"), ("won", "wins")):
            out[key] = parse_feed_ids(await self._get(f"/api/v1/auctionFeed/my-auctions/{feed}"))
        return out

    async def live(self, ids: list[str], now: float | None = None) -> tuple[dict[str, dict[str, Any]], int | None]:
        idlist = ",".join(ids) if ids else "0"
        # Built by hand: the site expects literal commas
        data = await self._get(f"/gonzales.php?idlist={idlist}&auctionDetailsIds={idlist}")
        return parse_live(data, time.time() if now is None else now)

    async def static(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        if not ids:
            return {}
        params = [("auctionIds[]", i) for i in ids] + [("withBidderDetails", "0")]
        return parse_static(await self._get("/api/v1/auction/staticData", params=params))

    async def history(self, auction_id: str) -> PriceHistory | None:
        data = await self._get("/api/v2/auction/extraStaticData", params={"auctionId": auction_id})
        return parse_history(data, auction_id)

    # ---- point-pack scanner (read-only)

    async def browse_category(self, category_id: int) -> list[str]:
        """Same list as the site's category filter (Packs = 7): live and upcoming auction ids."""
        data = await self._get("/api/v1/browse", params={"cat": str(category_id)})
        return [str(i) for i in data] if isinstance(data, list) else []

    async def previous_winners(self, auction_id: str) -> list[dict[str, Any]]:
        data = await self._get("/api/v2/auction/extraStaticData", params={"auctionId": auction_id})
        return (data.get(str(auction_id)) or {}).get("previous_winners") or []

    async def me(self) -> dict[str, str]:
        """Your username and id, so you can be left out of bidder counts.
        Only these two fields are kept; the rest of the account response is ignored."""
        data = await self._get("/api/v2/users/me")
        data = data.get("user") if isinstance(data.get("user"), dict) else data
        return {"id": str(data.get("id") or ""), "name": str(data.get("name") or data.get("username") or "")}

    # ---- booking: these spend points. Same calls the site's own Book / Cancel buttons make.

    async def book(self, auction_id: str, count: int, max_price: float | None = None) -> dict[str, Any]:
        """Book `count` bids. With max_price the site stops bidding past that price."""
        if count < 1:
            raise SiteError("Nothing to book")
        body: dict[str, Any] = {"bid_count": int(count), "book_partial": False}
        if max_price:
            body["max_price"] = float(max_price)
        data = await self._request("POST", f"/api/v1/auction/{auction_id}/bidBuddy", json_body=body)
        return booking_result(data, "book")

    async def cancel_booked(self, auction_id: str) -> dict[str, Any]:
        """Cancel your booked bids. Unused points go back to your balance."""
        data = await self._request("DELETE", f"/api/v1/auction/{auction_id}/bidBuddy")
        return booking_result(data, "cancel")


def booking_result(data: Any, action: str) -> dict[str, Any]:
    """The site answers {"success": true, ...} or an error; turn errors into SiteError."""
    if isinstance(data, dict) and data.get("success"):
        return data
    message = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            message = err.get("message") or err.get("title")
        elif isinstance(err, str):
            message = err
        message = message or data.get("message")
    raise SiteError(f"GovAuctions refused to {action}: {message or 'no reason given'}")


def _float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None
