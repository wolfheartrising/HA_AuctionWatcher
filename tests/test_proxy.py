"""Bidding for someone else: nothing is bid until they've named a max AND agreed to pay."""

from aw.proxy import (ASKED, CONFIRMED, CONFIRMING, DECLINED, ProxyRequest, ask_text, confirm_text,
                      on_agree, on_amount, on_bids, on_decline, parse_amount, parse_gov_reply, result_text, watch_text,
                      gov_confirm_text)


def req(**kw):
    return ProxyRequest(lot_id="1", person_id="p", hard_max=60, title="Bluey ship", **kw)


def test_parse_amount():
    assert parse_amount("45") == 45 and parse_amount("$45") == 45 and parse_amount("max 60.50") == 60
    assert parse_amount("yes please") is None


def test_needs_a_max_then_agreement_and_never_past_your_hard_max():
    r = req()
    assert on_agree(r, 50.0) == 0 and r.state == ASKED                     # no max named yet: nothing
    ok, why = on_amount(r, 75, current_price=10)
    assert not ok and "$60" in why and r.state == ASKED                     # above your hard max
    ok, why = on_amount(r, 9, current_price=10)
    assert not ok and "at least $11" in why                                  # below the current bid
    ok, _ = on_amount(r, 40, current_price=10)
    assert ok and r.state == CONFIRMING and r.bid_amount == 0               # named, not agreed: still nothing
    assert on_agree(r, 49.0) == 40 and r.state == CONFIRMED and r.agreed_max == 40
    assert any("agreed to pay up to $40" in t for _, t in r.log)


def test_raise_after_agreeing_needs_a_new_agreement():
    r = req()
    on_amount(r, 40, 10); on_agree(r, None)
    ok, why = on_amount(r, 35, 20)
    assert not ok and "already agreed to $40" in why
    ok, _ = on_amount(r, 55, 20)
    assert ok and r.state == CONFIRMED and r.their_max == 55 and r.bid_amount == 40   # still $40 until they agree
    on_decline(r)                                                           # said no to the raise
    assert r.state == CONFIRMED and r.bid_amount == 40 and r.their_max == 40
    on_amount(r, 55, 20)
    assert on_agree(r, None) == 55


def test_decline_and_texts():
    r = req()
    on_decline(r)
    assert r.state == DECLINED
    assert "up to $60" in ask_text("Sam", "Bluey ship", "u", 10, 60, None)
    assert "can't be withdrawn" in confirm_text("Bluey ship", 40, 49.0) and "$49.00" in confirm_text("Bluey ship", 40, 49.0)
    r2 = req(); on_amount(r2, 40, 10); on_agree(r2, None)
    assert "outbid" in result_text("outbid", "Bluey ship", {"price": 41}, r2)
    assert "don't owe" in result_text("ended", "Bluey ship", {}, r2)
    assert result_text("pickup", "x", {}, r2) is None


def test_just_watching_never_bids_and_only_sends_updates():
    r = ProxyRequest(lot_id="9", person_id="p", hard_max=0, site="govauctions", mode="watch", state="watching")
    assert r.key == "govauctions:9" and r.bid_amount == 0
    assert "Nothing will be bid" in watch_text("Sam", "TV", "u", 3.5)
    assert "ends soon" in result_text("ending_soon", "TV", {"price": 4.0}, r)
    assert "ended at $12.00" in result_text("won", "TV", {"price": 12.0}, r)
    assert result_text("outbid", "TV", {"price": 4.0}, r) is None and result_text("bid_placed", "TV", {}, r) is None


def test_govauctions_reply_bids_and_optional_price():
    assert parse_gov_reply("40") == (40, None)
    assert parse_gov_reply("40 $25") == (40, 25.0) and parse_gov_reply("$25.50 for 40 bids") == (40, 25.5)


def test_govauctions_they_pay_for_bids_win_or_lose():
    r = ProxyRequest(lot_id="5", person_id="p", hard_max=50, site="govauctions", point_cost=0.13, title="TV")
    ok, why = on_bids(r, 80, None, 3.0)
    assert not ok and "50 bids" in why
    ok, why = on_bids(r, 40, 2.0, 3.0)
    assert not ok and "higher than that" in why
    ok, _ = on_bids(r, 40, 25.0, 3.0)
    assert ok and r.state == CONFIRMING and r.bid_amount == 0
    assert "win or lose" in gov_confirm_text("TV", 40, 0.13, 25.0) and "$5.20" in gov_confirm_text("TV", 40, 0.13, 25.0)
    r.start_bids = 12
    assert on_agree(r, None) == 40 and r.agreed_price == 25.0
    assert "$5.20" in r.log[-1][1]
    assert "3 bids used ($0.39)" in result_text("ended", "TV", {"your_bids": 15}, r)
    assert "40 bids used ($5.20)" in result_text("ended", "TV", {"your_bids": 99}, r)       # never more than agreed
    assert "plus the final price $14.20" in result_text("won", "TV", {"your_bids": 20, "pay_if_won": 14.2}, r)


def test_stop_words_and_stopped_requests():
    from aw.proxy import gov_used, wants_stop
    assert wants_stop("Stop") and wants_stop(" cancel! ") and not wants_stop("stop at 40")
    r = req(); on_amount(r, 40, 10); on_agree(r, None)
    r.stopped = True
    ok, why = on_amount(r, 55, 20)
    assert not ok and "cancelled" in why                      # no raises after stopping
    assert result_text("outbid", "Bluey ship", {"price": 41}, r) is None
    assert "won" in result_text("won", "Bluey ship", {"price": 38.0}, r)   # but still hears how it ends
    g = ProxyRequest(lot_id="5", person_id="p", hard_max=50, site="govauctions", start_bids=10, agreed_max=20)
    assert gov_used(g, 14) == 4 and gov_used(g, 99) == 20 and gov_used(g, 3) == 0
