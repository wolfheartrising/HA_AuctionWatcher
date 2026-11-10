"""Bidding for someone else, in Home Assistant: Telegram questions and buttons, then the Mac.bid bid.

Off unless switched on (Settings tab). Only people on your list are listened to, and only through
Home Assistant's Telegram bot (they must have pressed /start, and be an allowed chat).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.storage import Store

from . import global_settings
from .const import CONF_SITE, DOMAIN, SITE_GOVAUCTIONS, SITE_MACBID, STORAGE_VERSION
from .proxy import (ASKED, CANCELLED, CONFIRMED, CONFIRMING, HEADER, OPEN, WATCHING, Person, ProxyRequest, ask_text,
                    confirm_text, on_agree, on_amount, on_decline, parse_amount, result_text, to_dict, watch_text)
from .sites import macbid as mb
from .sites.base import AuthError, SiteError

_LOGGER = logging.getLogger(__name__)

DATA_KEY = f"{DOMAIN}_proxy"
AGREE, DECLINE = "/aw_agree", "/aw_no"
RELAYED = {"bid_placed", "bid_failed", "outbid", "ending_soon", "won", "ended"}


class ProxyManager:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.proxy")
        self.enabled = False
        self.people: list[Person] = []
        self.requests: dict[str, ProxyRequest] = {}   # "site:item id" -> request
        self._unsubs: list[Any] = []

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
            await self._tg(person.chat_id, watch_text(person.name, st.title, st.url, st.price), photo=st.image)
            return
        if site != SITE_MACBID:
            raise SiteError("Bidding for someone is Mac.bid only for now; use \"Just keep them posted\" here")
        if st.status != "live":
            raise SiteError("That Mac.bid item isn't live")
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
        req = ProxyRequest(lot_id=lot_id, person_id=person_id, hard_max=int(hard_max), site=site,
                           title=st.title, url=st.url)
        req.note(f"asked {person.name} (your max ${hard_max})")
        self.requests[key] = req
        await self._save()
        await self._tg(person.chat_id, ask_text(person.name, st.title, st.url, st.price, int(hard_max), None), photo=st.image)

    async def async_cancel(self, site: str, lot_id: str) -> None:
        req = self.requests.get(f"{site}:{lot_id}")
        if req is None or req.state not in OPEN:
            return
        person = self._person(req.person_id)
        was_confirmed = req.state == CONFIRMED
        req.state = CANCELLED
        req.note("cancelled by you" + (" (the max bid already placed stays on Mac.bid)" if was_confirmed else ""))
        await self._save()
        if person:
            await self._tg(person.chat_id, f"Never mind about {req.title}: I've stopped "
                           + ("updating you on it." if req.mode == "watch" else "that request.")
                           + (" A bid already placed can't be withdrawn on Mac.bid; I'll let you know how it ends."
                              if was_confirmed else ""))

    async def async_set(self, enabled: bool | None = None, add: dict[str, Any] | None = None, remove: str | None = None) -> None:
        if enabled is not None:
            self.enabled = enabled
        if add:
            p = Person.from_dict(add)
            if not p.name or not p.chat_id.lstrip("-").isdigit():
                raise SiteError("A person needs a name and their numeric Telegram chat id")
            self.people.append(p)
        if remove:
            self.people = [p for p in self.people if p.id != remove]
        await self._save()

    # ---------------- from Telegram
    async def _on_text(self, event: Event) -> None:
        if not self.enabled:
            return
        person = self._person(chat_id=event.data.get("chat_id"))
        if person is None:
            return
        text = str(event.data.get("text") or "")
        mine = sorted((r for r in self.requests.values()
                       if r.person_id == person.id and r.mode == "bid" and r.state in OPEN),
                      key=lambda r: (r.state == CONFIRMED, -r.created))
        if not mine:
            return
        req = mine[0]
        amount = parse_amount(text)
        if amount is None:
            await self._tg(person.chat_id, f"For {req.title}, reply with the most you'd pay as a bid, e.g. \"40\" (up to ${req.hard_max}).")
            return
        st = self._state(req.site, req.lot_id)
        if st is None or st.status != "live":
            await self._tg(person.chat_id, f"{req.title} isn't live anymore.")
            return
        ok, why = on_amount(req, amount, st.price)
        await self._save()
        if not ok:
            await self._tg(person.chat_id, why)
            return
        est = self._estimate(req, amount)
        await self._tg(person.chat_id, confirm_text(req.title, amount, est),
                       [f"I agree to pay:{AGREE} {req.id}", f"No thanks:{DECLINE} {req.id}"])
        self._tell_owner(person, req, f"named a max of ${amount} (waiting for them to agree)")

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
        if st is None or st.status != "live":
            await self._tg(person.chat_id, f"{req.title} isn't live anymore, so no bid.")
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
        if kind == "won" and req.mode == "bid":
            st = self._state(req.site, req.lot_id)
            d["total"] = mb.win_cost(d.get("price"), st.extra.get("sales_tax") if st is not None else None)
        text = result_text(kind, req.title, d, req)
        if kind in ("won", "ended"):
            req.note("ended" if req.mode == "watch" else ("won" if kind == "won" else "lost"))
            if req.mode == "watch":
                req.state = CANCELLED  # done: no more updates
            self.hass.async_create_task(self._save())
        if text:
            st = self._state(req.site, req.lot_id)
            self.hass.async_create_task(self._tg(person.chat_id, text, photo=st.image if st is not None and kind in ("won", "ended") else None))

    def summary(self, site: str, lot_id: str) -> dict[str, Any] | None:
        req = self.requests.get(f"{site}:{lot_id}")
        if req is None:
            return None
        person = self._person(req.person_id)
        return {"id": req.id, "mode": req.mode, "person": person.name if person else "?", "person_id": req.person_id, "state": req.state,
                "hard_max": req.hard_max, "their_max": req.their_max, "agreed_max": req.agreed_max,
                "bid": req.bid_amount, "estimate": req.estimate, "log": req.log[-6:]}

    def view(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "telegram_ready": self.telegram_ready(),
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
