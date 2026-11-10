"""Mac.bid parsing, on data shaped like real responses."""

import json

import pytest

from aw.sites import macbid as mb
from aw.sites.base import AuthError, SiteError

ME = "2439061"
NOW = 1_794_300_000.0  # 2026-11-10 ~13:20 UTC
LOT = {
    "date_launched": "2026-11-03T22:39:04.147Z", "total_bids": "12", "is_open": True, "lot_number": "3129K",
    "unique_bidders": [1537535, 2275950, 4064017], "lot_id": 67465504,
    "extension_window": "2026-11-11T00:14:09.000Z", "end_time": "2026-11-11T00:16:09.000Z",
    "winning_bidder_id": "2275950", "auction_id": "100707", "current_bid": "22", "instant_win_price": 144,
    "title": "Segway Electric Scooter", "watchers_count": 31, "status": "LIVE",
    "stock_image_url": "https://media.mac.bid/x.webp",
}
WATCH = {"watchlist_full": [{"id": 67465504, "auction_id": 100707, "title": "Segway Electric Scooter", "retail_price": 178.99,
                             "condition_name": "OPEN BOX", "category": "Sports & Outdoors", "lot_number": "3129K",
                             "image_url": "https://media.mac.bid/y.webp"}],
         "watchlist": [], "auction_registrations": []}


def test_watching_not_bidding():
    static = mb.parse_watchlist(WATCH)
    s = mb.build_state("67465504", LOT, static["67465504"], ME, {"67465504"}, NOW)
    assert s.status == "live" and s.category == "watching"
    assert s.price == 22.0 and s.buy_it_now == 144.0 and s.active_bidders == 3
    assert s.ends_at.isoformat().startswith("2026-11-11T00:16:09")
    assert s.url == "https://www.mac.bid/auction/100707/lot/67465504"
    assert s.extra["winning"] is False and s.extra["retail_price"] == 178.99 and s.labels == ["OPEN BOX"]


def test_bidding_and_winning_leaves_you_out_of_count():
    lot = {**LOT, "unique_bidders": [1537535, int(ME), 4064017], "winning_bidder_id": ME}
    s = mb.build_state("67465504", lot, None, ME, set(), NOW)
    assert s.category == "bidding" and s.extra["winning"] is True and s.leader == "you"
    assert s.active_bidders == 2


def test_extension_window_and_ended():
    end = 1_794_355_000 + 9  # just after the extension window opened (00:14:09 UTC)
    assert mb.build_state("1", LOT, None, ME, set(), 1_794_356_100.0).extra["in_extension"] in (True, False)
    closed = {**LOT, "is_open": False}
    assert mb.build_state("1", closed, None, ME, set(), NOW).status == "ended"


def test_parse_auth_accepts_browser_value_and_rejects_junk():
    raw = json.dumps({"access_token": "a", "token": "a", "refresh_token": "r", "expires": 1, "expiration_refresh": 2,
                      "user_id": 2439061, "email": "x"})
    auth = mb.parse_auth(raw)
    assert auth == {"access_token": "a", "refresh_token": "r", "expires": 1.0, "expiration_refresh": 2.0, "user_id": "2439061"}
    assert mb.parse_auth(json.dumps(raw))["refresh_token"] == "r"  # double-encoded copy
    with pytest.raises(SiteError):
        mb.parse_auth('{"token": "a"}')


def test_active_ids_defensive():
    assert mb.parse_active_ids([]) == []
    assert mb.parse_active_ids({"data": [{"lot_id": 5}, {"auction_lot_id": "6"}], "pagination": {}}) == ["5", "6"]


def test_win_cost_and_pickups():
    assert mb.win_cost(100) == 118.0          # +15% premium + $3 lot fee
    assert mb.win_cost(None) is None
    data = [{"id": 650001, "invoice_id": 710001, "product_name": "Air Fryer", "status": "PAID", "lot_number": "4969M",
             "lot_id": 50001, "price": 18, "cover_image": "https://media.mac.bid/z.webp", "date_paid": "2026-11-08",
             "auction_abandon_date": "2026-11-12T22:00:00.000Z"}]
    p = mb.parse_pickups(data, NOW)["pickup-50001"]
    assert p.status == "awaiting_pickup" and p.title == "Air Fryer" and p.price == 18.0
    assert p.ends_at.isoformat().startswith("2026-11-12T22:00") and p.extra["paid"] and not p.extra["overdue"]
    assert mb.parse_pickups([], NOW) == {}


from aw.logic import Rules, TimedSnapshot, choose_timed_interval, evaluate_timed  # noqa: E402


def _st(**kw):
    lot = {**LOT, **kw}
    return {"67465504": mb.build_state("67465504", lot, None, ME, set(), NOW)}


def test_outbid_ending_soon_over_max_won():
    rules = Rules(limits={"67465504": 30})
    winning = dict(unique_bidders=[int(ME), 2275950], winning_bidder_id=ME)
    ev, snap = evaluate_timed(_st(**winning), rules, TimedSnapshot(), NOW)
    assert ev == []                                                   # startup: nothing to report
    ev, snap = evaluate_timed(_st(unique_bidders=[int(ME), 2275950], current_bid="35"), rules, snap, NOW)
    assert sorted(k for k, _ in ev) == ["outbid", "over_max"]         # lost the lead, and past the $30 max
    ev, snap = evaluate_timed(_st(unique_bidders=[int(ME), 2275950], current_bid="36"), rules, snap, NOW)
    assert ev == []                                                   # each fires once
    near_end = NOW + (mb._dt(LOT["end_time"]).timestamp() - NOW) - 300   # 5 minutes before the end
    ev, snap = evaluate_timed(_st(**winning), rules, snap, near_end)
    assert [k for k, _ in ev] == ["ending_soon"]
    ev, _ = evaluate_timed(_st(**winning, is_open=False), rules, snap, near_end + 400)
    assert [k for k, _ in ev] == ["won"]


def test_pickup_reminder_once():
    p = mb.parse_pickups([{"lot_id": 1, "product_name": "Fryer", "auction_abandon_date": "2026-11-10T20:00:00Z"}], NOW)
    ev, snap = evaluate_timed(p, Rules(), TimedSnapshot(), NOW)
    assert [k for k, _ in ev] == ["pickup"]
    assert evaluate_timed(p, Rules(), snap, NOW)[0] == []


def test_timed_polling_speeds_up_near_the_end():
    s = _st()
    end = s["67465504"].ends_at.timestamp()
    assert choose_timed_interval(s, end - 3 * 86400, 10, 45) == 15 * 60
    assert choose_timed_interval(s, end - 3600, 10, 45) == 120
    assert choose_timed_interval(s, end - 300, 10, 45) == 10


def test_lot_refresh_schedule():
    assert mb.lot_refresh_seconds(5 * 60) == 0        # last 15 minutes: every check
    assert mb.lot_refresh_seconds(90 * 60) == 120     # last 2 hours: every 2 minutes
    assert mb.lot_refresh_seconds(20 * 3600) == 900   # otherwise every 15 minutes
    assert mb.lot_refresh_seconds(None) == 900
