"""Mac.bid max-bid rules: when to place your max, and when not to."""

from datetime import datetime, timezone

from aw.logic import MacBidMemory, MacBidSettings, decide_macbid, macbid_committed
from aw.sites.base import AuctionState

END = datetime(2026, 11, 11, 0, 16, 9, tzinfo=timezone.utc)
E = END.timestamp()
NOW_SET = MacBidSettings(mode="now")
LATE = MacBidSettings(mode="late", late_seconds=180)


def lot(**kw):
    base = dict(site="macbid", auction_id="1", title="Segway", url="u", category="watching", status="live",
                price=22.0, ends_at=END, extra={"winning": False})
    base.update(kw)
    return AuctionState(**base)


def test_off_and_no_max_do_nothing():
    assert decide_macbid(lot(), MacBidSettings(), MacBidMemory(), 40, E - 9999)[0] == ""
    assert "max" in decide_macbid(lot(), NOW_SET, MacBidMemory(), 0, E - 9999)[2]


def test_now_places_max_once():
    assert decide_macbid(lot(), NOW_SET, MacBidMemory(), 40, E - 9999)[:2] == ("place", 40)
    # placed $40, then outbid: someone's max is higher -> don't keep throwing bids
    action, _, why = decide_macbid(lot(price=41.0), NOW_SET, MacBidMemory(placed=40), 40, E - 9999)
    assert action == "" and "past your max" in why
    action, _, why = decide_macbid(lot(price=35.0), NOW_SET, MacBidMemory(placed=40), 40, E - 9999)
    assert action == "" and "outbid" in why
    # you raised your max after being outbid -> place the new one
    assert decide_macbid(lot(price=35.0), NOW_SET, MacBidMemory(placed=40), 50, E - 9999)[:2] == ("place", 50)


def test_late_waits_until_the_window():
    action, _, why = decide_macbid(lot(), LATE, MacBidMemory(), 40, E - 3600)
    assert action == "" and "3 min before the end" in why
    assert decide_macbid(lot(), LATE, MacBidMemory(), 40, E - 120)[:2] == ("place", 40)


def test_winning_paused_budget_retry():
    assert decide_macbid(lot(extra={"winning": True}), NOW_SET, MacBidMemory(), 40, E - 99)[0] == ""
    assert "switched off" in decide_macbid(lot(), NOW_SET, MacBidMemory(), 40, E - 99, paused=True)[2]
    assert "budget" in decide_macbid(lot(), NOW_SET, MacBidMemory(), 40, E - 99, budget_left=30)[2]
    assert decide_macbid(lot(), NOW_SET, MacBidMemory(last_attempt=E - 110), 40, E - 99)[0] == ""


def test_committed_dollars():
    states = {"1": lot(auction_id="1", extra={"winning": True}), "2": lot(auction_id="2", price=10.0),
              "3": lot(auction_id="3", price=60.0), "4": lot(auction_id="4")}
    c = macbid_committed(states, {"1": 40, "2": 25, "3": 50}, {"4": 30}, {"4": "late"})
    # 1 winning at 40, 2 still in at 25 (price 10 < 25), 3 lost (price 60 > 50), 4 scheduled late at 30
    assert c == {"placed": 65, "scheduled": 30}



def test_stops_after_two_unrecorded_bids_instead_of_retrying_forever():
    from aw.logic import MB_MAX_FAILURES, MacBidMemory, MacBidSettings, decide_macbid
    from aw.sites.base import AuctionState
    s = AuctionState(site="macbid", auction_id="1", title="x", url="u", category="bidding", status="live", price=20.0)
    mem = MacBidMemory(failures=MB_MAX_FAILURES)
    action, _, why = decide_macbid(s, MacBidSettings(mode="now"), mem, 50, 1_000_000.0)
    assert action == "" and "stopped after 2" in why
    mem.failures = 1
    assert decide_macbid(s, MacBidSettings(mode="now"), mem, 50, 1_000_000.0)[0] == "place"



def test_bid_steps_match_the_websites_dropdown():
    from aw.sites import macbid as mb
    assert mb.next_valid_bid(110) == 115 and mb.next_valid_bid(116) == 120 and mb.next_valid_bid(97) == 98
    assert mb.next_valid_bid(100) == 105 and mb.next_valid_bid(500) == 510 and mb.next_valid_bid(1000) == 1050
    assert mb.valid_bids(116, 4) == [120, 125, 130, 135] and mb.valid_bids(98, 4) == [99, 100, 105, 110]
    assert not mb.is_valid_bid(111) and mb.is_valid_bid(115) and mb.is_valid_bid(99) and not mb.is_valid_bid(515)
    assert mb.round_down_bid(111) == 110 and mb.round_down_bid(118.9) == 115 and mb.round_down_bid(1049) == 1000
    assert mb.round_down_bid(73) == 73
    assert mb.lot_now({"current_bid": 116}, "7")["min_next"] == 120


def test_for_someone_needs_an_allowed_amount():
    from aw.proxy import ProxyRequest, on_amount
    r = ProxyRequest(lot_id="1", person_id="p", hard_max=150, title="Oboe")
    ok, why = on_amount(r, 111, 110, min_next=115)
    assert not ok
    ok, why = on_amount(r, 118, 110, min_next=115)
    assert not ok and "$115 or $120" in why
    assert on_amount(r, 120, 110, min_next=115)[0]



def test_leader_can_lower_max_to_the_current_price_only():
    from aw.sites import macbid as mb
    leading = {"price": 116.0, "winning": True}
    assert mb.lowering_to_price(116, leading)                       # the "retract to current price" move
    assert not mb.lowering_to_price(117, leading)                   # anything else follows the normal rules
    assert not mb.lowering_to_price(116, {"price": 116.0, "winning": False})   # only while you lead
