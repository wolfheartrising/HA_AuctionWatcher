"""Bidding for someone else, in Home Assistant: Telegram questions and buttons, then the Mac.bid bid.

Off unless switched on (Settings tab). Only people on your list are listened to, and only through
Home Assistant's Telegram bot (they must have pressed /start, and be an allowed chat).
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.storage import Store

from . import global_settings
from .const import CONF_SITE, DOMAIN, SITE_GOVAUCTIONS, SITE_MACBID, STORAGE_VERSION
from .proxy import (ASKED, CANCELLED, CONFIRMED, CONFIRMING, HEADER, OPEN, WATCHING, Person, ProxyRequest, ask_text,
                    confirm_text, gov_ask_text, gov_confirm_text, gov_used, on_agree, on_amount, on_bids, on_decline,
                    clean_name, on_wish, parse_amount, parse_gov_reply, result_text, to_dict, wants_stop, watch_text)
from .sites import macbid as mb
from .sites.base import AuthError, SiteError

_LOGGER = logging.getLogger(__name__)

DATA_KEY = f"{DOMAIN}_proxy"
AGREE, DECLINE = "/aw_agree", "/aw_no"
RELAYED = {"bid_placed", "bid_failed", "outbid", "ending_soon", "won", "ended", "bid_limit"}


class ProxyManager:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.proxy")
        self.enabled = False
        self.people: list[Person] = []
        self.requests: dict[str, ProxyRequest] = {}   # "site:item id" -> request
        self._unsubs: list[Any] = []
        self._seen: dict[str, str] = {}   # chat id -> name, chats that messaged the bot since HA started (not saved)

    # ---------------- storage / listening
    async def async_load(self) -> None:
        d = await self._store.async_load() or {}
        self.enabled = bool(d.get("enabled"))
        self.people = [Person.from_dict(p) for p in d.get("people", [])]
        self.requests = {r.key: r for r in (ProxyRequest.from_dict(v) for v in (d.get("requests") or {}).values())}
        self._unsubs = [self.hass.bus.async_listen("telegram_text", self._on_text),
                        self.hass.bus.async_listen("telegram_callback", self._on_callback)]

    async def _save(self) -> None:
        for coord in self._coords().values():
            coord.async_update_listeners()  # item cards show the request's state
        cutoff = time.time() - 30 * 86400
        self.requests = {k: r for k, r in self.requests.items() if r.state in OPEN or r.created >= cutoff}
        await self._store.async_save({"enabled": self.enabled, "people": [to_dict(p) for p in self.people],
                                      "requests": {k: to_dict(r) for k, r in self.requests.items()}})

    @callback
    def stop(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs = []

    # ---------------- helpers
    def _person(self, person_id: str | None = None, chat_id: Any = None) -> Person | None:
        for p in self.people:
            if (person_id and p.id == person_id) or (chat_id is not None and str(p.chat_id) == str(chat_id)):
                return p
        return None

    def _coords(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for e in self.hass.config_entries.async_entries(DOMAIN):
            if getattr(e, "runtime_data", None) is not None:
                out.setdefault(e.data.get(CONF_SITE, SITE_GOVAUCTIONS), e.runtime_data)
        return out

    def telegram_ready(self) -> bool:
        return self.hass.services.has_service("telegram_bot", "send_message")

    async def _tg(self, chat_id: str, text: str, keyboard: list[str] | None = None, photo: str | None = None) -> None:
        """A message headed "🔔 Auction Watcher"; with the item's picture when there is one."""
        target = int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id
        body = f"{HEADER}\n\n{text}"
        extra: dict[str, Any] = {"target": [target]}
        if keyboard:
            extra["inline_keyboard"] = keyboard
        if photo and photo.startswith("https://") and len(body) <= 1000:
            try:
                await self.hass.services.async_call("telegram_bot", "send_photo",
                                                    {**extra, "url": photo, "caption": body}, blocking=True)
                return
            except Exception as err:  # noqa: BLE001  (picture failed: send the text alone)
                _LOGGER.debug("Telegram photo failed, sending text: %s", err)
        try:
            await self.hass.services.async_call("telegram_bot", "send_message", {**extra, "message": body}, blocking=True)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Couldn't send the Telegram message to %s: %s", chat_id, err)

    def _tell_owner(self, person: Person | None, req: ProxyRequest, text: str) -> None:
        if (g := global_settings.get(self.hass)) is not None:
            g.send("proxy", {"auction_id": req.lot_id, "url": req.url,
                             "title_line": f"{person.name if person else 'Bidding for someone'}: {req.title}", "text": text})

    def _state(self, site: str, lot_id: str) -> Any:
        coord = self._coords().get(site)
        return (coord.data or {}).get(lot_id) if coord else None

    def _estimate(self, req: ProxyRequest, amount: int) -> float | None:
        st = self._state(req.site, req.lot_id)
        tax = st.extra.get("sales_tax") if st is not None else None
        return mb.win_cost(amount, tax)

    # ---------------- from the page
    async def async_ask(self, site: str, lot_id: str, person_id: str, hard_max: int, mode: str = "bid") -> None:
        if not self.enabled:
            raise SiteError("Turn on \"Bid for others\" on the Settings tab first")
        if not self.telegram_ready():
            raise SiteError("Home Assistant's Telegram bot integration isn't set up (it's needed for the messages and buttons)")
        person = self._person(person_id)
        if person is None:
            raise SiteError("Pick someone from your list")
        st = self._state(site, lot_id)
        if st is None or st.status not in ("live", "upcoming"):
            raise SiteError("That item isn't live")
        key = f"{site}:{lot_id}"
        if mode == "watch":
            old = self.requests.get(key)
            if old and old.state == CONFIRMED:
                raise SiteError("They've already agreed to a bid on this item (they get its updates anyway)")
            req = ProxyRequest(lot_id=lot_id, person_id=person_id, hard_max=0, site=site, mode="watch",
                               title=st.title, url=st.url, state=WATCHING)
            req.note(f"{person.name} is watching it with you")
            self.requests[key] = req
            await self._save()
            await self._tg(person.chat_id, watch_text(person.name, st.title, st.url, st.price, site), photo=st.image)
            return
        if site not in (SITE_MACBID, SITE_GOVAUCTIONS):
            raise SiteError("Bidding for someone isn't possible on this site; use \"Just keep them posted\"")
        if site == SITE_GOVAUCTIONS:
            await self._ask_gov(person, st, key, int(hard_max))
            return
        if st.status != "live":
            raise SiteError("That Mac.bid item isn't live")
        mine = self.requests.get(key)
        if (st.extra.get("you_are_bidding") or st.extra.get("winning")) and not (mine and mine.mode == "bid" and mine.state in OPEN):
            raise SiteError("You're already bidding on this item yourself. Your account has one max per item on Mac.bid, "
                            "so I can't keep theirs separate from yours")
        if hard_max <= int(st.price or 0):
            raise SiteError(f"Your max must be above the current bid (${int(st.price or 0)})")
        old = self.requests.get(key)
        if old and old.state == CONFIRMED:
            if old.person_id != person_id:
                raise SiteError(f"{self._person(old.person_id).name if self._person(old.person_id) else 'Someone'} already agreed on this item")
            old.hard_max = hard_max
            old.note(f"your max changed to ${hard_max}")
            await self._save()
            await self._tg(person.chat_id, f"Good news: I can now go up to ${hard_max} for you on {st.title}. "
                                           "Reply with a higher max if you want to raise yours.")
            return
        wish = old.wants if old and old.mode == "watch" and old.state in OPEN else 0
        req = ProxyRequest(lot_id=lot_id, person_id=person_id, hard_max=int(hard_max), site=site,
                           title=st.title, url=st.url)
        req.note(f"asked {person.name} (your max ${hard_max})")
        self.requests[key] = req
        if wish and wish <= int(hard_max):
            ok, _ = on_amount(req, wish, st.price)
            if ok:
                await self._save()
                await self._tg(person.chat_id, confirm_text(req.title, wish, self._estimate(req, wish)),
                               [f"I agree to pay:{AGREE} {req.id}", f"No thanks:{DECLINE} {req.id}"], photo=st.image)
                return
        await self._save()
        await self._tg(person.chat_id, ask_text(person.name, st.title, st.url, st.price, int(hard_max), None), photo=st.image)

    async def _ask_gov(self, person: Person, st: Any, key: str, hard_max: int) -> None:
        """GovAuctions: your max is in bids; they say how many bids they'll pay for (win or lose)."""
        if hard_max < 1:
            raise SiteError("Set the most bids you'll spend for them")
        coord = self._coords().get(SITE_GOVAUCTIONS)
        cost = float(coord.config_entry.options.get("point_cost", 0.13)) if coord else 0.13
        old = self.requests.get(key)
        if old and old.state == CONFIRMED:
            if old.person_id != person.id:
                raise SiteError("Someone else already agreed on this auction")
            old.hard_max = hard_max
            old.note(f"your max changed to {hard_max} bids")
            await self._save()
            await self._tg(person.chat_id, f"I can now spend up to {hard_max} bids for you on {st.title}. "
                                           "Reply with a higher total if you want more.")
            return
        wish = (old.wants, old.wants_price) if old and old.mode == "watch" and old.state in OPEN else (0, 0.0)
        req = ProxyRequest(lot_id=st.auction_id, person_id=person.id, hard_max=hard_max, site=SITE_GOVAUCTIONS,
                           point_cost=cost, title=st.title, url=st.url)
        req.note(f"asked {person.name} (your max {hard_max} bids)")
        self.requests[key] = req
        if wish[0] and wish[0] <= hard_max:
            ok, _ = on_bids(req, wish[0], wish[1] or None, st.price)
            if ok:
                await self._save()
                await self._tg(person.chat_id, gov_confirm_text(req.title, wish[0], cost, req.their_price),
                               [f"I agree to pay:{AGREE} {req.id}", f"No thanks:{DECLINE} {req.id}"], photo=st.image)
                return
        await self._save()
        await self._tg(person.chat_id, gov_ask_text(person.name, st.title, st.url, st.price, hard_max, cost), photo=st.image)

    async def async_cancel(self, site: str, lot_id: str) -> None:
        req = self.requests.get(f"{site}:{lot_id}")
        if req is not None:
            await self._stop(req, by_them=False)

    async def _stop(self, req: ProxyRequest, by_them: bool) -> None:
        """Stop a request, by you (page) or them (replying "stop"):
        - not agreed yet / just watching: closed, nothing was bid;
        - GovAuctions, agreed: booked bids cancelled (unused points come back), the bid limit set to what's used,
          and they're told how many of their bids were used (what they owe);
        - Mac.bid, agreed: the max bid can't be withdrawn there, so it stays; no raises, and they still hear how it ends."""
        if req.state not in OPEN or req.stopped:
            return
        person = self._person(req.person_id)
        who = "they" if by_them else "you"
        name = person.name if person else "them"
        if req.state == CONFIRMED and req.site == SITE_GOVAUCTIONS:
            coord = self._coords().get(SITE_GOVAUCTIONS)
            st = self._state(req.site, req.lot_id)
            used = gov_used(req, st.your_bids if st is not None else req.start_bids)
            try:
                if coord is not None and st is not None:
                    await coord.async_set_bid_limit(req.lot_id, int(st.your_bids or 0))  # stops auto-booking here
                    if st.your_booked_left:
                        await coord.async_cancel(req.lot_id)
            except (SiteError, AuthError) as err:
                req.note(f"couldn't cancel the booked bids: {err}")
                self._tell_owner(person, req, f"stopped, but the booked bids could NOT be cancelled: {err}. Cancel them on the site.")
            req.state = CANCELLED
            req.note(f"stopped by {who}: {used} of their bids used (${used * req.point_cost:.2f}); booked bids cancelled")
            msg = (f"Stopped bidding for you on {req.title}. {used} of your bids were used "
                   f"(${used * req.point_cost:.2f}), which is what you owe.")
        elif req.state == CONFIRMED and req.mode == "bid":
            req.stopped = True
            req.note(f"stopped by {who}: the ${req.bid_amount} max bid stays on Mac.bid (bids can't be withdrawn)")
            msg = (f"OK, no more raises on {req.title}. Your ${req.bid_amount} max bid can't be withdrawn on Mac.bid, "
                   "so it stays; I'll let you know how it ends.")
        else:
            was_watch = req.mode == "watch"
            req.state = CANCELLED
            req.note(f"stopped by {who}")
            msg = (f"OK, I've stopped updating you on {req.title}." if was_watch
                   else f"OK, cancelled: no bid on {req.title}.")
        await self._save()
        if person:
            await self._tg(person.chat_id, msg if by_them or not msg.startswith("OK, ") else "Never mind: " + msg[4:])
        if by_them:
            self._tell_owner(person, req, f"{name} stopped it. " + msg)

    async def async_set(self, enabled: bool | None = None, add: dict[str, Any] | None = None, remove: str | None = None) -> None:
        if enabled is not None:
            self.enabled = enabled
        if add:
            p = Person.from_dict(add)
            if not p.name or not p.chat_id.lstrip("-").isdigit():
                raise SiteError("A person needs a name and their numeric Telegram chat id")
            self.people.append(p)
        if remove:
            for req in [r for r in self.requests.values() if r.person_id == remove]:
                await self._stop(req, by_them=False)
            self.people = [p for p in self.people if p.id != remove]
        await self._save()

    # ---------------- from Telegram
    async def _on_text(self, event: Event) -> None:
        cid = event.data.get("chat_id")
        if cid is not None and len(self._seen) < 50:
            name = " ".join(x for x in (event.data.get("from_first"), event.data.get("from_last")) if x)
            self._seen[str(cid)] = name or str(cid)
        if not self.enabled:
            return
        person = self._person(chat_id=event.data.get("chat_id"))
        if person is None:
            return
        text = str(event.data.get("text") or "")
        mine = sorted((r for r in self.requests.values()
                       if r.person_id == person.id and r.state in OPEN and not r.stopped
                       and (r.mode == "bid" or wants_stop(text) or parse_amount(text) is not None)),
                      key=lambda r: (r.state == CONFIRMED, -r.created))
        if not mine:
            return
        req = mine[0]
        if wants_stop(text):
            await self._stop(req, by_them=True)
            return
        if req.mode == "watch":
            if req.site == SITE_GOVAUCTIONS:
                amount, price = parse_gov_reply(text)
            else:
                amount, price = parse_amount(text), None
            if not amount:
                return
            reply = on_wish(req, amount, price)
            await self._save()
            await self._tg(person.chat_id, reply)
            unit = f"{amount} bids" if req.site == SITE_GOVAUCTIONS else f"${amount}"
            self._tell_owner(person, req, f"wants you to bid up to {unit}" + (f" (max price ${price:.2f})" if price else "")
                             + (": place it on eBay by hand." if req.site == "ebay"
                                else ". Open the item's card, set your max and tap \"Ask them\"."))
            return
        if req.site == SITE_GOVAUCTIONS:
            await self._on_gov_text(person, req, text)
            return
        amount = parse_amount(text)
        if amount is None:
            await self._tg(person.chat_id, f"For {req.title}, reply with the most you'd pay as a bid, e.g. \"40\" (up to ${req.hard_max}).")
            return
        st = self._state(req.site, req.lot_id)
        if st is None or st.status != "live":
            await self._tg(person.chat_id, f"{req.title} isn't live anymore.")
            return
        live = await self._live(req)
        ok, why = on_amount(req, amount, live["price"] if live else st.price, min_next=live["min_next"] if live else None)
        await self._save()
        if not ok:
            await self._tg(person.chat_id, why)
            return
        est = self._estimate(req, amount)
        await self._tg(person.chat_id, confirm_text(req.title, amount, est),
                       [f"I agree to pay:{AGREE} {req.id}", f"No thanks:{DECLINE} {req.id}"])
        self._tell_owner(person, req, f"named a max of ${amount} (waiting for them to agree)")

    async def _on_gov_text(self, person: Person, req: ProxyRequest, text: str) -> None:
        bids, price = parse_gov_reply(text)
        if bids is None:
            await self._tg(person.chat_id, f"For {req.title}, reply with how many bids you'd pay for, e.g. \"40\" or \"40 $25\" "
                                           f"(up to {req.hard_max}).")
            return
        st = self._state(req.site, req.lot_id)
        if st is None or st.status not in ("live", "upcoming"):
            await self._tg(person.chat_id, f"{req.title} isn't live anymore.")
            return
        ok, why = on_bids(req, bids, price, st.price)
        await self._save()
        if not ok:
            await self._tg(person.chat_id, why)
            return
        await self._tg(person.chat_id, gov_confirm_text(req.title, bids, req.point_cost, req.their_price),
                       [f"I agree to pay:{AGREE} {req.id}", f"No thanks:{DECLINE} {req.id}"])
        self._tell_owner(person, req, f"offered {bids} bids" + (f" (max price ${req.their_price:.2f})" if req.their_price else "")
                         + "; waiting for them to agree")

    async def _agree_gov(self, person: Person, req: ProxyRequest, st: Any) -> None:
        """Book their bids: the limit is the bids you had before they agreed + what they agreed to."""
        first = req.state == CONFIRMING and not req.agreed_max
        if first:
            req.start_bids = int(st.your_bids or 0)
        amount = on_agree(req, None)
        await self._save()
        if not amount:
            return
        coord = self._coords().get(SITE_GOVAUCTIONS)
        self._tell_owner(person, req, f"agreed to pay for up to {amount} bids (${amount * req.point_cost:.2f}) win or lose")
        try:
            await coord.async_set_bid_limit(req.lot_id, req.start_bids + amount)
            auto = coord.auto_settings(req.lot_id)
            if auto.auto_rebook:
                if req.agreed_price:
                    await coord.async_set_auto(req.lot_id, replace(auto, max_price=req.agreed_price))
                req.note(f"auto-booking spends their bids (limit {req.start_bids + amount} bids in all)")
                msg = f"You're in on {st.title}: I'll spend up to {amount} bids for you when it's worth it."
            else:
                used = max(0, int(st.your_bids or 0) - req.start_bids)
                to_book = amount - used - int(st.your_booked_left or 0)
                if to_book > 0:
                    booked = await coord.async_book(req.lot_id, to_book, req.agreed_price or None)
                    req.note(f"booked {booked} bids for them")
                msg = f"Your bids are booked on {st.title}: up to {amount}" + (
                    f", stopping above ${req.agreed_price:.2f}." if req.agreed_price else ".")
            await self._tg(person.chat_id, msg)
        except (SiteError, AuthError) as err:
            req.note(f"bids not booked: {err}")
            await self._tg(person.chat_id, f"I couldn't book your bids on {req.title}: {err}")
            self._tell_owner(person, req, f"bids NOT booked: {err}")
        await self._save()

    async def _live(self, req: ProxyRequest) -> dict[str, Any] | None:
        """Mac.bid's item as it is right now (price, minimum next bid); None if it can't be read."""
        coord = self._coords().get(req.site)
        if coord is None or not hasattr(coord, "async_lot_now"):
            return None
        try:
            return await coord.async_lot_now(req.lot_id)
        except (SiteError, AuthError) as err:
            _LOGGER.debug("Couldn't read Mac.bid lot %s live: %s", req.lot_id, err)
            return None

    async def _on_callback(self, event: Event) -> None:
        if not self.enabled:
            return
        data = str(event.data.get("data") or "")
        if not data.startswith((AGREE, DECLINE)):
            return
        person = self._person(chat_id=event.data.get("chat_id"))
        rid = data.split(" ", 1)[1].strip() if " " in data else ""
        req = next((r for r in self.requests.values() if r.id == rid), None)
        if person is None or req is None or req.person_id != person.id:
            return
        await self._answer(event, "Got it")
        if data.startswith(DECLINE):
            on_decline(req)
            await self._save()
            await self._tg(person.chat_id, "OK, no bid." if req.state != CONFIRMED else f"OK, your max stays ${req.bid_amount}.")
            self._tell_owner(person, req, "said no thanks")
            return
        st = self._state(req.site, req.lot_id)
        if st is None or st.status not in ("live", "upcoming") or (req.site != SITE_GOVAUCTIONS and st.status != "live"):
            await self._tg(person.chat_id, f"{req.title} isn't live anymore, so no bid.")
            return
        if req.site == SITE_GOVAUCTIONS:
            await self._agree_gov(person, req, st)
            return
        live = await self._live(req)
        if live and req.their_max < live["min_next"]:
            await self._tg(person.chat_id, f"The bidding on {req.title} moved: the minimum bid is now ${live['min_next']}, "
                                           f"above your ${req.their_max}. Reply with a higher max (up to ${req.hard_max}) "
                                           "if you still want it.")
            return
        amount = on_agree(req, self._estimate(req, req.their_max))
        await self._save()
        if not amount:
            return
        self._tell_owner(person, req, f"agreed to pay up to ${req.agreed_max}; placing a ${amount} max bid")
        coord = self._coords().get(req.site)
        try:
            await coord.async_set_bid_limit(req.lot_id, amount)
            await coord.async_place_bid(req.lot_id, amount)
            req.note(f"sent a ${amount} max bid to Mac.bid")
        except (SiteError, AuthError) as err:
            req.note(f"bid not placed: {err}")
            await self._tg(person.chat_id, f"I couldn't place your bid on {req.title}: {err}")
            self._tell_owner(person, req, f"bid NOT placed: {err}")
        await self._save()

    async def _answer(self, event: Event, text: str) -> None:
        try:
            await self.hass.services.async_call("telegram_bot", "answer_callback_query",
                                                {"callback_query_id": event.data.get("id"), "message": text}, blocking=True)
            msg = event.data.get("message") or {}
            if msg.get("message_id"):
                await self.hass.services.async_call("telegram_bot", "edit_replymarkup",
                                                    {"message_id": msg["message_id"], "chat_id": event.data.get("chat_id"),
                                                     "inline_keyboard": []}, blocking=True)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Telegram button answer failed: %s", err)

    # ---------------- from the Mac.bid coordinator
    @callback
    def relay(self, site: str, kind: str, data: dict[str, Any]) -> None:
        """Tell the person how their item is going (outbid, won, ...)."""
        if not self.enabled or kind not in RELAYED:
            return
        req = self.requests.get(f"{site}:{data.get('auction_id')}")
        if req is None:
            return
        person = self._person(req.person_id)
        if kind in ("won", "ended") and req.state in (ASKED, CONFIRMING):
            req.state = CANCELLED
            req.note("the item ended before they agreed")
            self.hass.async_create_task(self._save())
            return
        if req.state not in (CONFIRMED, WATCHING) or person is None:
            return
        d = dict(data)
        if kind == "won" and req.site == SITE_GOVAUCTIONS:
            st = self._state(req.site, req.lot_id)
            d["pay_if_won"] = getattr(st, "pay_if_won", None) if st is not None else None
        elif kind == "won" and req.mode == "bid":
            st = self._state(req.site, req.lot_id)
            d["total"] = mb.win_cost(d.get("price"), st.extra.get("sales_tax") if st is not None else None)
        text = result_text(kind, req.title, d, req)
        if kind in ("won", "ended"):
            req.note("ended" if req.mode == "watch" else ("won" if kind == "won" else "lost"))
            if req.mode == "watch":
                req.state = CANCELLED  # done: no more updates
            self.hass.async_create_task(self._save())
        if kind == "bid_placed" and d.get("raised"):
            self._tell_owner(person, req, f"Mac.bid recorded ${d.get('amount')} instead of the ${d.get('sent')} they agreed to "
                                          "(its minimum bid). They've been told.")
        if text:
            st = self._state(req.site, req.lot_id)
            self.hass.async_create_task(self._tg(person.chat_id, text, photo=st.image if st is not None and kind in ("won", "ended") else None))

    def chats(self) -> list[dict[str, str]]:
        """Telegram chats you could add: the bot's allowed chats (Home Assistant's Telegram bot setup)
        plus chats that messaged the bot since Home Assistant started."""
        out: dict[str, dict[str, str]] = {}
        for e in self.hass.config_entries.async_entries("telegram_bot"):
            for sub in (getattr(e, "subentries", None) or {}).values():
                cid = (sub.data or {}).get("chat_id")
                if cid is not None:
                    out[str(cid)] = {"chat_id": str(cid), "name": clean_name(sub.title) or str(cid), "source": "allowed chat"}
        for cid, name in self._seen.items():
            out.setdefault(cid, {"chat_id": cid, "name": name, "source": "messaged the bot"})
        taken = {p.chat_id for p in self.people}
        return [c for c in out.values() if c["chat_id"] not in taken]

    def summary(self, site: str, lot_id: str) -> dict[str, Any] | None:
        req = self.requests.get(f"{site}:{lot_id}")
        if req is None:
            return None
        person = self._person(req.person_id)
        return {"id": req.id, "mode": req.mode, "site": req.site, "point_cost": req.point_cost, "stopped": req.stopped,
                "wants": req.wants, "wants_price": req.wants_price,
                "agreed_price": req.agreed_price, "start_bids": req.start_bids,
                "person": person.name if person else "?", "person_id": req.person_id, "state": req.state,
                "hard_max": req.hard_max, "their_max": req.their_max, "agreed_max": req.agreed_max,
                "bid": req.bid_amount, "estimate": req.estimate, "log": req.log[-6:]}

    def view(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "telegram_ready": self.telegram_ready(), "chats": self.chats(),
                "people": [to_dict(p) for p in self.people],
                "requests": [{**to_dict(r), "person": (self._person(r.person_id) or Person("?", "")).name}
                             for r in sorted(self.requests.values(), key=lambda r: -r.created)][:30]}


