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

import time
import uuid
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
USER_AGENT = "Mozilla/5.0 (compatible; HA-AuctionWatcher; +https://github.com/wolfheartrising/HA_AuctionWatcher)"
RENEW_EARLY_SECONDS = 120  # renew the 30-minute key this long before it expires


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


def lot_refresh_seconds(seconds_left: float | None) -> int:
    """How often one lot needs checking: often only near its end."""
    if seconds_left is None:
        return 900
    if seconds_left <= 15 * 60:
        return 0          # every check
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
            f"{locations[d]['name']} ({locations[d]['city_state']})" for d in loc.get("transfer_destinations", [])
            if d in locations) if can else [],
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
        url=(f"{WEB}/auction/{lot.get('auction_id') or static.get('auction_id')}/lot/{lot_id}"
             if (lot.get("auction_id") or static.get("auction_id")) else f"{WEB}/account/watchlist"),
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
            "auction_id": str(lot.get("auction_id") or static.get("auction_id") or "") or None,
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

    @property
    def user_id(self) -> str:
        return str(self.auth.get("user_id") or "")

    def _headers(self, with_auth: bool = True) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json",
             "User-Agent": USER_AGENT, "Origin": WEB, "Referer": WEB + "/"}
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
        self.auth_changed = True

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

    async def renew(self) -> None:
        now = time.time()
        refresh_ok = self.auth.get("refresh_token") and (self.auth.get("expiration_refresh") or 0) > now
        if refresh_ok:
            try:
                data = await self._call("PUT", "/auth/refresh-token",
                                        {"device_id": self.device_id, "token": self.auth["refresh_token"], "remember_me": True},
                                        auth=False)
                self._store(data)
                return
            except AuthError:
                if not self._email:
                    raise
        await self.login()

    async def ensure_fresh(self) -> None:
        if not self.auth.get("access_token") or (self.auth.get("expires") or 0) < time.time() + RENEW_EARLY_SECONDS:
            await self.renew()

    async def get(self, path: str) -> Any:
        await self.ensure_fresh()
        try:
            return await self._call("GET", path)
        except AuthError:
            await self.renew()  # key revoked early: renew once and retry
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
