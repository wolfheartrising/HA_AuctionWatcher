"""Weekly budget, pause switch, shared balance, spending ledger, points at risk, bundles."""

from datetime import datetime, timezone

from aw.logic import (
    AutoMemory,
    AutoSettings,
    Rules,
    decide_auto,
    points_at_risk,
    record_spending,
    spent_since,
    week_start,
)
from aw.sites import govauctions as ga
from aw.sites.base import AuctionState

NOW = 1_794_268_000.0
ON = AutoSettings(auto_cancel=True, auto_rebook=True, rebook_amount=20)
RULES = Rules(max_bidders=2, limits={"1": 1000, "2": 300})


def a(aid="1", **kw) -> AuctionState:
    base = dict(site="govauctions", auction_id=aid, title="x", url="u", category="bidding", status="live",
                clock_seconds=4, active_bidders=1, your_bids=900, your_booked_left=0, labels=[])
    base.update(kw)
    return AuctionState(**base)


def test_pause_stops_booking_but_not_cancelling():
    assert decide_auto(a(), ON, AutoMemory(), RULES, 500, NOW, paused=True)[0] == ""
    crowded = a(active_bidders=5, your_booked_left=12)
    assert decide_auto(crowded, ON, AutoMemory(), RULES, 500, NOW, paused=True)[:2] == ("cancel", 12)


def test_budget_caps_and_stops_booking():
    assert decide_auto(a(), ON, AutoMemory(), RULES, 500, NOW, budget_left=7)[:2] == ("book", 7)
    action, _, why = decide_auto(a(), ON, AutoMemory(), RULES, 500, NOW, budget_left=0)
    assert action == "" and "budget" in why


def test_shared_balance_two_auctions_cannot_double_spend():
    # what the coordinator does: book in order, taking from one running balance
    available = 25
    first = decide_auto(a("1"), ON, AutoMemory(), RULES, available, NOW)
    available -= first[1]
    second = decide_auto(a("2", your_bids=0), ON, AutoMemory(), RULES, available, NOW)
    assert first[:2] == ("book", 20) and second[:2] == ("book", 5)


def test_ledger_counts_bids_placed():
    ledger, last = [], {}
    assert record_spending(ledger, last, {"1": a(your_bids=900)}, NOW) is False  # first sight: baseline only
    record_spending(ledger, last, {"1": a(your_bids=910)}, NOW + 60)
    record_spending(ledger, last, {"1": a(your_bids=910)}, NOW + 120)
    record_spending(ledger, last, {"1": a(your_bids=925)}, NOW + 180)
    assert spent_since(ledger, NOW) == 25 and spent_since(ledger, NOW + 150) == 15


def test_week_starts_monday():
    wed = datetime(2026, 11, 11, 15, 0, tzinfo=timezone.utc)
    assert datetime.fromtimestamp(week_start(wed), timezone.utc) == datetime(2026, 11, 9, tzinfo=timezone.utc)


def test_points_at_risk():
    states = {"1": a("1", your_bids=900, your_booked_left=12), "2": a("2", your_bids=50), "3": a("3", status="ended", your_booked_left=9)}
    risk = points_at_risk(states, {"1": ON, "2": AutoSettings(auto_cancel=True)}, RULES)
    # auction 1: 12 booked, auto-rebook could still book 1000-900-12 = 88; auction 2 has no auto-rebook; 3 ended
    assert risk["booked"] == 12 and risk["earmarked"] == 88


def test_bundle_points():
    assert ga.bundle_points("DJI Mini 5 Pro ... + 2300 Points") == 2300
    assert ga.bundle_points("$300 Walmart Gift Card + 660 Points") == 660
    assert ga.bundle_points("2500 Point Pack!") is None
    static = ga.parse_static({"static": {"9": {"name": "Drone + 2300 Points", "icons": [{"title": "1¢ Auction"}]}}})["9"]
    st = ga.build_state("9", {"live": True, "price": 90.0, "your_bids": 100}, static, None, set(), 0.13, NOW)
    assert st.extra["bundle_points"] == 2300 and st.extra["bundle_value"] == 299.0
    assert st.extra["net_cost_if_won_now"] == round(13.01 - 299.0, 2)
