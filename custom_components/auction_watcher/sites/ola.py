"""OLA (Online Liquidation Auction, olabid.com) — read-only for now.

Uses OLA's own app API (endpoints the vendor documented for us; honest user agent, no app
impersonation). Email sign-in -> access + refresh tokens, then your watchlist, winning items and
bid history. Bidding is over OLA's realtime SignalR hub (a short-lived "live bidding token" plus a
method on the hub, like Mac.bid's websocket); the transport is built here, but the hub's exact
endpoint, token path and method name/arguments come from the app bundle and aren't live-confirmed
yet (isolated in the "bidding" section below so confirming them is a one-line change). Nothing here
bids on its own — the coordinator only calls place_max_bid when you tap Bid.

Field names in OLA's JSON aren't documented, so parsing tries the common names and logs the real
keys once (INFO "OLA item fields") — send that line to finalise the mapping. No Home Assistant
imports here, so it's unit-testable on its own.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp

from .base import (AuctionState, AuthError, SiteError, CATEGORY_BIDDING, CATEGORY_WATCHING,
                   STATUS_ENDED, STATUS_LIVE, STATUS_PICKUP)

_LOGGER = logging.getLogger(__name__)

SITE = "ola"
API = "https://auction-api.olabid.com/api/v2"
# OLA's app API key (the same one shipped in their app; required on every request, with the owner's OK)
API_KEY = "1BAF2123-00D7-421E-B999-FB0B222D6577"
ITEM_URL = "https://link.olabid.com/item-details?id={id}"
RENEW_EARLY_SECONDS = 60

_logged_fields = False
# OLA won-item statuses that still need collecting (awaiting pickup) vs already done
PICKUP_PENDING = {"waitingforpayment", "readyforpickup", "scheduled", "waitingforpickup", "topickup", "paid"}
DONE_STATUSES = {"pickedup", "shipped", "delivered", "completed", "refunded", "cancelled"}


# ---------------------------------------------------------------- tolerant helpers

def _first(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return None


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = v.replace("$", "").replace(",", "").strip()
        try:
            return float(m)
        except ValueError:
            return None
    return None


def _int(v: Any) -> int | None:
    n = _num(v)
    return int(n) if n is not None else None


def _dt(v: Any) -> datetime | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000 if v > 1e12 else v, timezone.utc)
    s = str(v).strip().replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _as_list(data: Any) -> list[dict[str, Any]]:
    """OLA may wrap the list in data/items/results/records, or return it bare."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("data", "items", "results", "records", "auctionItems", "content", "list"):
            v = data.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
            if isinstance(v, dict):  # one more level, e.g. {data: {items: [...]}}
                for k2 in ("items", "results", "records", "data"):
                    if isinstance(v.get(k2), list):
                        return [x for x in v[k2] if isinstance(x, dict)]
        if _first(data, "id", "itemId", "auctionItemId"):  # a single item
            return [data]
    return []


# ---------------------------------------------------------------- parsing

def parse_tokens(data: Any) -> dict[str, Any]:
    """Pull tokens out of a sign-in / refresh response (field names vary)."""
    d = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
    if not isinstance(d, dict):
        raise AuthError("OLA didn't return a login")
    access = _first(d, "accessToken", "access_token", "token", "jwt")
    refresh = _first(d, "refreshToken", "refresh_token")
    if not access:
        msg = _first(d, "message", "error", "detail") or "no token"
        raise AuthError(f"OLA sign-in didn't return a token ({msg})")
    expires_in = _num(_first(d, "expiresIn", "accessTokenExpiresIn", "expires_in"))
    expires_at = _dt(_first(d, "accessTokenExpires", "expiresAt", "accessTokenExpiresAt"))
    expires = expires_at.timestamp() if expires_at else time.time() + (expires_in or 1500)
    refresh_exp = _dt(_first(d, "refreshTokenExpires", "refreshTokenExpiresAt"))
    refresh_in = _num(_first(d, "refreshTokenExpiresIn", "refresh_expires_in"))
    if refresh_exp is not None:
        refresh_expires = refresh_exp.timestamp()
    elif refresh_in:
        refresh_expires = time.time() + refresh_in
    else:
        refresh_expires = time.time() + 60 * 86400
    return {
        "access_token": str(access),
        "refresh_token": str(refresh) if refresh else "",
        "expires": expires,
        "refresh_expires": refresh_expires,
        "user_id": str(_first(d, "userId", "id", "customerId") or ""),
    }


