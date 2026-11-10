"""eBay, through eBay's official APIs (no page scraping).

Needs a free eBay developer Production keyset: App ID (client id) + Cert ID (client secret).

Level A, keys only (application token):
  POST /identity/v1/oauth2/token  grant_type=client_credentials
  GET  /buy/browse/v1/item/get_item_by_legacy_id?legacy_item_id=...   -> price, bids, end time
  Follows the item numbers you add.

Level B, plus signing in with your eBay account (user token, ~18-month refresh):
  The user opens the consent link, signs in, and pastes back the address of
  eBay's "sign-in complete" page; the code in it is exchanged for tokens.
  Trading API GetMyeBayBuying (WatchList + BidList)   -> your watchlist and bids,
  including whether you're the high bidder and your max bid.

NOTE: written from eBay's documented formats; not yet run against eBay
(the developer keys were still under review).
"""

from __future__ import annotations

import base64
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import aiohttp

from .base import (
    CATEGORY_BIDDING,
    CATEGORY_WATCHING,
    STATUS_ENDED,
    STATUS_LIVE,
    AuctionState,
    AuthError,
    SiteError,
)

SITE = "ebay"
API = "https://api.ebay.com"
AUTH = "https://auth.ebay.com/oauth2/authorize"
SCOPES = "https://api.ebay.com/oauth/api_scope"
MARKETPLACE = "EBAY_US"
TRADING_COMPAT = "1271"
NS = {"e": "urn:ebay:apis:eBLBaseComponents"}
RENEW_EARLY_SECONDS = 120


class RateLimited(SiteError):
    """eBay answered 429."""


# ---------------------------------------------------------------- sign-in helpers

def consent_url(app_id: str, runame: str) -> str:
    """Link that opens eBay's sign-in/consent page for this app."""
    return (f"{AUTH}?client_id={quote(app_id)}&response_type=code"
            f"&redirect_uri={quote(runame)}&scope={quote(SCOPES)}")


def code_from_pasted(text: str) -> str:
    """Accept the whole address of eBay's sign-in-complete page, or just the code."""
    text = (text or "").strip()
    if "code=" in text:
        q = parse_qs(urlparse(text).query)
        code = (q.get("code") or [""])[0]
        if code:
            return code
    if text and " " not in text and "://" not in text:
        return text
    raise SiteError("Couldn't find the sign-in code in that address")


def parse_item_id(text: str) -> str | None:
    """Item number from '123456789012' or any ebay.com/itm/... link."""
    text = (text or "").strip()
    if text.isdigit():
        return text
    import re
    m = re.search(r"/itm/(?:[^/?#]*/)?(\d{9,15})", text) or re.search(r"[?&]item=(\d{9,15})", text)
    return m.group(1) if m else None


# ---------------------------------------------------------------- parsing

def state_from_browse(item: dict[str, Any], watching: bool, now: float) -> AuctionState:
    """From Browse API getItem / get_item_by_legacy_id."""
    legacy = str(item.get("legacyItemId") or "")
    bid = item.get("currentBidPrice") or item.get("price") or {}
    ends = _dt(item.get("itemEndDate"))
    options = item.get("buyingOptions") or []
    status = STATUS_LIVE if not ends or ends.timestamp() > now else STATUS_ENDED
    bin_price = (item.get("price") or {}).get("value") if "FIXED_PRICE" in options else None
    image = (item.get("image") or {}).get("imageUrl")
    return AuctionState(
        site=SITE,
        auction_id=legacy or str(item.get("itemId") or ""),
        title=item.get("title") or f"eBay item {legacy}",
        url=item.get("itemWebUrl") or f"https://www.ebay.com/itm/{legacy}",
        category=CATEGORY_WATCHING,
        status=status,
        price=_num(bid.get("value")),
        currency=bid.get("currency") or "USD",
        image=image,
        ends_at=ends,
        original_end=ends,
        active_bidders=None,
        pay_if_won=_num(bid.get("value")),
        cost_if_won_now=_num(bid.get("value")),
        buy_it_now=_num(bin_price),
        labels=[x for x in [item.get("condition")] if x],
        extra={
            "winning": False,
            "you_are_bidding": False,
            "total_bids": _int(item.get("bidCount")),
            "min_next_bid": _num((item.get("minimumPriceToBid") or {}).get("value")),
            "auction": "AUCTION" in options,
            "shipping": _num(((item.get("shippingOptions") or [{}])[0].get("shippingCost") or {}).get("value")),
            "seller": (item.get("seller") or {}).get("username"),
            "on_watchlist": watching,
        },
    )


