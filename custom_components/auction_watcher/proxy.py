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
        return cls(name=clean_name(d.get("name")), chat_id=str(d.get("chat_id") or "").strip(), id=str(d.get("id") or uuid.uuid4().hex[:8]))


def clean_name(name: Any) -> str:
    """'Schrodinger (609098350)' -> 'Schrodinger' (Telegram chat titles often include the id)."""
    return re.sub(r"\s*\(-?\d{5,}\)\s*$", "", str(name or "")).strip()


@dataclass
class ProxyRequest:
    lot_id: str
    person_id: str
    hard_max: int                      # the most YOU will bid for them: dollars (Mac.bid) or bids (GovAuctions); 0 = watching
    site: str = "macbid"
    mode: str = "bid"                  # "bid" | "watch"
    # GovAuctions (penny auctions): every bid costs a point, win or lose
    point_cost: float = 0.13           # $ per bid, for what they agree to pay
    their_price: float = 0.0           # their max auction price (0 = none): booked bids stop above it
    agreed_price: float = 0.0
    start_bids: int = 0                # your bids on the auction when they agreed (theirs are the ones after)
    stopped: bool = False              # cancelled after a Mac.bid bid was placed: no raises, still told how it ends
    wants: int = 0                     # while just watching, they asked you to bid: dollars (Mac.bid/eBay) or bids (GovAuctions)
    wants_price: float = 0.0           # GovAuctions: the max price they mentioned with it
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


_PRICE = re.compile(r"\$\s*(\d{1,6}(?:[.,]\d{1,2})?)")


def parse_gov_reply(text: str) -> tuple[int | None, float | None]:
    """GovAuctions reply: '40' (bids) or '40 $25' / '40 bids $25.50' (bids + max price)."""
    t = text or ""
    price_m = _PRICE.search(t)
    price = float(price_m.group(1).replace(",", ".")) if price_m else None
    rest = t[:price_m.start()] + t[price_m.end():] if price_m else t
    m = re.search(r"(\d{1,5})", rest)
    return (int(m.group(1)) if m else None), price


def on_bids(req: ProxyRequest, bids: int, price: float | None, current_price: float | None,
            now: float | None = None) -> tuple[bool, str]:
    """GovAuctions: they replied with how many bids they'll pay for (and maybe a max price)."""
    if req.state not in (ASKED, CONFIRMING, CONFIRMED):
        return False, "That request is closed."
    if bids < 1:
        return False, "Reply with how many bids you'd pay for, e.g. \"40\" or \"40 $25\"."
    if req.state == CONFIRMED and bids <= req.agreed_max and not price:
        return False, f"You've already agreed to {req.agreed_max} bids. Reply with a higher total (up to {req.hard_max}) to add more."
    if bids > req.hard_max:
        return False, f"The most I can spend for you on this one is {req.hard_max} bids. Reply with {req.hard_max} or fewer."
    if price is not None and current_price is not None and price <= current_price:
        return False, f"The price is already ${current_price:.2f}, so a max price has to be higher than that."
    req.their_max = bids
    req.their_price = float(price or 0)
    if req.state != CONFIRMED:
        req.state = CONFIRMING
    req.note(f"they offered {bids} bids" + (f", max price ${price:.2f}" if price else ""), now)
    return True, ""


STOP_WORDS = {"stop", "cancel", "no", "nevermind", "never mind", "quit"}


def wants_stop(text: str) -> bool:
    return (text or "").strip().lower().strip(".!") in STOP_WORDS


def gov_used(req: ProxyRequest, your_bids_now: int) -> int:
    """Their bids used so far on a GovAuctions request (never more than they agreed to)."""
    used = max(0, int(your_bids_now or 0) - req.start_bids)
    return min(used, req.agreed_max) if req.agreed_max else used


def on_amount(req: ProxyRequest, amount: int, current_price: float | None, now: float | None = None,
              min_next: int | None = None) -> tuple[bool, str]:
    """They replied with a max. Returns (accepted, what to tell them). Accepted -> CONFIRMING.
    min_next = Mac.bid's smallest acceptable bid right now (its steps grow with the price)."""
    floor = int(min_next or int(current_price or 0) + 1)
    if req.stopped:
        return False, "That request was cancelled, so I can't raise it."
    if req.state not in (ASKED, CONFIRMING, CONFIRMED):
        return False, "That request is closed."
    if req.state == CONFIRMED and amount <= req.agreed_max:
        return False, f"You've already agreed to ${req.agreed_max}. To raise it, reply with a higher amount (up to ${req.hard_max})."
    if amount > req.hard_max:
        return False, f"The most I can bid for you on this one is ${req.hard_max}. Reply with ${req.hard_max} or less."
    if amount < floor:
        return False, f"The current bid is ${int(current_price or 0)}, so your max has to be at least ${floor}."
    if req.site == "macbid":
        from .sites.macbid import is_valid_bid, next_valid_bid, round_down_bid
        if not is_valid_bid(amount):
            lower = round_down_bid(amount)
            options = [x for x in (lower, next_valid_bid(lower)) if x >= floor and x <= req.hard_max]
            return False, ("Mac.bid only takes its own steps at this price"
                           + (f": reply with {' or '.join('$' + str(x) for x in options)}." if options else "."))
    req.their_max = amount
    req.state = CONFIRMING if req.state != CONFIRMED else req.state
    req.note(f"they named a max of ${amount}", now)
    return True, ""


