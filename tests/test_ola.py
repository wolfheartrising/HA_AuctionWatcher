"""OLA read-only handler: tolerant parsing of the documented read endpoints."""

import time

from aw.sites import ola
from aw.sites.base import AuthError


def test_parse_tokens_variants():
    t = ola.parse_tokens({"accessToken": "a", "refreshToken": "r", "expiresIn": 1800, "userId": 7})
    assert t["access_token"] == "a" and t["refresh_token"] == "r" and t["user_id"] == "7"
    assert t["expires"] > time.time() + 1000
    t2 = ola.parse_tokens({"data": {"access_token": "x", "refresh_token": "y"}})
    assert t2["access_token"] == "x"
    try:
        ola.parse_tokens({"message": "bad password"})
        assert False
    except AuthError as e:
        assert "bad password" in str(e)


def test_item_state_common_fields():
    now = 1_700_000_000.0
    raw = {"id": 15717458, "title": "Dewalt Drill", "currentBid": 12.5, "myMaxBid": 20,
           "endDate": "2099-01-01T00:00:00Z", "imageUrl": "https://img/x.jpg", "bidCount": 4,
           "isWinning": True, "retailPrice": 199.0, "locationName": "Stow"}
    st = ola.item_state(raw, "bidding", now)
    assert st.auction_id == "15717458" and st.title == "Dewalt Drill" and st.price == 12.5
    assert st.status == "live" and st.extra["winning"] and st.leader == "you"
    assert st.extra["your_max_bid"] == 20 and st.extra["total_bids"] == 4 and st.extra["retail_price"] == 199.0
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
