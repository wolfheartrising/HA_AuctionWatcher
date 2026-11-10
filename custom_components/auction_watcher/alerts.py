"""Alert wording and where alerts go. Plain Python (no Home Assistant), so it's unit-tested.

Targets:
  * notify services  (notify.mobile_app_phone, notify.telegram, notify.alexa_media_kitchen, ...)
      - mobile_app_*  -> push with tap-to-open link
      - alexa_media*  -> spoken announcement (Alexa Media Player)
      - anything else -> text with the link in it (Telegram, email, persistent_notification, ...)
  * notify entities  (notify.send_message): Telegram bot chats, Alexa Devices "Announce"/"Speak", ...
      - ids containing announce/speak/alexa/echo -> spoken text, otherwise text with link
"""

from __future__ import annotations

from typing import Any

KINDS = ("opportunity", "bid_limit", "ended", "won", "booked", "cancelled", "booking_failed",
         "outbid", "ending_soon", "over_max", "pickup", "pack", "budget", "bid_placed", "bid_failed")
SPOKEN_HINTS = ("announce", "speak", "alexa", "echo")


def _money(v: Any) -> str:
    try:
        return f"${float(v):.2f}"
    except (TypeError, ValueError):
        return "?"


def _spoken_money(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "an unknown price"
    dollars, cents = int(f), round((f - int(f)) * 100)
    return f"{dollars} dollars" + (f" {cents}" if cents else "")


def _human_minutes(m: Any) -> str:
    try:
        m = int(m)
    except (TypeError, ValueError):
        return "soon"
    if m < 60:
        return f"{m} minute{'' if m == 1 else 's'}"
    h, mm = divmod(m, 60)
    return f"{h} hour{'' if h == 1 else 's'}" + (f" {mm} min" if mm else "")


def human_time(dt: Any) -> str:
    """'Wed Nov 12, 5:00 PM' (dt should already be in your local time zone)."""
    return f"{dt:%a %b} {dt.day}, {dt.hour % 12 or 12}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'}"


def _human_deadline(iso: Any, local: str | None = None) -> str:
    if local:
        return local
    from datetime import datetime
    try:
        return human_time(datetime.fromisoformat(str(iso)))
    except (TypeError, ValueError):
        return "the deadline"


def format_alert(kind: str, d: dict[str, Any]) -> tuple[str, str, str]:
    """Return (title, message, spoken)."""
    name = d.get("title") or f"Auction {d.get('auction_id')}"
    bids, cost = d.get("your_bids") or 0, _money(d.get("your_points_cost"))
    limit = d.get("bid_limit") or 0
    if kind == "opportunity":
        n = d.get("active_bidders") or 0
        title = f"{n} bidder{'' if n == 1 else 's'} left: {name}"
        msg = f"{_money(d.get('price'))}. Last minute: {', '.join(d.get('recent_bidders') or []) or 'nobody'}."
        msg += f" You've used {bids} bids ({cost})" + (f" of your {limit} limit." if limit else ".")
        if d.get("median_winner_bids"):
            msg += f" Past winners typically used {d['median_winner_bids']} bids."
        spoken = f"Auction alert. {name} is down to {n} bidder{'' if n == 1 else 's'}."
    elif kind == "bid_limit":
        title = f"Bid limit reached: {name}"
        msg = f"You've used {bids} bids ({cost}); your limit is {limit}."
        if d.get("your_booked_left"):
            msg += f" {d['your_booked_left']} booked bids are still running: cancel them on the site to stop."
        spoken = f"Auction alert. You reached your bid limit on {name}."
    elif kind == "ended":
        title = f"Ended: {name}"
        msg = f"Final {_money(d.get('price'))}, leader {d.get('leader') or 'unknown'}. You used {bids} bids ({cost})."
        if d.get("buy_it_now") and bids:
            msg += f" Buy It Now {_money(d['buy_it_now'])} returns your points, for a limited time."
        spoken = f"Auction ended. {name} finished at {_spoken_money(d.get('price'))}."
    elif kind == "outbid":
        title = f"Outbid: {name}"
        msg = f"Now {_money(d.get('price'))}"
        if d.get("minutes_left") is not None:
            msg += f", ends in {_human_minutes(d['minutes_left'])}"
        msg += f". Your max is {_money(limit)}." if limit else "."
        spoken = f"You were outbid on {name}."
    elif kind == "ending_soon":
        title = f"Ending in {_human_minutes(d.get('minutes_left'))}: {name}"
        msg = f"Now {_money(d.get('price'))}, {'you are winning' if d.get('winning') else 'you are not winning'}."
        if limit:
            msg += f" Your max is {_money(limit)}."
        spoken = f"{name} ends in {_human_minutes(d.get('minutes_left'))}."
    elif kind == "over_max":
        title = f"Past your max: {name}"
        msg = f"Now {_money(d.get('price'))}, over your max of {_money(limit)}."
        spoken = f"{name} went past your max bid."
    elif kind == "pickup":
        when = _human_deadline(d.get("ends_at"), d.get("ends_local"))
        title = f"Pick up by {when}: {name}"
        msg = f"Won for {_money(d.get('price'))}. Pickup deadline {when}."
        spoken = f"Reminder: pick up {name} by {when}."
    elif kind == "budget":
        title = "Weekly auction budget used up"
        msg = (f"You've spent {d.get('spent')} of your {d.get('budget')} points this week. "
               "Auto-booking is stopped until Monday (or raise the budget in the settings).")
        spoken = "Your weekly auction budget is used up. Auto booking is stopped."
    elif kind == "pack":
        title = f"Good point pack: {name}"
        msg = (f"Worth {_money(d.get('value'))} ({d.get('points')} points). Past winners usually needed "
               f"~{d.get('expected_bids')} bids ({d.get('past_wins')} wins).")
        if d.get("suggested_max"):
            msg += (f" Suggested max {d['suggested_max']} bids ({round((d.get('cover') or 0) * 100)}% of past winners"
                    f" needed no more).")
        if d.get("bids_to_lock") is not None:
            msg += f" Open to new bidders for about {d['bids_to_lock']} more bids."
        spoken = f"A good point pack is open: {name}."
    elif kind == "bid_placed":
        title = f"Max bid ${d.get('amount')} placed: {name}"
        msg = f"{d.get('reason') or ''}. Mac.bid raises your bid for you up to ${d.get('amount')}. Now {_money(d.get('price'))}."
        spoken = f"Placed a {d.get('amount')} dollar max bid on {name}."
    elif kind == "bid_failed":
        title = f"Bid problem: {name}"
        msg = f"{d.get('reason') or 'Mac.bid refused the bid.'} Check the item on Mac.bid."
        spoken = f"Bid problem on {name}. Check your phone."
    elif kind == "booked":
        title = f"Booked {d.get('count')} bids: {name}"
        msg = f"{d.get('reason') or ''}. Price {_money(d.get('price'))}, {d.get('active_bidders')} bidders."
        if limit:
            msg += f" You've used {bids} of your {limit} limit."
        spoken = f"Booked {d.get('count')} bids on {name}."
    elif kind == "cancelled":
        title = f"Cancelled booked bids: {name}"
        msg = f"{d.get('reason') or ''}. {d.get('count')} unused bids go back to your balance."
        spoken = f"Cancelled your booked bids on {name}."
    elif kind == "booking_failed":
        title = f"Booking problem: {name}"
        msg = f"{d.get('reason') or 'The site refused.'} Check the auction on the site."
        spoken = f"Booking problem on {name}. Check your phone."
    else:  # won
        title = f"You won: {name}"
        msg = "Open the site to pay or exchange it."
        spoken = f"Congratulations, you won {name}."
    return title, msg, spoken


def _is_spoken(target: str) -> bool:
    t = target.lower()
    return any(h in t for h in SPOKEN_HINTS)


def build_calls(
    kind: str,
    data: dict[str, Any],
    services: list[str],
    entities: list[str],
) -> list[tuple[str, str, dict[str, Any]]]:
    """Return Home Assistant action calls as (domain, service, service_data)."""
    title, msg, spoken = format_alert(kind, data)
    url = data.get("url") or ""
    with_link = f"{msg}\n{url}" if url else msg
    calls: list[tuple[str, str, dict[str, Any]]] = []

    for svc in services:
        svc = svc.removeprefix("notify.")
        if svc.startswith("mobile_app_"):
            calls.append(("notify", svc, {
                "title": title,
                "message": msg,
                "data": {
                    "url": url, "clickAction": url,
                    "tag": f"auction-{data.get('auction_id')}",
                    "priority": "high", "ttl": 0,
                },
            }))
        elif svc.startswith("alexa_media"):
            calls.append(("notify", svc, {"message": spoken, "data": {"type": "announce"}}))
        else:
            calls.append(("notify", svc, {"title": title, "message": with_link}))

    for ent in entities:
        if _is_spoken(ent):
            calls.append(("notify", "send_message", {"entity_id": ent, "message": spoken}))
        else:
            calls.append(("notify", "send_message", {"entity_id": ent, "title": title, "message": with_link}))
    return calls
