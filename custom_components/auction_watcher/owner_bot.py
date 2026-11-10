"""The owner's side of the Telegram bot: your own auctions from your phone.

Only the chat you set as "Owner" (Settings → For others) can use these. Anything that spends asks
you to confirm with a second button; your max, budgets, spending cap and pause switch still apply.

  /status    balance, spending cap, what you're winning / outbid on / auto-bidding
  /active    your live items, each with a quick bid and Stop
  /pause     pause all auto-booking and auto-bidding;  /resume to undo
  /help      this list
Buttons on your alerts (outbid, few bidders left, good point pack) and on "X wants you to bid" requests.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import Event

from . import global_settings
from .proxy import kb_row, short
from .sites import macbid as mb
from .sites.base import AuthError, SiteError

_LOGGER = logging.getLogger(__name__)

OWNER_HELP = ("Your Auction Watcher controls:\n\n"
              "/status: balance, spending cap and what you're winning, outbid on or auto-bidding\n"
              "/active: your live items, each with a quick bid and Stop\n"
              "/pause: pause all automatic bidding · /resume: turn it back on\n\n"
              "Alerts here come with buttons (bid the next step, +1 bid, follow a good pack). Anything that spends asks "
              "you to confirm first; your max, budgets and spending cap still apply.")

OWNER_ALERT_KINDS = ("outbid", "opportunity", "pack")


def _key(site: str, aid: Any) -> str:
    return f"{site}:{aid}"


class OwnerMixin:
    """Mixed into ProxyManager (which provides hass, _tg, _coords, _stop, async_ask, requests, people…)."""

    owner_chat: str | None
    owner_alerts: bool

    def is_owner(self, event: Event) -> bool:
        cid = event.data.get("chat_id")
        uid = event.data.get("user_id")
        return bool(self.owner_chat) and str(self.owner_chat) in (str(cid), str(uid))

    async def _owner_say(self, text: str, rows: list[str] | None = None, photo: str | None = None) -> None:
        if self.owner_chat:
            await self._tg(self.owner_chat, text, rows, photo=photo)

    # ---------------- what's going on
    def _items(self) -> list[tuple[str, Any]]:
        out = []
        for site, coord in self._coords().items():
            for st in (coord.data or {}).values():
                out.append((site, st))
        return out

    def _is_mine_live(self, site: str, st: Any) -> bool:
        if st.status not in ("live", "upcoming"):
            return False
        x = st.extra or {}
        return (st.category == "bidding" or x.get("winning") or x.get("you_are_bidding") or bool(st.your_booked_left)
                or x.get("mb_mode") == "late")

    async def owner_status(self) -> None:
        lines = []
        for site, coord in self._coords().items():
            name = {"govauctions": "GovAuctions", "macbid": "Mac.bid", "ebay": "eBay"}.get(site, site)
            mine = [st for st in (coord.data or {}).values() if self._is_mine_live(site, st)]
            winning = [st for st in mine if (st.extra or {}).get("winning") or (site == "govauctions" and st.leader and
                                                                                  st.leader == getattr(coord, "username", None))]
            outbid = [st for st in mine if st not in winning and (st.your_bids or (st.extra or {}).get("you_are_bidding"))]
            head = f"{name}: {len(mine)} active, {len(winning)} winning, {len(outbid)} outbid"
            if site == "govauctions" and coord.points is not None:
                head += f" · {coord.points} points"
            if getattr(coord, "auto_paused", False):
                head += " · auto PAUSED"
            lines.append(head)
            for st in (winning[:4] + outbid[:4]):
                tag = "winning" if st in winning else "outbid"
                lines.append(f"  • {short(st.title, 34)}: ${st.price or 0:.2f} ({tag})")
        g = global_settings.get(self.hass)
        cap = g.summary() if g else None
        if cap:
            per = "this month" if cap["period"] == "month" else "this week"
            lines.append(f"Spending cap: ${cap['used']:.2f} of ${cap['amount']:.0f} {per}"
                         + (" (reached)" if cap["used"] >= cap["amount"] else f", ${cap['left']:.2f} left"))
        await self._owner_say("\n".join(lines) or "Nothing set up yet.")

    async def owner_active(self) -> None:
        rows, lines = [], []
        mine = [(site, st) for site, st in self._items() if self._is_mine_live(site, st)][:10]
        if not mine:
            await self._owner_say("Nothing active right now.")
            return
        for site, st in mine:
            key = _key(site, st.auction_id)
            if site == "govauctions":
                lines.append(f"• {short(st.title, 34)}: ${st.price or 0:.2f}, {st.your_bids} bids used, {st.your_booked_left} booked")
                rows.append(kb_row([(f"+1 bid: {short(st.title, 14)}", f"/aw_o_quick {key}"), ("Stop", f"/aw_o_stop {key}")]))
            elif site == "macbid":
                nxt = (st.extra or {}).get("min_next_bid")
                win = (st.extra or {}).get("winning")
                lines.append(f"• {short(st.title, 34)}: ${st.price or 0:.2f}" + (" (winning)" if win else ""))
                first = (f"Bid ${nxt}: {short(st.title, 12)}", f"/aw_o_quick {key}") if nxt and not win else None
                rows.append(kb_row([b for b in (first, ("Stop", f"/aw_o_stop {key}")) if b]))
            else:
                lines.append(f"• {short(st.title, 34)}: ${st.price or 0:.2f}")
        await self._owner_say("Your active items:\n" + "\n".join(lines), rows)

    async def owner_pause(self, paused: bool) -> None:
        for coord in self._coords().values():
            if hasattr(coord, "async_set_auto_paused"):
                await coord.async_set_auto_paused(paused)
        await self._owner_say("All automatic bidding and booking is PAUSED. /resume to turn it back on." if paused
                              else "Automatic bidding and booking resumed (only where you turned it on).")

    # ---------------- commands and buttons from your chat
    async def owner_command(self, command: str) -> None:
        if command in ("/status",):
            await self.owner_status()
        elif command in ("/active", "/items", "/auctions"):
            await self.owner_active()
        elif command == "/pause":
            await self.owner_pause(True)
        elif command == "/resume":
            await self.owner_pause(False)
        elif command in ("/help", "/start"):
            await self._owner_say(OWNER_HELP)
        elif command == "/stop":
            await self._owner_say("Tap Stop next to an item in /active.")

    def _find(self, key: str) -> tuple[Any, Any, str, str]:
        site, _, aid = key.partition(":")
        coord = self._coords().get(site)
        st = (coord.data or {}).get(aid) if coord else None
        return coord, st, site, aid

    async def owner_button(self, data: str) -> None:
        parts = data.split()
        cmd = parts[0]
        try:
            if cmd == "/aw_o_no":
                await self._owner_say("OK, nothing done.")
            elif cmd in ("/aw_o_quick", "/aw_o_book") and len(parts) >= 2:
                coord, st, site, aid = self._find(parts[1])
                if st is None:
                    await self._owner_say("That item isn't followed anymore.")
                    return
                n = int(parts[2]) if cmd == "/aw_o_book" and len(parts) > 2 else 1
                if site == "macbid":
                    nxt = (st.extra or {}).get("min_next_bid")
                    q = (f"Bid the next step on {st.title}? About ${nxt} (checked live when you confirm); "
                         f"if you win at it you pay about ${mb.win_cost(nxt, (st.extra or {}).get('sales_tax')) or 0:.2f} with fees. "
                         "Bids can't be cancelled.")
                    yes = f"/aw_o_yes quick {parts[1]}"
                else:
                    q = f"Place {n} bid{'' if n == 1 else 's'} on {st.title}? ({n} point{'' if n == 1 else 's'}, placed in turn.)"
                    yes = f"/aw_o_yes book {parts[1]} {n}"
                await self._owner_say(q, [kb_row([("Yes, bid", yes), ("No", "/aw_o_no")])])
            elif cmd == "/aw_o_yes" and len(parts) >= 3:
                coord, st, site, aid = self._find(parts[2])
                if coord is None or st is None:
                    await self._owner_say("That item isn't followed anymore.")
                    return
                if parts[1] == "quick":
                    done = await coord.async_quick_bid(aid)
                else:
                    done = f"{await coord.async_book(aid, int(parts[3]) if len(parts) > 3 else 1)} bids booked"
                await self._owner_say(f"{st.title}: {done}.")
            elif cmd == "/aw_o_stop" and len(parts) >= 2:
                coord, st, site, aid = self._find(parts[1])
                if coord is None or st is None:
                    await self._owner_say("That item isn't followed anymore.")
                    return
                await self._owner_say(f"{st.title}: stopped ({await coord.async_stop(aid)}).")
            elif cmd == "/aw_o_follow" and len(parts) >= 2:
                coord = self._coords().get("govauctions")
                if coord is None:
                    return
                await coord.async_watch(parts[1], True)
                if len(parts) > 2 and int(parts[2]):
                    await coord.async_set_bid_limit(parts[1], int(parts[2]))
                await self._owner_say("Following it: it's on your Watching tab with Book and auto-booking. Nothing was bid.")
            elif cmd == "/aw_o_allow" and len(parts) >= 3:
                req = next((r for r in self.requests.values() if r.id == parts[1]), None)
                if req is None or req.state != "watching":
                    await self._owner_say("That request isn't open anymore.")
                    return
                await self.async_ask(req.site, req.lot_id, req.person_id, int(parts[2]), "bid")
                await self._owner_say(f"OK: asked them to confirm on {req.title}, up to {parts[2]}"
                                      + (" bids." if req.site == "govauctions" else " dollars."))
            elif cmd == "/aw_o_deny" and len(parts) >= 2:
                req = next((r for r in self.requests.values() if r.id == parts[1]), None)
                if req is None:
                    return
                req.wants = 0
                req.note("you said no to bidding")
                await self._save()
                person = self._person(req.person_id)
                if person:
                    await self._tg(person.chat_id, f"Not this time on {req.title}; I'll keep you posted on it.")
                await self._owner_say("OK, told them.")
        except (SiteError, AuthError, ValueError) as err:
            await self._owner_say(f"Couldn't: {err}")

    # ---------------- alerts with buttons, and "X wants you to bid" approvals
    def owner_alert(self, site: str, kind: str, data: dict[str, Any]) -> None:
        """Your own outbid / few-bidders / good-pack alerts, with buttons (when your chat is set)."""
        if not (self.owner_chat and self.owner_alerts and kind in OWNER_ALERT_KINDS):
            return
        g = global_settings.get(self.hass)
        if g is not None and kind not in g.alert_kinds:
            return
        key = _key(site, data.get("auction_id"))
        title = data.get("title") or "an item"
        price = data.get("price")
        if kind == "outbid":
            text = f"Outbid on {title}" + (f": now ${price:.2f}." if isinstance(price, (int, float)) else ".")
            coord, st, _, _ = self._find(key)
            nxt = (st.extra or {}).get("min_next_bid") if st is not None else None
            rows = [kb_row([(f"Bid next step ${nxt}", f"/aw_o_quick {key}"), ("Stop", f"/aw_o_stop {key}")])] \
                if site == "macbid" and nxt else None
        elif kind == "opportunity":
            n = data.get("active_bidders")
            text = f"{n} other bidder{'' if n == 1 else 's'} left on {title}" + (f" (${price:.2f})." if isinstance(price, (int, float)) else ".")
            rows = [kb_row([("+1 bid", f"/aw_o_quick {key}"), ("Book 10", f"/aw_o_book {key} 10"), ("Stop", f"/aw_o_stop {key}")])]
        else:  # pack
            sm = data.get("suggested_max") or 0
            text = (f"Good point pack open: {title}. Past winners usually used ~{data.get('expected_bids')} bids"
                    + (f"; suggested max {sm} bids." if sm else "."))
            rows = [kb_row([("Follow" + (f" (max {sm})" if sm else ""), f"/aw_o_follow {data.get('auction_id')} {int(sm)}")])]
        self.hass.async_create_task(self._owner_say(text + (f"\n{data.get('url')}" if data.get("url") else ""), rows))

    async def owner_approve(self, req: Any, person_name: str, amount: int, price: float | None) -> bool:
        """Someone you keep posted wants a bid: ask you with buttons. False if no owner chat is set."""
        if not self.owner_chat:
            return False
        if req.site == "ebay":
            await self._owner_say(f"{person_name} wants you to bid up to ${amount} on {req.title} (eBay: place it by hand).\n{req.url}")
            return True
        if req.site == "govauctions":
            options = sorted({amount, amount * 2})
            label = lambda n: f"Allow {n} bids"  # noqa: E731
        else:
            options = sorted({amount, mb.round_down_bid(amount * 1.5)})
            label = lambda n: f"Allow up to ${n}"  # noqa: E731
        text = (f"{person_name} wants you to bid up to {amount}{' bids' if req.site == 'govauctions' else ' dollars'}"
                + (f" (max price ${price:.2f})" if price else "") + f" on {req.title}.\n"
                "Allow sets your max for them; they then confirm with \"I agree to pay\".")
        rows = [kb_row([(label(n), f"/aw_o_allow {req.id} {n}") for n in options] + [("No", f"/aw_o_deny {req.id}")])]
        await self._owner_say(text, rows)
        return True
