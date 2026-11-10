"""Auto-cancel / auto-rebook rules. These spend real points, so the 'do nothing' cases matter most."""

from dataclasses import replace

from aw.logic import AutoMemory, AutoSettings, Rules, decide_auto
from aw.sites.base import AuctionState

NOW = 1_794_268_000.0
ON = AutoSettings(auto_cancel=True, auto_rebook=True, rebook_mode="fixed", rebook_amount=20)
RULES = Rules(max_bidders=2, limits={"1": 1000})


def auction(**kw) -> AuctionState:
    base = dict(site="govauctions", auction_id="1", title="Steaks", url="u", category="bidding", status="live",
                price=86.0, clock_seconds=4, active_bidders=2, closed_to_new=True, your_bids=876,
                your_booked_left=0, labels=["1¢ Auction"])
    base.update(kw)
    return AuctionState(**base)


def test_off_by_default_and_needs_a_limit():
    assert decide_auto(auction(), AutoSettings(), AutoMemory(), RULES, 500, NOW)[0] == ""
    action, _, why = decide_auto(auction(), ON, AutoMemory(), Rules(max_bidders=2), 500, NOW)
    assert action == "" and "bid limit" in why


def test_crowded_cancels_and_quiet_rebooks_fixed_amount():
    a = auction(active_bidders=4, your_booked_left=17)
    assert decide_auto(a, ON, AutoMemory(), RULES, 500, NOW)[:2] == ("cancel", 17)
    a = auction(active_bidders=2, your_booked_left=0)
    assert decide_auto(a, ON, AutoMemory(cancelled_with=17), RULES, 500, NOW)[:2] == ("book", 20)


def test_rebook_what_was_left():
    s = replace(ON, rebook_mode="remaining")
    assert decide_auto(auction(), s, AutoMemory(cancelled_with=17), RULES, 500, NOW)[:2] == ("book", 17)
    # nothing remembered yet -> falls back to the fixed amount
    assert decide_auto(auction(), s, AutoMemory(), RULES, 500, NOW)[:2] == ("book", 20)


def test_never_past_bid_limit_or_balance():
    assert decide_auto(auction(your_bids=990), ON, AutoMemory(), RULES, 500, NOW)[:2] == ("book", 10)
    action, _, why = decide_auto(auction(your_bids=1000), ON, AutoMemory(), RULES, 500, NOW)
    assert action == "" and "limit" in why
    assert decide_auto(auction(), ON, AutoMemory(), RULES, 7, NOW)[:2] == ("book", 7)
    assert decide_auto(auction(), ON, AutoMemory(), RULES, 0, NOW)[0] == ""


def test_never_cancels_where_it_would_lock_you_out():
    crowded = dict(active_bidders=5, your_booked_left=30)
    action, _, why = decide_auto(auction(labels=["No Re-Entry"], **crowded), ON, AutoMemory(), RULES, 500, NOW)
    assert action == "" and "Re-Entry" in why
    action, _, _ = decide_auto(auction(your_bids=0, **crowded), ON, AutoMemory(), RULES, 500, NOW)
    assert action == ""


def test_waits_out_first_countdown_and_retries_slowly():
    assert decide_auto(auction(clock_seconds=5000), ON, AutoMemory(), RULES, 500, NOW)[0] == ""
    assert decide_auto(auction(), ON, AutoMemory(last_attempt=NOW - 5), RULES, 500, NOW)[0] == ""
    assert decide_auto(auction(), ON, AutoMemory(last_attempt=NOW - 60), RULES, 500, NOW)[0] == "book"


def test_only_rebooks_when_out_and_someone_is_bidding():
    assert decide_auto(auction(your_booked_left=5), ON, AutoMemory(), RULES, 500, NOW)[0] == ""
    assert decide_auto(auction(active_bidders=0), ON, AutoMemory(), RULES, 500, NOW)[0] == ""
    only_cancel = AutoSettings(auto_cancel=True)
    assert decide_auto(auction(), only_cancel, AutoMemory(), RULES, 500, NOW)[0] == ""


def test_topup_is_opt_in_and_counts_booked_bids_against_the_limit():
    s = replace(ON, topup_at=3)
    # off (0): 3 left does nothing, same as before
    assert decide_auto(auction(your_booked_left=3), ON, AutoMemory(), RULES, 500, NOW)[0] == ""
    # on: 3 left -> top up with the fixed amount; 4 left -> not yet
    action, count, why = decide_auto(auction(your_booked_left=3), s, AutoMemory(), RULES, 500, NOW)
    assert (action, count) == ("book", 20) and "topping up" in why
    assert decide_auto(auction(your_booked_left=4), s, AutoMemory(), RULES, 500, NOW)[0] == ""
    # bids still booked count against Max bids: 990 used + 3 booked of 1000 -> room 7
    assert decide_auto(auction(your_bids=990, your_booked_left=3), s, AutoMemory(), RULES, 500, NOW)[:2] == ("book", 7)
    assert decide_auto(auction(your_bids=997, your_booked_left=3), s, AutoMemory(), RULES, 500, NOW)[0] == ""
    # still never tops up when it's crowded, paused or over budget
    crowded = auction(your_booked_left=3, active_bidders=5)
    assert decide_auto(crowded, s, AutoMemory(), RULES, 500, NOW)[0] == "cancel"
    assert decide_auto(auction(your_booked_left=3), s, AutoMemory(), RULES, 500, NOW, paused=True)[0] == ""
    assert decide_auto(auction(your_booked_left=3), s, AutoMemory(), RULES, 500, NOW, budget_left=0)[0] == ""
    assert AutoSettings.from_dict({"topup_at": "3"}).topup_at == 3 and AutoSettings.from_dict({}).topup_at == 0