def get(hass: HomeAssistant) -> ProxyManager | None:
    return hass.data.get(DATA_KEY)


@websocket_api.websocket_command({vol.Required("type"): "auction_watcher/proxy"})
@websocket_api.require_admin
@callback
def ws_view(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    p = get(hass)
    if p is None:
        connection.send_error(msg["id"], "not_loaded", "Auction Watcher isn't set up yet")
        return
    connection.send_result(msg["id"], p.view())


@websocket_api.websocket_command({
    vol.Required("type"): "auction_watcher/proxy/change",
    vol.Required("action"): vol.In(["set", "ask", "cancel"]),
    vol.Optional("enabled"): bool,
    vol.Optional("add"): {vol.Required("name"): str, vol.Required("chat_id"): vol.Coerce(str)},
    vol.Optional("remove"): str,
    vol.Optional("lot_id"): vol.Coerce(str),
    vol.Optional("person_id"): str,
    vol.Optional("hard_max"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100000)),
    vol.Optional("site", default=SITE_MACBID): str,
    vol.Optional("mode", default="bid"): vol.In(["bid", "watch"]),
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_change(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    p = get(hass)
    if p is None:
        connection.send_error(msg["id"], "not_loaded", "Auction Watcher isn't set up yet")
        return
    try:
        if msg["action"] == "set":
            await p.async_set(msg.get("enabled"), msg.get("add"), msg.get("remove"))
        elif msg["action"] == "ask":
            await p.async_ask(msg["site"], msg.get("lot_id", ""), msg.get("person_id", ""), int(msg.get("hard_max") or 0), msg["mode"])
        else:
            await p.async_cancel(msg["site"], msg.get("lot_id", ""))
    except (SiteError, AuthError) as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    connection.send_result(msg["id"], p.view())


@callback
def async_register_ws(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_view)
    websocket_api.async_register_command(hass, ws_change)
