"""Alert wording and routing to phone push, Telegram and Alexa."""

from aw.alerts import build_calls, format_alert

DATA = {
    "auction_id": "17782125",
    "title": "Omaha Steaks Holiday Steak Flight",
    "url": "https://www.govauctions.com/auction/17782125",
    "price": 82.42,
    "leader": "SnLB78125270",
    "active_bidders": 2,
    "recent_bidders": ["SnLB78125270", "RobertCMendes"],
    "your_bids": 876,
    "your_points_cost": 113.88,
    "your_booked_left": 17,
    "bid_limit": 1000,
    "buy_it_now": 716,
    "median_winner_bids": 1011,
}


def test_opportunity_wording():
    title, msg, spoken = format_alert("opportunity", DATA)
    assert title == "2 bidders left: Omaha Steaks Holiday Steak Flight"
    assert "SnLB78125270, RobertCMendes" in msg and "876 bids ($113.88) of your 1000 limit" in msg
    assert "1011 bids" in msg
    assert spoken == "Auction alert. Omaha Steaks Holiday Steak Flight is down to 2 bidders."
    assert "http" not in spoken


def test_every_kind_has_text():
    for kind in ("opportunity", "bid_limit", "ended", "won"):
        title, msg, spoken = format_alert(kind, DATA)
        assert title and msg and spoken
    assert "82 dollars 42" in format_alert("ended", DATA)[2]
    assert "17 booked bids are still running" in format_alert("bid_limit", DATA)[1]


def test_routing_by_target():
    calls = build_calls(
        "opportunity", DATA,
        services=["mobile_app_pixel_8", "notify.telegram_me", "alexa_media_kitchen_echo"],
        entities=["notify.telegram_bot_chat_123", "notify.living_room_echo_announce"],
    )
    by_target = {(c[1], c[2].get("entity_id")): c[2] for c in calls}

    push = by_target[("mobile_app_pixel_8", None)]
    assert push["data"]["clickAction"] == DATA["url"] and push["data"]["tag"] == "auction-17782125"
    assert DATA["url"] not in push["message"]  # the tap opens it instead

    telegram = by_target[("telegram_me", None)]
    assert telegram["message"].endswith(DATA["url"])

    alexa = by_target[("alexa_media_kitchen_echo", None)]
    assert alexa["data"] == {"type": "announce"} and "http" not in alexa["message"]

    tg_entity = by_target[("send_message", "notify.telegram_bot_chat_123")]
    assert tg_entity["title"] and tg_entity["message"].endswith(DATA["url"])

    echo_entity = by_target[("send_message", "notify.living_room_echo_announce")]
    assert "title" not in echo_entity and echo_entity["message"].startswith("Auction alert.")


def test_no_targets_no_calls():
    assert build_calls("won", DATA, [], []) == []
