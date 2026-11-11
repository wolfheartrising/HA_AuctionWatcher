"""OLA handler: tolerant parsing of the read endpoints, plus the SignalR bidding transport."""

import asyncio
import json
import time

import aiohttp

from aw.sites import ola
from aw.sites.base import AuthError


def test_parse_tokens_variants():
    t = ola.parse_tokens({"data": {"accessToken": "a", "refreshToken": "r", "accessTokenExpiresIn": 1800, "refreshTokenExpiresIn": 999999, "userId": 7}})
    assert t["access_token"] == "a" and t["refresh_token"] == "r" and t["user_id"] == "7"
    assert t["expires"] > time.time() + 1000 and t["refresh_expires"] > time.time() + 900000
    t2 = ola.parse_tokens({"data": {"access_token": "x", "refresh_token": "y"}})
    assert t2["access_token"] == "x"
    try:
        ola.parse_tokens({"message": "bad password"})
        assert False
    except AuthError as e:
        assert "bad password" in str(e)


def test_item_state_common_fields():
    now = 1_700_000_000.0
    raw = {"id": 15717458, "name": "Dewalt Drill", "currentBidAmount": 12.5, "nextBidAmount": 13.5,
           "auctionEndsAt": "2099-01-01T00:00:00", "previewBlobUrl": "https://img/x.jpg",
           "currentBidderId": 296195, "retailPrice": 199.0, "locationName": "Stow"}
    st = ola.item_state(raw, "bidding", now, my_id="296195")
    assert st.auction_id == "15717458" and st.title == "Dewalt Drill" and st.price == 12.5
    assert st.status == "live" and st.extra["winning"] and st.leader == "you"
    assert st.extra["min_next_bid"] == 13.5 and st.extra["retail_price"] == 199.0
    assert st.url == "https://link.olabid.com/item-details?id=15717458"
    assert st.image == "https://img/x.jpg" and st.extra["location"] == "Stow"


def test_item_state_alternate_names_and_ended():
    now = 1_700_000_000.0
    raw = {"auctionItemId": "99", "productName": "Mixer", "highBid": "$8.00",
           "auctionEndsAt": 1_600_000_000, "images": [{"url": "https://img/y.jpg"}]}
    st = ola.item_state(raw, "watching", now)
    assert st.auction_id == "99" and st.title == "Mixer" and st.price == 8.0
    assert st.status == "ended" and st.image == "https://img/y.jpg" and st.extra["on_watchlist"]


def test_as_list_wrappers_and_build_states():
    now = 1_700_000_000.0
    watch = ola.parse_items({"data": [{"id": "1", "title": "A"}, {"id": "2", "title": "B"}]}, "watching", now)
    win = ola.parse_items([{"id": "2", "title": "B", "isWinning": True}], "bidding", now)
    hist = ola.parse_items({"results": {"items": [{"id": "3", "title": "C"}]}}, "bidding", now)
    assert set(watch) == {"1", "2"} and set(win) == {"2"} and set(hist) == {"3"}
    merged = ola.build_states(watch, win, hist)
    assert set(merged) == {"1", "2", "3"} and merged["2"].extra["winning"] and merged["2"].category == "bidding"


def test_search_hits_map_to_hit_fields():
    from aw.searches import Hit
    data = {"pageContext": {}, "items": [
        {"id": 15569243, "name": "WYZE Cam v4", "currentBidAmount": 3.0, "auctionEndsAt": "2026-11-11T15:33:30",
         "previewBlobUrl": "https://content-prod.olabid.com/x.jpg", "status": "Active"}]}
    hits = ola.search_hits(data)
    assert len(hits) == 1
    h = hits[0]
    assert h["item_id"] == "15569243" and h["title"] == "WYZE Cam v4" and h["price"] == 3.0
    assert h["url"] == "https://link.olabid.com/item-details?id=15569243" and h["image"].endswith("x.jpg")
    assert h["ends_at"].startswith("2026-11-11")
    Hit(site="ola", **h)  # constructs without error


def test_keyword_helpers_shape():
    data = {"items": [{"id": 731361, "name": "milwaukee", "isActive": True},
                      {"id": 849294, "name": "segway", "isActive": True}]}
    out = [{"id": str(ola._first(k, "id") or ""), "name": ola._first(k, "name", "keyword") or ""} for k in ola._as_list(data)]
    assert out == [{"id": "731361", "name": "milwaukee"}, {"id": "849294", "name": "segway"}]