def on_agree(req: ProxyRequest, estimate: float | None, now: float | None = None) -> int:
    """They tapped "I agree to pay". Returns the max bid to place (Mac.bid dollars / GovAuctions bids; 0 = nothing)."""
    if not req.their_max or req.state not in (CONFIRMING, CONFIRMED):
        return 0
    req.agreed_max = req.their_max
    req.agreed_price = req.their_price
    req.estimate = estimate
    if req.site == "govauctions":
        req.state = CONFIRMED
        req.note(f"agreed to pay for up to {req.agreed_max} bids (${req.agreed_max * req.point_cost:.2f}) win or lose"
                 + (f", max price ${req.agreed_price:.2f}" if req.agreed_price else ""), now)
        return req.bid_amount
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
            f"Reply with the most you'd pay as a bid (up to ${hard_max}; Mac.bid takes $1 steps to $100, $5 to $500, $10 to $1,000).\n"
            "Mac.bid adds a 15% buyer's premium, a $3 lot fee and sales tax on top of the winning bid. "
            "Reply \"stop\" any time to cancel.")


def watch_text(name: str, title: str, url: str, price: float | None, site: str = "macbid") -> str:
    how = ('reply with how many bids you\'d pay for (e.g. "40", or "40 $25" with a max price)' if site == "govauctions"
           else 'reply with the most you\'d pay (e.g. "40")')
    return (f"Hi {name}! I'm keeping an eye on this for you:\n\n{title}\nCurrent price: ${price or 0:.2f}\n{url}\n\n"
            "I'll message you when it's about to end and how it ends. Nothing will be bid. "
            f"Want me to bid? {how}. Reply \"stop\" to stop updates.")


def on_wish(req: ProxyRequest, amount: int, price: float | None = None, now: float | None = None) -> str:
    """While just watching, they asked for a bid. Returns what to tell them (you decide on the page)."""
    req.wants = amount
    req.wants_price = float(price or 0)
    unit = f"{amount} bids" if req.site == "govauctions" else f"${amount}"
    req.note(f"asked you to bid for them: up to {unit}" + (f", max price ${price:.2f}" if price else ""), now)
    if req.site == "ebay":
        return (f"Got it: up to {unit} on {req.title}. I can't bid on eBay automatically, so I've passed it on "
                "to place by hand.")
    return (f"Got it: up to {unit} on {req.title}. I've asked for it to be set up; you'll get a message to confirm "
            "before anything is bid.")


def gov_ask_text(name: str, title: str, url: str, price: float | None, hard_max: int, point_cost: float) -> str:
    return (f"Hi {name}! Want me to bid on this penny auction for you?\n\n{title}\nPrice now: ${price or 0:.2f}\n{url}\n\n"
            f"Reply with how many bids you'd pay for (up to {hard_max}). Each bid costs ${point_cost:.2f}, "
            "and you pay for the bids used whether you win or not. Add a max price if you like, e.g. \"40 $25\". "
            "Reply \"stop\" any time to stop.")


def gov_confirm_text(title: str, bids: int, point_cost: float, price: float, labels_note: str = "") -> str:
    cost = bids * point_cost
    return (f"{title}\n\nI'll spend up to {bids} bids for you: up to ${cost:.2f}, which you pay for the bids actually "
            f"used, win or lose." + (f" I'll stop if the price passes ${price:.2f}." if price else "")
            + " If you win, you also pay the final auction price (plus anything the site charges for that item)."
            + (f" {labels_note}" if labels_note else "") + "\n\nTap \"I agree to pay\" to confirm.")


def confirm_text(title: str, amount: int, estimate: float | None) -> str:
    est = f" (about ${estimate:.2f} total with the 15% premium, $3 lot fee and tax)" if estimate is not None else " plus fees and tax"
    return (f"{title}\n\nI'll bid up to ${amount} for you. If it's won, you agree to pay the winning bid{est}. "
            "Mac.bid bids can't be withdrawn once placed.\n\nTap \"I agree to pay\" to confirm.")


def result_text(kind: str, title: str, d: dict[str, Any], req: ProxyRequest) -> str | None:
    """What the person hears about the item they asked for (None = nothing to send)."""
    price = d.get("price")
    if req.site == "govauctions" and req.mode == "bid":
        used = gov_used(req, d.get("your_bids") or 0)
        spent = f"{used} bid{'' if used == 1 else 's'} used (${used * req.point_cost:.2f})"
        if kind == "booked":
            return f"Your bids are booked on {title}: up to {req.bid_amount}" + (
                f", stopping above ${req.agreed_price:.2f}." if req.agreed_price else ".")
        if kind == "bid_limit":
            return f"Your {req.bid_amount} bids on {title} are used up ({spent}). Reply with more bids to keep going."
        if kind == "won":
            pay = d.get("pay_if_won")
            return (f"You won {title}! {spent}" + (f", plus the final price ${pay:.2f}." if isinstance(pay, (int, float))
                                                    else (f", plus the final price ${price:.2f}." if isinstance(price, (int, float)) else ".")))
        if kind == "ended":
            return f"{title} ended and someone else won it. {spent}, which is what you owe."
        return None
    if req.mode == "watch":
        if kind == "ending_soon":
            return f"{title} ends soon. Current price ${price or 0:.2f}."
        if kind in ("won", "ended"):
            return f"{title} has ended" + (f" at ${price:.2f}." if isinstance(price, (int, float)) else ".")
        return None
    if kind == "bid_placed":
        if d.get("raised"):
            return (f"Heads up on {title}: Mac.bid only takes bids in steps, so it recorded your max as ${d.get('amount')}, "
                    f"not the ${d.get('sent')} you agreed to. If it's won at more than ${req.bid_amount}, we'll sort out "
                    "the difference; reply \"stop\" if you don't want any more raises.")
        return f"Your max bid of ${req.bid_amount} is in on {title}. Mac.bid raises the bid for you up to that."
    if kind == "bid_failed":
        return f"Your bid on {title} didn't go through: {d.get('reason') or 'Mac.bid refused it'}. I'll take a look."
    if kind == "outbid":
        if req.stopped:
            return None
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