def parse_my_ebay_buying(xml_text: str, my_user_id: str, now: float) -> dict[str, AuctionState]:
    """WatchList + BidList from Trading API GetMyeBayBuying."""
    root = ET.fromstring(xml_text)
    ack = root.findtext("e:Ack", default="", namespaces=NS)
    if ack not in ("Success", "Warning"):
        err = root.findtext(".//e:Errors/e:LongMessage", default="", namespaces=NS) or ack
        if root.findtext(".//e:Errors/e:ErrorCode", default="", namespaces=NS) in ("931", "932", "21917053", "21916984"):
            raise AuthError(f"eBay sign-in no longer valid: {err}")
        raise SiteError(f"eBay said: {err}")
    out: dict[str, AuctionState] = {}
    for list_name, bidding in (("WatchList", False), ("BidList", True)):
        for it in root.findall(f".//e:{list_name}/e:ItemArray/e:Item", NS):
            st = _state_from_trading(it, bidding, my_user_id, now)
            # An item can be on both lists; the bid version knows more
            if st.auction_id not in out or bidding:
                out[st.auction_id] = st
    return out


def parse_user_id(xml_text: str) -> str:
    root = ET.fromstring(xml_text)
    return root.findtext(".//e:User/e:UserID", default="", namespaces=NS)


def _state_from_trading(it: ET.Element, bidding: bool, me: str, now: float) -> AuctionState:
    f = lambda path: it.findtext(path, default="", namespaces=NS)  # noqa: E731
    item_id = f("e:ItemID")
    ends = _dt(f("e:ListingDetails/e:EndTime"))
    high = f("e:SellingStatus/e:HighBidder/e:UserID")
    price = _num(f("e:SellingStatus/e:CurrentPrice")) or _num(f("e:SellingStatus/e:ConvertedCurrentPrice"))
    currency = (it.find("e:SellingStatus/e:CurrentPrice", NS).get("currencyID")
                if it.find("e:SellingStatus/e:CurrentPrice", NS) is not None else "USD")
    winning = bool(me) and high == me
    listing_status = f("e:SellingStatus/e:ListingStatus")
    status = STATUS_ENDED if listing_status in ("Completed", "Ended") or (ends and ends.timestamp() <= now) else STATUS_LIVE
    return AuctionState(
        site=SITE,
        auction_id=item_id,
        title=f("e:Title") or f"eBay item {item_id}",
        url=f("e:ListingDetails/e:ViewItemURL") or f"https://www.ebay.com/itm/{item_id}",
        category=CATEGORY_BIDDING if bidding else CATEGORY_WATCHING,
        status=status,
        price=price,
        currency=currency or "USD",
        image=f("e:PictureDetails/e:GalleryURL") or None,
        leader="you" if winning else (high or None),
        ends_at=ends,
        original_end=ends,
        pay_if_won=price,
        cost_if_won_now=price,
        buy_it_now=_num(f("e:BuyItNowPrice")) or None,
        extra={
            "winning": winning,
            "you_are_bidding": bidding,
            "total_bids": _int(f("e:SellingStatus/e:BidCount")),
            "your_max_bid": _num(f("e:BiddingDetails/e:MaxBid")),
            "shipping": _num(f("e:ShippingDetails/e:ShippingServiceOptions/e:ShippingServiceCost")),
            "on_watchlist": not bidding,
        },
    )


# ---------------------------------------------------------------- client