def item_state(raw: dict[str, Any], category: str, now: float, my_id: str = "") -> AuctionState:
    """One OLA auction item -> AuctionState, trying the common field names."""
    global _logged_fields
    if not _logged_fields:
        _logged_fields = True
        _LOGGER.info("OLA item fields (send this to finalise the mapping): %s",
                     {k: v for k, v in raw.items() if not isinstance(v, (dict, list))})
    item_id = str(_first(raw, "id", "itemId", "auctionItemId", "lotId", "auctionItemInventoryId") or "")
    title = _first(raw, "name", "title", "productName", "itemName") or f"OLA item {item_id}"
    price = _num(_first(raw, "currentBidAmount", "currentBid", "current_bid", "highBid", "currentPrice",
                        "winningBid", "bidAmount", "latestBid", "price"))
    next_bid = _num(_first(raw, "nextBidAmount", "minimumNextBid", "minNextBid"))
    ends = _dt(_first(raw, "endsAt", "endDate", "auctionEndsAt", "closingDate", "endTime",
                      "auctionEndDate", "biddingEndsAt", "expiresAt"))
    image = _first(raw, "previewBlobUrl", "imageUrl", "image", "thumbnailUrl", "thumbnail", "mainImage", "primaryImage")
    if not image:
        imgs = _first(raw, "images", "imageUrls", "photos")
        if isinstance(imgs, list) and imgs:
            image = imgs[0] if isinstance(imgs[0], str) else _first(imgs[0], "url", "imageUrl", "src")
    your_max = _num(_first(raw, "myMaxBid", "maxBid", "proxyBid", "myBid", "yourMaxBid"))
    bidder = _first(raw, "currentBidderId", "winningBidderId", "highBidderId")
    winning = bool(_first(raw, "isWinning", "isCurrentBidder", "winning", "isHighBidder")) or \
        (bool(my_id) and bidder is not None and str(bidder) == str(my_id))
    sold = str(_first(raw, "status") or "").lower() in ("sold", "ended", "closed", "finished")
    finishes_ms = _num(_first(raw, "auctionFinishesInMs"))
    ended = sold or (ends is not None and ends.timestamp() <= now) or (finishes_ms is not None and finishes_ms <= 0)
    status = STATUS_ENDED if ended else STATUS_LIVE
    return AuctionState(
        site=SITE,
        auction_id=item_id,
        title=str(title),
        url=ITEM_URL.format(id=item_id),
        category=category,
        status=status,
        price=price,
        image=str(image) if image else None,
        ends_at=ends,
        original_end=ends,
        leader="you" if winning else None,
        pay_if_won=price,
        cost_if_won_now=price,
        buy_it_now=_num(_first(raw, "buyNowPrice", "instantWinPrice", "buyItNow")),
        labels=[x for x in [_first(raw, "condition", "conditionName")] if x],
        extra={
            "winning": winning,
            "you_are_bidding": category == CATEGORY_BIDDING,
            "on_watchlist": category == CATEGORY_WATCHING,
            "your_max_bid": your_max,
            "min_next_bid": next_bid,
            "status_text": _first(raw, "status"),
            "shipping": _num(_first(raw, "shippingCost")),
            "is_paid": bool(_first(raw, "isPaid")) if _first(raw, "isPaid") is not None else None,
            "sku": _first(raw, "sku"),
            "total_bids": _int(_first(raw, "numberOfBids", "bidCount", "totalBids", "bidsCount")),
            "watchers": _int(_first(raw, "watcherCount", "watchers", "watchCount")),
            "retail_price": _num(_first(raw, "retailPrice", "msrp", "estimatedRetail", "retail")),
            "location": _first(raw, "locationName", "location", "warehouse", "pickupLocation"),
        },
    )


