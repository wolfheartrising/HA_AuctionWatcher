"""OLA read-only handler: tolerant parsing of the documented read endpoints."""

import time

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
