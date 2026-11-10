"""One spending cap across all sites: weekly or monthly, block or warn."""

from datetime import datetime

from aw.logic import SpendCap, period_start, record_wins, wins_since
from aw.sites.base import AuctionState


def test_period_start_week_and_month():
    now = datetime(2026, 11, 12, 15, 30)  # a Thursday
    assert datetime.fromtimestamp(period_start(now, "week")) == datetime(2026, 11, 9)
    assert datetime.fromtimestamp(period_start(now, "month")) == datetime(2026, 11, 1)


def test_block_vs_warn():
    assert SpendCap(100, "week", "block").left(30) == 70
    assert SpendCap(100, "week", "block").left(130) == -30
    assert SpendCap(100, "week", "warn").left(130) is None      # warn never blocks
    assert SpendCap(0).left(500) is None                          # no cap
    assert SpendCap(100, mode="warn").reached(100) and not SpendCap(100).reached(99.99)
    assert not SpendCap(0).reached(1000)


def test_macbid_wins_counted_once_from_when_they_show_up():
    def st(aid, status, price):
        return AuctionState(site="macbid", auction_id=aid, title=aid, url="u", category="bidding", status=status, price=price)
    wins = {}
    assert record_wins(wins, {"a": st("a", "awaiting_pickup", 40.0), "b": st("b", "live", 9.0)}, 1000)
    assert not record_wins(wins, {"a": st("a", "awaiting_pickup", 41.0)}, 2000)   # same win, not again
    record_wins(wins, {"c": st("c", "awaiting_pickup", 15.5)}, 3000)
    assert wins_since(wins, 0) == 55.5 and wins_since(wins, 2500) == 15.5