def search_hits(data: Any) -> list[dict[str, Any]]:
    """OLA search results -> plain dicts (turned into Hit objects by the search manager)."""
    out = []
    for raw in _as_list(data):
        item_id = str(_first(raw, "id", "itemId", "auctionItemId") or "")
        if not item_id:
            continue
        out.append({
            "item_id": item_id,
            "title": _first(raw, "name", "title") or f"OLA item {item_id}",
            "url": ITEM_URL.format(id=item_id),
            "price": _num(_first(raw, "currentBidAmount", "currentBid", "price")),
            "ends_at": (_dt(_first(raw, "auctionEndsAt", "endsAt")).isoformat()
                        if _dt(_first(raw, "auctionEndsAt", "endsAt")) else None),
            "image": _first(raw, "previewBlobUrl", "imageUrl", "image"),
            "condition": _first(raw, "status"),
        })
    return out


def parse_items(data: Any, category: str, now: float, my_id: str = "") -> dict[str, AuctionState]:
    out: dict[str, AuctionState] = {}
    for raw in _as_list(data):
        st = item_state(raw, category, now, my_id)
        if st.auction_id:
            out[st.auction_id] = st
    return out


# ---------------------------------------------------------------- client

class OlaClient:
    """OLA app API client: email sign-in, token refresh, and read endpoints."""

    def __init__(self, session: aiohttp.ClientSession, auth: dict[str, Any] | None = None,
                 email: str | None = None, password: str | None = None, api_key: str | None = None) -> None:
        self._session = session
        self.auth = dict(auth or {})
        self._email = email
        self._password = password
        self._api_key = (api_key or "").strip() or API_KEY
        self.auth_changed = False

    @property
    def signed_in(self) -> bool:
        return bool(self.auth.get("access_token") or (self._email and self._password))

    @property
    def user_id(self) -> str:
        return str(self.auth.get("user_id") or "")

    def _headers(self, auth: bool = True) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json", "X-Api-Key": self._api_key}
        if auth and self.auth.get("access_token"):
            h["Authorization"] = f"Bearer {self.auth['access_token']}"
        return h

    async def _call(self, method: str, path: str, body: Any = None, auth: bool = True,
                    any_status: bool = False) -> Any:
        url = path if path.startswith("http") else API + path
        try:
            async with self._session.request(method, url, json=body, headers=self._headers(auth),
                                             timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if any_status and resp.status != 429:
                    try:
                        data = await resp.json(content_type=None)
                    except (ValueError, aiohttp.ContentTypeError):
                        data = None
                    if isinstance(data, dict):
                        return {**data, "_status": resp.status}
                    raise SiteError(f"OLA answered {resp.status} for {path}")
                if resp.status in (401, 403):
                    raise AuthError(f"OLA sign-in no longer valid ({resp.status})")
                if resp.status == 429:
                    raise SiteError(f"OLA says too many requests ({path})")
                if resp.status >= 400:
                    raise SiteError(f"OLA answered {resp.status} for {path}")
                if resp.status == 204 or resp.content_length == 0:
                    return {}
                try:
                    return await resp.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError):
                    return {}
        except aiohttp.ClientError as err:
            raise SiteError(f"Couldn't reach OLA: {err}") from err

    def _store(self, data: Any) -> None:
        self.auth = {**self.auth, **parse_tokens(data)}
        self.auth_changed = True

    async def sign_in(self) -> None:
        if not (self._email and self._password):
            raise AuthError("OLA needs your email and password")
        data = await self._call("POST", "/account/sign-in/email",
                                {"email": self._email, "password": self._password}, auth=False, any_status=True)
        status = data.pop("_status", 200) if isinstance(data, dict) else 200
        if status >= 400:
            raise AuthError(str(_first(data, "message", "error", "detail") or f"OLA sign-in failed ({status})"))
        self._store(data)

    async def refresh(self) -> None:
        token = self.auth.get("refresh_token")
        if token and (self.auth.get("refresh_expires") or 0) > time.time():
            try:
                data = await self._call("POST", "/account/refresh",
                                        {"refreshToken": token}, auth=False, any_status=True)
                if isinstance(data, dict) and data.pop("_status", 200) < 400 and _first(data, "accessToken", "access_token", "token"):
                    self._store(data)
                    return
            except AuthError:
                pass
        await self.sign_in()

    async def ensure_fresh(self) -> None:
        if not self.auth.get("access_token") or (self.auth.get("expires") or 0) < time.time() + RENEW_EARLY_SECONDS:
            await self.refresh()

    async def get(self, path: str) -> Any:
        await self.ensure_fresh()
        try:
            return await self._call("GET", path)
        except AuthError:
            await self.refresh()  # token revoked early: refresh once and retry
            return await self._call("GET", path)

    async def account(self) -> dict[str, Any]:
        data = await self.get("/account/current")
        return data if isinstance(data, dict) else {}

    async def watchlist(self, now: float) -> dict[str, AuctionState]:
        return parse_items(await self.get("/auction-items/my/watchlist"), CATEGORY_WATCHING, now, self.user_id)

    async def winning(self, now: float) -> dict[str, AuctionState]:
        return parse_items(await self.get("/auction-items/my/winning"), CATEGORY_BIDDING, now, self.user_id)

    async def bids_history(self, now: float) -> dict[str, AuctionState]:
        return parse_items(await self.get("/auction-items/my/bids/history"), CATEGORY_BIDDING, now, self.user_id)

    async def outbid(self, now: float) -> dict[str, AuctionState]:
        return parse_items(await self.get("/auction-items/my/outbidded"), CATEGORY_BIDDING, now, self.user_id)

    async def item_details(self, item_id: str) -> dict[str, Any]:
        """Full item detail (condition, total bids, shipping): GET /auction-items/{id}/details."""
        data = await self.get(f"/auction-items/{item_id}/details")
        return data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else (data or {})

    async def wins(self, now: float) -> dict[str, AuctionState]:
        """Items you've won: awaiting pickup (not yet collected) stay on the Awaiting-pickup tab."""
        out = parse_items(await self.get("/auction-items/my/wins-history"), CATEGORY_BIDDING, now, self.user_id)
        for st in out.values():
            status = str(st.extra.get("status_text") or "").lower().replace(" ", "")
            if status in DONE_STATUSES:
                st.status = STATUS_ENDED
            elif st.extra.get("winning") or status in PICKUP_PENDING:
                st.status = STATUS_PICKUP
        return out

    async def keywords(self) -> list[dict[str, Any]]:
        """Your OLA saved searches (the app calls them keywords): [{"id","name","active"}]."""
        data = await self.get("/keywords/list")
        return [{"id": str(_first(k, "id") or ""), "name": _first(k, "name", "keyword") or "", "active": bool(_first(k, "isActive"))}
                for k in _as_list(data) if _first(k, "name", "keyword")]

    async def add_keyword(self, name: str) -> None:
        """Save a keyword (OLA saved search). Same as the app's add-keyword."""
        await self.ensure_fresh()
        await self._call("POST", "/keywords/create", {"keyword": name})

    async def remove_keyword(self, keyword_id: str) -> None:
        """Delete one of your OLA saved searches by id."""
        await self.ensure_fresh()
        await self._call("DELETE", f"/keywords/{keyword_id}/delete")

    async def search(self, query: str, pages: int = 1, page_size: int = 50) -> list[dict[str, Any]]:
        """Search OLA's live catalogue (same call as the app's search): POST /auction-items/list."""
        hits: list[dict[str, Any]] = []
        for page in range(1, pages + 1):
            data = await self._call("POST", "/auction-items/list",
                                    {"searchQuery": query, "pageNumber": page, "pageSize": page_size})
            got = search_hits(data)
            hits += got
            if len(got) < page_size:
                break
        return hits


def build_states(watchlist: dict[str, AuctionState], winning: dict[str, AuctionState],
                 history: dict[str, AuctionState], outbid: dict[str, AuctionState] | None = None) -> dict[str, AuctionState]:
    """Merge the lists. Later sources override: history (ended) < watchlist < outbid < winning."""
    out: dict[str, AuctionState] = {}
    for src in (history, watchlist, outbid or {}, winning):
        for aid, st in src.items():
            out[aid] = st
    return out