class EbayClient:
    """App token for item lookups; optional user token (refreshed for ~18 months) for My eBay."""

    def __init__(self, session: aiohttp.ClientSession, app_id: str, cert_id: str,
                 runame: str | None = None, user_auth: dict[str, Any] | None = None) -> None:
        self._session = session
        self.app_id = app_id
        self._cert_id = cert_id
        self.runame = runame
        self.user_auth = dict(user_auth or {})
        self.user_auth_changed = False
        self._app_token: str | None = None
        self._app_expires = 0.0

    @property
    def signed_in(self) -> bool:
        return bool(self.user_auth.get("refresh_token"))

    def _basic(self) -> str:
        return "Basic " + base64.b64encode(f"{self.app_id}:{self._cert_id}".encode()).decode()

    async def _token(self, form: dict[str, str]) -> dict[str, Any]:
        try:
            async with self._session.post(
                API + "/identity/v1/oauth2/token", data=form,
                headers={"Authorization": self._basic(), "Content-Type": "application/x-www-form-urlencoded"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400 or "access_token" not in (data or {}):
                    msg = (data or {}).get("error_description") or (data or {}).get("error") or resp.status
                    raise AuthError(f"eBay refused the keys or sign-in: {msg}")
                return data
        except aiohttp.ClientError as err:
            raise SiteError(f"Couldn't reach eBay: {err}") from err

    async def app_token(self) -> str:
        if not self._app_token or self._app_expires < time.time() + RENEW_EARLY_SECONDS:
            d = await self._token({"grant_type": "client_credentials", "scope": SCOPES})
            self._app_token = d["access_token"]
            self._app_expires = time.time() + float(d.get("expires_in", 7200))
        return self._app_token

    async def exchange_code(self, code: str) -> None:
        if not self.runame:
            raise SiteError("Signing in needs the RuName from your eBay developer account")
        d = await self._token({"grant_type": "authorization_code", "code": code, "redirect_uri": self.runame})
        now = time.time()
        self.user_auth = {
            "access_token": d["access_token"],
            "expires": now + float(d.get("expires_in", 7200)),
            "refresh_token": d.get("refresh_token"),
            "refresh_expires": now + float(d.get("refresh_token_expires_in", 47304000)),
        }
        self.user_auth_changed = True

    async def user_token(self) -> str:
        a = self.user_auth
        if not a.get("refresh_token"):
            raise AuthError("Not signed in to eBay")
        if (a.get("refresh_expires") or 0) < time.time():
            raise AuthError("eBay sign-in expired (they last about 18 months); sign in again")
        if not a.get("access_token") or (a.get("expires") or 0) < time.time() + RENEW_EARLY_SECONDS:
            d = await self._token({"grant_type": "refresh_token", "refresh_token": a["refresh_token"], "scope": SCOPES})
            a["access_token"] = d["access_token"]
            a["expires"] = time.time() + float(d.get("expires_in", 7200))
            self.user_auth_changed = True
        return a["access_token"]

    async def item(self, item_id: str) -> dict[str, Any]:
        token = await self.app_token()
        try:
            async with self._session.get(
                API + "/buy/browse/v1/item/get_item_by_legacy_id", params={"legacy_item_id": item_id},
                headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 429:
                    raise RateLimited("eBay says too many requests")
                if resp.status == 404:
                    raise SiteError(f"eBay item {item_id} not found (ended long ago, or a multi-variation listing)")
                if resp.status >= 400:
                    raise SiteError(f"eBay answered {resp.status} for item {item_id}")
                return await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise SiteError(f"Couldn't reach eBay: {err}") from err

    async def _trading(self, call: str, body: str) -> str:
        token = await self.user_token()
        xml = (f'<?xml version="1.0" encoding="utf-8"?><{call}Request xmlns="urn:ebay:apis:eBLBaseComponents">'
               f"{body}</{call}Request>")
        try:
            async with self._session.post(
                API + "/ws/api.dll", data=xml.encode(),
                headers={"X-EBAY-API-IAF-TOKEN": token, "X-EBAY-API-CALL-NAME": call,
                         "X-EBAY-API-SITEID": "0", "X-EBAY-API-COMPATIBILITY-LEVEL": TRADING_COMPAT,
                         "Content-Type": "text/xml"},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 429:
                    raise RateLimited("eBay says too many requests")
                return await resp.text()
        except aiohttp.ClientError as err:
            raise SiteError(f"Couldn't reach eBay: {err}") from err

    async def my_user_id(self) -> str:
        return parse_user_id(await self._trading("GetUser", ""))

    async def my_ebay(self, my_user_id: str, now: float | None = None) -> dict[str, AuctionState]:
        body = ("<WatchList><Include>true</Include><Pagination><EntriesPerPage>100</EntriesPerPage></Pagination></WatchList>"
                "<BidList><Include>true</Include><Pagination><EntriesPerPage>100</EntriesPerPage></Pagination></BidList>")
        return parse_my_ebay_buying(await self._trading("GetMyeBayBuying", body), my_user_id,
                                    time.time() if now is None else now)


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