def test_prune_old_ended_keeps_recent_and_watchlist():
    from datetime import datetime, timezone
    now = 1_700_000_000.0
    old_end = datetime.fromtimestamp(now - 10 * 86400, timezone.utc)
    recent_end = datetime.fromtimestamp(now - 86400, timezone.utc)

    old_hist = ola.item_state({"id": "1", "name": "Old", "auctionEndsAt": old_end.isoformat(), "status": "Sold"}, "bidding", now)
    recent_hist = ola.item_state({"id": "2", "name": "Recent", "auctionEndsAt": recent_end.isoformat(), "status": "Sold"}, "bidding", now)
    old_watch = ola.item_state({"id": "3", "name": "Watched old", "auctionEndsAt": old_end.isoformat(), "status": "Sold"}, "watching", now)
    live = ola.item_state({"id": "4", "name": "Live", "auctionEndsAt": "2099-01-01T00:00:00"}, "bidding", now)

    pruned = ola.prune_old_ended({"1": old_hist, "2": recent_hist, "3": old_watch, "4": live}, now, keep_days=3)
    assert set(pruned) == {"2", "3", "4"}


# ---------------------------------------------------------------- bidding transport

def test_is_valid_bid_whole_dollars_at_least_min():
    assert ola.is_valid_bid(13, 13) and ola.is_valid_bid(20, 13)
    assert not ola.is_valid_bid(12, 13)      # below OLA's next minimum
    assert not ola.is_valid_bid(13.5, 13)    # OLA bids in whole dollars
    assert not ola.is_valid_bid(0, 1)
    assert ola.is_valid_bid(5, None)         # no minimum known: any positive whole dollar
    assert not ola.is_valid_bid("nope", 1)


def test_split_records_and_json_or_none():
    sep = ola.RECORD_SEP
    assert ola._split_records("a" + sep + "b" + sep) == ["a", "b"]
    assert ola._json_or_none('{"x":1}') == {"x": 1}
    assert ola._json_or_none("not json") is None


class _ACM:
    def __init__(self, obj):
        self._obj = obj

    async def __aenter__(self):
        return self._obj

    async def __aexit__(self, *a):
        return False


class _Resp:
    def __init__(self, status, data):
        self.status = status
        self._data = data

    async def json(self, content_type=None):
        return self._data


class _Msg:
    def __init__(self, mtype, data=""):
        self.type = mtype
        self.data = data


class _WS:
    def __init__(self, incoming):
        self._in = list(incoming)
        self.sent = []

    async def send_str(self, s):
        self.sent.append(s)

    async def receive(self, timeout=None):
        return self._in.pop(0) if self._in else _Msg(aiohttp.WSMsgType.CLOSED)


class _Session:
    def __init__(self, negotiate, ws):
        self._neg = negotiate
        self._ws = ws
        self.ws_url = None

    def post(self, url, headers=None, timeout=None):
        return _ACM(_Resp(200, self._neg))

    def ws_connect(self, url, headers=None, heartbeat=None, timeout=None):
        self.ws_url = url
        return _ACM(self._ws)


def test_signalr_invoke_handshake_then_result():
    sep = ola.RECORD_SEP
    ws = _WS([
        _Msg(aiohttp.WSMsgType.TEXT, "{}" + sep),                                          # handshake ack
        _Msg(aiohttp.WSMsgType.TEXT, json.dumps({"type": 6}) + sep),                        # ping, ignored
        _Msg(aiohttp.WSMsgType.TEXT, json.dumps({"type": 3, "invocationId": "1", "result": {"ok": True}}) + sep),
    ])
    sess = _Session({"connectionToken": "ctok"}, ws)
    hub = ola.OlaSignalR(sess, "https://bidding-api.olabid.com/bidding-hub", "acc", "key", "btok")
    res = asyncio.run(hub.invoke("ChangeMaxBid", [{"maxBid": 25}]))
    assert res == {"ok": True}
    assert json.loads(ws.sent[0].rstrip(sep)) == {"protocol": "json", "version": 1}
    inv = json.loads(ws.sent[1].rstrip(sep))
    assert inv["type"] == 1 and inv["target"] == "ChangeMaxBid" and inv["arguments"] == [{"maxBid": 25}]
    assert sess.ws_url.startswith("wss://") and "id=ctok" in sess.ws_url and "access_token=btok" in sess.ws_url


def test_signalr_invoke_raises_on_completion_error():
    sep = ola.RECORD_SEP
    ws = _WS([
        _Msg(aiohttp.WSMsgType.TEXT, "{}" + sep),
        _Msg(aiohttp.WSMsgType.TEXT, json.dumps({"type": 3, "invocationId": "1", "error": "bid too low"}) + sep),
    ])
    sess = _Session({"connectionId": "c"}, ws)
    hub = ola.OlaSignalR(sess, "https://h/bidding-hub", "acc", "key")
    try:
        asyncio.run(hub.invoke("ChangeMaxBid", []))
        assert False
    except ola.SiteError as e:
        assert "bid too low" in str(e)
