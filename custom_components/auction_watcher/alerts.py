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

KINDS = ("opportunity", "bid_limit", "ended", "won", "booked", "cancelled", "booking_failed")
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
