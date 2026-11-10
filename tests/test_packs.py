from datetime import datetime, timezone

from aw.packs import entry_open, pack_stats, parse_points, score_pack, stars_for, suggest_max

UTC = timezone.utc


def ts(hour):
    return datetime(2026, 11, 3, hour, 30, tzinfo=UTC).timestamp()


NIGHT = [1200, 1500, 900, 2600, 1100, 1300, 1250, 1000]
MORNING = [300, 450, 500, 200, 600, 420, 480, 380]
WINS = [{"timestamp": ts(2), "bids_placed": b} for b in NIGHT] + [{"timestamp": ts(9), "bids_placed": b} for b in MORNING]


def test_parse_points():
    assert parse_points("2500 Point Pack!") == 2500
    assert parse_points("1,000 Points Pack") == 1000
    assert parse_points("+2X Time as Highest Multiplier") is None
    assert parse_points("$300 Walmart Gift Card + 660 Points") is None


def test_stats():
    s = pack_stats(WINS, 2500, UTC)
    assert s["wins"] == 16 and s["blocks"]["night"]["median"] == 1225 and s["blocks"]["morning"]["median"] == 435
    assert s["lost_money"] == round(1 / 16, 2)


def test_grade_is_cautious_about_when_it_ends():
    s = pack_stats(WINS, 2500, UTC)
    # starts in the morning and may run into the afternoon (no data there): the morning median counts
    morning = score_pack(2500, s, ts(10), UTC)
    assert (morning["grade"], morning["expected_bids"]) == ("green", 435) and morning["cheapest_block"] == "morning"
    # starts in the evening, likely ends overnight: the worse (night) median counts
    evening = score_pack(2500, s, ts(21), UTC)
    assert (evening["grade"], evening["expected_bids"]) == ("yellow", 1225)


def test_too_little_history_gets_no_grade():
    few = pack_stats(WINS[8:12], 2500, UTC)
    assert score_pack(2500, few, ts(10), UTC)["grade"] == "unknown"
    assert score_pack(2500, None, ts(3), UTC)["grade"] == "unknown"
    assert suggest_max(2500, WINS[8:12]) is None


def test_suggested_max():
    s = suggest_max(2500, WINS)
    # 10 of these 16 past wins needed <= 1000 bids; half the pack is 1250 -> suggest 1000
    assert s["max"] == 1000 and s["net_win"] == 1500
    assert 0.55 <= s["cover"] <= 0.7
    # a small pack whose winners usually need most of it: capped at half, low coverage
    tight = [{"timestamp": ts(9), "bids_placed": b} for b in (180, 200, 220, 240, 150, 260, 190, 210, 230, 170, 250, 205, 215, 195, 225)]
    t = suggest_max(250, tight)
    assert t["max"] == 120 and t["cover"] == 0.0


def test_stars():
    assert [stars_for(r) for r in (0.2, 0.4, 0.55, 0.8, None)] == [3, 2, 1, 0, None]


def test_entry_window():
    assert entry_open(0.53, 5, False) == (True, 447)
    assert entry_open(5.01, 5, False) == (False, 0)
    assert entry_open(1.0, 5, True) == (False, 0)
