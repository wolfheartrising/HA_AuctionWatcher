"""Parsing and alert rules, on data shaped like real GovAuctions responses."""

from datetime import datetime, timezone

import pytest

from aw.cookies import cookie_header, parse_cookie_input
from aw.logic import Rules, Snapshot, choose_interval, evaluate, is_opportunity
from aw.sites import govauctions as ga
from aw.sites.base import AuthError

NOW = 1_794_268_000.0


def live_json(rows, history=None, logged_in=True):
    return {
        "auctions": rows,
        "auctionsDetails": [{"auctionId": int(k), "history": v} for k, v in (history or {}).items()],
        "p": {"s": 1, "bc": 104} if logged_in else {"s": 0},
    }


STEAK = {"i": 17782125, "s": 1, "r": "82.42", "w": "SnLB78125270", "t": 4, "noNewBidders": True, "x": 876, "bb": {"a": 20, "c": 17}}
DRONE = {"i": 17781916, "s": 1, "r": "94.56", "w": "Tejano1968", "t": 2, "noNewBidders": True, "x": 0}
STEAK_HISTORY = [
    ["82.42", NOW - 2, "SnLB78125270", 0],
    ["82.41", NOW - 6, "RobertCMendes", 0],
    ["82.40", NOW - 11, "SnLB78125270", 0],
    ["82.39", NOW - 90, "someone_old", 0],  # older than 60 s: not active
]
STATIC = {"static": {"17782125": {
    "name": "Omaha Steaks® Holiday Steak Flight", "buyItNowPrice": 716, "productId": 48508,
    "noJumperLimit": 10, "unixStarts": int(NOW - 3600), "exchangeable": True,
    "icons": [{"title": "1¢ Auction"}], "images": {"frontpageImage": "https://img/x.jpg"},
}}}


def test_parse_live_counts_recent_distinct_bidders():
    live, points = ga.parse_live(live_json([STEAK, DRONE], {"17782125": STEAK_HISTORY}), NOW)
    assert points == 104
    s = live["17782125"]
    assert s["active_bidders"] == 2
    assert s["recent_bidders"] == ["SnLB78125270", "RobertCMendes"]
    assert (s["your_bids"], s["your_booked_total"], s["your_booked_left"]) == (876, 20, 17)
    assert live["17781916"]["active_bidders"] is None  # no history requested -> unknown, not 0


def test_logged_out_raises_auth_error():
    with pytest.raises(AuthError):
        ga.parse_live(live_json([], logged_in=False), NOW)


def test_static_and_one_cent_terms():
    static = ga.parse_static(STATIC)["17782125"]
    assert static["title"].startswith("Omaha Steaks")
    assert static["labels"] == ["1¢ Auction"]
    live, _ = ga.parse_live(live_json([STEAK], {"17782125": STEAK_HISTORY}), NOW)
    st = ga.build_state("17782125", live["17782125"], static, None, set(), 0.13, NOW)
    assert st.status == "live" and st.category == "bidding"
    assert st.pay_if_won == 0.01
    assert st.cost_if_won_now == round(0.01 + 876 * 0.13, 2)


@pytest.mark.parametrize("labels,price,bids,expected", [
    ([], 50.0, 100, (50.0, 63.0)),
    (["40% Off"], 50.0, 100, (30.0, 43.0)),
    (["Win-Win"], 50.0, 100, (50.0, 56.5)),
])
def test_win_terms(labels, price, bids, expected):
    assert ga.win_terms(labels, price, bids, 0.13) == expected


def test_history_summary():
    data = {"1": {"previous_winners": [
        {"price": "70.00", "bids_placed": "1000", "estimated_total_cost": 200},
        {"price": "10.00", "bids_placed": "100", "estimated_total_cost": 20},
        {"price": "40.00", "bids_placed": "500"},
    ], "pastWinnersInterval": 30, "pastWinnersIntervalUnit": "day"}}
    h = ga.parse_history(data, "1")
    assert (h.wins, h.median_final_price, h.median_winner_bids, h.period_days) == (3, 40.0, 500, 30)
    assert h.avg_winner_cost == 110.0
    assert ga.parse_history({}, "1") is None


def _states(steak_active=2, steak_bids=876):
    hist = STEAK_HISTORY[:3] if steak_active == 2 else STEAK_HISTORY[:1]
    live, _ = ga.parse_live(live_json([{**STEAK, "x": steak_bids}], {"17782125": hist}), NOW)
    static = ga.parse_static(STATIC)
    return {"17782125": ga.build_state("17782125", live["17782125"], static["17782125"], None, set(), 0.13, NOW)}


def test_opportunity_fires_once_then_again_after_leaving():
    rules = Rules(max_bidders=2)
    events, snap = evaluate(_states(), rules, Snapshot(), [])
    assert [k for k, _ in events] == ["opportunity"]
    events, snap = evaluate(_states(), rules, snap, [])
    assert events == []  # still qualifying: no repeat
    events, snap = evaluate(_states(steak_active=1), Rules(max_bidders=0), snap, [])
    assert events == []  # left the rule
    events, _ = evaluate(_states(), rules, snap, [])
    assert [k for k, _ in events] == ["opportunity"]


