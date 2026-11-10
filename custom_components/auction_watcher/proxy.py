"""Bidding for someone else (opt-in). Plain Python (no Home Assistant), unit-tested.

You set a hard max on a Mac.bid item for a person on your list. They get a Telegram message,
reply with their max (up to yours), then tap "I agree to pay". Only then is a max bid placed:
the lower of their max and yours. Everything they agreed to is logged.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

ASKED = "asked"                 # waiting for their max
CONFIRMING = "confirming"       # they named a max; waiting for "I agree"
CONFIRMED = "confirmed"         # agreed; bid placed (or being placed)
DECLINED = "declined"           # they said no
CANCELLED = "cancelled"         # you cancelled, or the item ended before they agreed
WATCHING = "watching"           # "just watching": they get updates, nothing is bid
OPEN = (ASKED, CONFIRMING, CONFIRMED, WATCHING)
HEADER = "🔔 Auction Watcher"     # first line of every message, so they know what it's about


@dataclass
class Person:
    name: str
    chat_id: str                       # their Telegram chat id (they must have pressed /start with your bot)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Person":
        return cls(name=str(d.get("name") or "").strip(), chat_id=str(d.get("chat_id") or "").strip(), id=str(d.get("id") or uuid.uuid4().hex[:8]))


@dataclass
class ProxyRequest:
    lot_id: str
    person_id: str
    hard_max: int                      # the most YOU will bid for them (0 in "just watching")
    site: str = "macbid"
    mode: str = "bid"                  # "bid" | "watch"
    title: str = ""
    url: str = ""
    state: str = ASKED
    their_max: int = 0                 # what they named (confirmed or waiting for "I agree")
    agreed_max: int = 0                # what they agreed to pay up to (the bid placed is min(this, hard_max))
    estimate: float | None = None      # estimated total if won at the agreed max (fees + tax)
    created: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    log: list[list[Any]] = field(default_factory=list)   # [time, text]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ProxyRequest":
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in known})

    def note(self, text: str, now: float | None = None) -> None:
        self.log.append([time.time() if now is None else now, text])
        del self.log[:-50]

    @property
    def key(self) -> str:
        return f"{self.site}:{self.lot_id}"

    @property
    def bid_amount(self) -> int:
        return min(self.agreed_max, self.hard_max) if self.agreed_max else 0


def to_dict(x: Any) -> dict[str, Any]:
    return asdict(x)


_AMOUNT = re.compile(r"\$?\s*(\d{1,6})(?:[.,]\d{1,2})?")


def parse_amount(text: str) -> int | None:
    """'45', '$45', '45.50', 'max 60' -> whole dollars (Mac.bid bids are whole dollars)."""
    m = _AMOUNT.search(text or "")
    return int(m.group(1)) if m else None


def on_amount(req: ProxyRequest, amount: int, current_price: float | None, now: float | None = None) -> tuple[bool, str]:
    """They replied with a max. Returns (accepted, what to tell them). Accepted -> CONFIRMING."""
    floor = int(current_price or 0) + 1
    if req.state not in (ASKED, CONFIRMING, CONFIRMED):
        return False, "That request is closed."
    if req.state == CONFIRMED and amount <= req.agreed_max:
        return False, f"You've already agreed to ${req.agreed_max}. To raise it, reply with a higher amount (up to ${req.hard_max})."
    if amount > req.hard_max:
        return False, f"The most I can bid for you on this one is ${req.hard_max}. Reply with ${req.hard_max} or less."
    if amount < floor:
        return False, f"The current bid is ${int(current_price or 0)}, so your max has to be at least ${floor}."
    req.their_max = amount
    req.state = CONFIRMING if req.state != CONFIRMED else req.state
    req.note(f"they named a max of ${amount}", now)
    return True, ""


def on_agree(req: ProxyRequest, estimate: float | None, now: float | None = None) -> int:
    """They tapped "I agree to pay". Returns the max bid to place (0 = nothing to do)."""
    if not req.their_max or req.state not in (CONFIRMING, CONFIRMED):
        return 0
    req.agreed_max = req.their_max
    req.estimate = estimate
    req.state = CONFIRMED
    req.note(f"agreed to pay up to ${req.agreed_max} (est. ${estimate:.2f} with fees and tax)" if estimate is not None
             else f"agreed to pay up to ${req.agreed_max}", now)
    return req.bid_amount


def on_decline(req: ProxyRequest, now: float | None = None) -> None:
    if req.state == CONFIRMED:
        req.their_max = req.agreed_max  # a declined raise keeps the earlier agreement
        req.note("declined the raise; earlier agreement stands", now)
        return
    req.state = DECLINED
    req.note("declined", now)


# ---------------- message texts (plain text; Telegram adds the buttons)

def ask_text(name: str, title: str, url: str, price: float | None, hard_max: int, ends: str | None) -> str:
    return (f"Hi {name}! Want me to bid on this for you?\n\n{title}\nCurrent bid: ${int(price or 0)}"
            + (f" · ends {ends}" if ends else "") + f"\n{url}\n\n"
            f"Reply with the most you'd pay as a bid (whole dollars, up to ${hard_max}).\n"
            "Mac.bid adds a 15% buyer's premium, a $3 lot fee and sales tax on top of the winning bid.")


def watch_text(name: str, title: str, url: str, price: float | None) -> str:
    return (f"Hi {name}! I'm keeping an eye on this for you:\n\n{title}\nCurrent price: ${price or 0:.2f}\n{url}\n\n"
            "I'll message you when it's about to end and how it ends. Nothing will be bid.")


def confirm_text(title: str, amount: int, estimate: float | None) -> str:
    est = f" (about ${estimate:.2f} total with the 15% premium, $3 lot fee and tax)" if estimate is not None else " plus fees and tax"
    return (f"{title}\n\nI'll bid up to ${amount} for you. If it's won, you agree to pay the winning bid{est}. "
            "Mac.bid bids can't be withdrawn once placed.\n\nTap \"I agree to pay\" to confirm.")


def result_text(kind: str, title: str, d: dict[str, Any], req: ProxyRequest) -> str | None:
    """What the person hears about the item they asked for (None = nothing to send)."""
    price = d.get("price")
    if req.mode == "watch":
        if kind == "ending_soon":
            return f"{title} ends soon. Current price ${price or 0:.2f}."
        if kind in ("won", "ended"):
            return f"{title} has ended" + (f" at ${price:.2f}." if isinstance(price, (int, float)) else ".")
        return None
    if kind == "bid_placed":
        return f"Your max bid of ${req.bid_amount} is in on {title}. Mac.bid raises the bid for you up to that."
    if kind == "bid_failed":
        return f"Your bid on {title} didn't go through: {d.get('reason') or 'Mac.bid refused it'}. I'll take a look."
    if kind == "outbid":
        return (f"You've been outbid on {title} (now ${int(price or 0)}). Your max was ${req.bid_amount}. "
                f"Reply with a higher amount (up to ${req.hard_max}) to raise it.")
    if kind == "ending_soon":
        return f"{title} ends soon. Current bid ${int(price or 0)}; your max is ${req.bid_amount}."
    if kind == "won":
        total = d.get("total")
        return (f"You won {title} for ${price:.2f}!" if isinstance(price, (int, float)) else f"You won {title}!") + \
            (f" With fees and tax that's about ${total:.2f}." if isinstance(total, (int, float)) else "")
    if kind == "ended":
        return f"{title} ended and someone else won it. Your max was ${req.bid_amount}, so you don't owe anything."
    return None