def test_no_alert_during_first_long_countdown():
    s = _states()["17782125"]
    assert is_opportunity(s, Rules(max_bidders=2))  # clock 4 s: real bidding phase
    s.clock_seconds = 10594  # ~3 hours left on the first countdown, like the iPad auction
    assert not is_opportunity(s, Rules(max_bidders=2))
    s.clock_seconds = 15
    assert is_opportunity(s, Rules(max_bidders=2))


def test_tracker_catches_people_who_jump_back_in():
    from aw.logic import BidderTracker
    t = BidderTracker()
    # zanenab bids, then pauses long enough to fall out of the site's last-9 list
    t.add("1", [(NOW - 100, "zanenab"), (NOW - 95, "SnLB"), (NOW - 90, "Robert")])
    t.add("1", [(NOW - 10, "SnLB"), (NOW - 5, "Robert")])
    assert t.active("1", NOW, 60) == ["Robert", "SnLB"]             # 60 s window misses zanenab
    assert t.active("1", NOW, 180) == ["Robert", "SnLB", "zanenab"]  # 3 min window counts them
    assert t.active("1", NOW + 200, 180) == []                       # and forgets old ones


def test_stable_seconds_waits_but_zero_alerts_immediately():
    states = _states()
    events, snap = evaluate(states, Rules(max_bidders=2, stable_seconds=0), Snapshot(), [], NOW)
    assert [k for k, _ in events] == ["opportunity"]
    rules = Rules(max_bidders=2, stable_seconds=20)
    events, snap = evaluate(states, rules, Snapshot(), [], NOW)
    assert events == []                                   # qualifies, but only just
    events, snap = evaluate(states, rules, snap, [], NOW + 10)
    assert events == []
    events, snap = evaluate(states, rules, snap, [], NOW + 20)
    assert [k for k, _ in events] == ["opportunity"]      # stayed low for 20 s


def test_manifest_version_matches_const():
    import json
    from pathlib import Path
    from aw.const import VERSION
    manifest = json.loads((Path(__file__).resolve().parent.parent / "custom_components/auction_watcher/manifest.json").read_text())
    assert manifest["version"] == VERSION


def test_closed_auction_needs_you_to_be_in_it():
    s = _states(steak_bids=0)["17782125"]
    assert s.closed_to_new and not is_opportunity(s, Rules(max_bidders=2))


def test_bid_limit_blocks_opportunity_and_alerts_once():
    rules = Rules(max_bidders=2, limits={"17782125": 800})
    events, snap = evaluate(_states(), rules, Snapshot(), [])
    assert [k for k, _ in events] == ["bid_limit"]
    assert events[0][1]["bid_limit"] == 800
    events, _ = evaluate(_states(), rules, snap, [])
    assert events == []


def test_ended_and_won():
    states = _states()
    _, snap = evaluate(states, Rules(), Snapshot(), ["111"])  # old win: no alert
    states["17782125"].status = "ended"
    events, snap = evaluate(states, Rules(), snap, ["111", "17782125"])
    assert sorted(k for k, _ in events) == ["ended", "won"]


def test_polling_speed():
    states = _states()
    assert choose_interval(states, NOW, 10, 45, 60, 1800) == 10
    states["17782125"].status = "upcoming"
    states["17782125"].starts_at = datetime.fromtimestamp(NOW + 600, timezone.utc)
    assert choose_interval(states, NOW, 10, 45, 60, 1800) == 60
    states["17782125"].starts_at = datetime.fromtimestamp(NOW + 7200, timezone.utc)
    assert choose_interval(states, NOW, 10, 45, 60, 1800) == 45 * 60
    assert choose_interval({}, NOW, 10, 45, 60, 1800) == 45 * 60


def test_cookie_formats():
    netscape = (
        "# Netscape HTTP Cookie File\n"
        ".govauctions.com\tTRUE\t/\tTRUE\t0\tA\t1\n"
        "www.govauctions.com\tFALSE\t/\tTRUE\t9999999999\tA\t2\n"
        "#HttpOnly_www.govauctions.com\tFALSE\t/\tTRUE\t9999999999\tREMEMBERME\tx=y\n"
        "www.govauctions.com\tFALSE\t/\tTRUE\t1\tOLD\tz\n"
        "other.com\tFALSE\t/\tFALSE\t0\tNOPE\tq\n"
    )
    c = parse_cookie_input(netscape, domain="govauctions.com", now=NOW)
    assert c == {"A": "2", "REMEMBERME": "x=y"}
    assert parse_cookie_input("Cookie: A=1; B=two=2") == {"A": "1", "B": "two=2"}
    assert cookie_header({"A": "1", "B": "2"}) == "A=1; B=2"


def test_label_notes_explain_special_types():
    assert "1¢" in ga.label_note("1¢ Auction")
    assert "half" in ga.label_note("Win-Win")
    assert "can't bid again" in ga.label_note("No Re-Entry")
    assert "40%" in ga.label_note("40% Off")
    assert ga.label_note("1¢ Auction", "The site's own words") == "The site's own words"
    static = ga.parse_static({"static": {"1": {"name": "X", "icons": [{"title": "1¢ Auction", "message": "Most winners pay 1¢"}],
                                                "noReEntry": True}}})["1"]
    assert static["label_notes"] == {"1¢ Auction": "Most winners pay 1¢", "No Re-Entry": ga.label_note("No Re-Entry")}
