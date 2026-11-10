# Changelog

Each version gets a GitHub release automatically when it reaches `main` (see `.github/workflows/release.yml`).
The section for a version becomes its release notes, so HACS shows them under "Update available".

After updating: restart Home Assistant if Python changed, then reload the Auctions page (it offers a Reload button
when it's older than the plugin).

## 1.6.1
- eBay setup now supports either **official API keys** (App ID + Cert ID, optional RuName sign-in) **or your own
  eBay browser cookies** for owner-only no-API mode.
- In cookie mode, Auction Watcher can follow your own eBay watchlist, bids and saved-search-backed items without
  developer keys.
- eBay setup text and errors now clearly explain the two sign-in paths.

## 1.6.0
- **Telegram fixed:** replies now go to each chat's notify entity, which newer Home Assistant requires ("Please specify
  at least 1 notify entity"), so commands and buttons finally answer.
- **Locked to your chosen bot:** messages from other Telegram bots on the same Home Assistant are ignored.
- **The bot for you:** set your own chat under Settings → For others. Then /status, /active (quick bid and Stop per
  item), /pause, /resume, /help; your outbid / few-bidders / good-pack alerts with buttons; and "X wants you to bid"
  requests with Allow buttons. Anything that spends asks you to confirm.
- Requests: **Forget** (remove a record) and **Reassign** (when its person was removed); the card no longer shows a
  wish on closed requests or defaults to a person who's gone.

## 1.5.8
- For others: with debug logging on, every Telegram message, button tap and command is logged with what happened to it
  (or why it was ignored), to track down buttons/commands that seem to do nothing. People are also recognised by their
  user id when a tap arrives without a chat id.

## 1.5.7
- For others: ✎ next to each person to rename them or fix their chat id; their open items stay with them. (Switching
  everyone to another bot is just the Telegram bot picker: chat ids are the same with every bot.)

## 1.5.6
- Pressing **Start** before being added gets a "you're not set up yet" reply, and that person now appears in the
  Settings → For others dropdown ("messaged the bot").

## 1.5.5
- For others bot commands: **/items**, **/stop** (asks which if several), **/help**, and a short explanation when they
  press **Start**. Suggested BotFather command list in the README.

## 1.5.4
- For others: choose which **Telegram bot** talks to people (Settings → For others), so a dedicated "auctions" bot can be
  used alongside your main one. All messages and buttons go through it, and the chat list shows only its allowed chats.

## 1.5.3
- Quick bids: **+1 bid** on GovAuctions cards (one bid, placed right away: stake a claim before the entry limit) and
  **Bid $X (next step)** on Mac.bid cards (the smallest bid Mac.bid accepts, read live when you tap). One confirmation;
  your max, budget and spending cap still apply. New action `auction_watcher.quick_bid`.

## 1.5.2
- A red **■ Stop** button on any card with something automatic running. GovAuctions: auto-cancel, auto-rebook and
  top-up off, and booked bids still waiting cancelled. Mac.bid: the scheduled late bid off (a placed max can't be
  withdrawn there). New action `auction_watcher.stop`.

## 1.5.1
- Point packs: a **Follow** button per pack auction bookmarks it on GovAuctions and sets its Max bids to the suggested
  max, so it gets a full card (Book, auto-booking, limits). Following never bids by itself.
- Point packs: the timer of a live pack now reads "live · first countdown …" (bidding is already open), not "starts in".
- New action `auction_watcher.follow`.

## 1.5.0
- For others (Telegram): every message has buttons tied to its item (amounts in Mac.bid's real steps or GovAuctions
  bid counts, Other amount, No thanks / Stop, I'd like to bid). A typed number or "stop" with several items open asks
  "which item?" instead of guessing. `/items` lists their open items.

## 1.4.2
- Mac.bid: "Lower my max to $X" (the current price) while you're winning, checked live; your saved max follows so
  auto-bid won't raise it again.

## 1.4.1
- Saved searches no longer lose their results, "already seen" list and last run on restart (they're loaded before
  anything can use them, and never saved before that). Same start-up protection for global settings and For others.

## 1.4.0
- Mac.bid bids use only the amounts its own "Set your max bid" list offers ($1 steps to $100, $5 to $500, $10 to
  $1,000, then $50). The Max bid box is that list; automatic bids round down, never up.

## 1.3.8
- Requests use Home Assistant's standard web client identity instead of naming the plugin.

## 1.3.7
- Mac.bid auto-bids stop after 2 bids Mac.bid didn't record (no endless retries), check the live minimum first, and
  only items you're in are checked every few seconds near the end.

## 1.3.6
- Mac.bid items you're bidding on are checked every 2 minutes (every minute in their last 2 hours).

## 1.3.5
- Mac.bid: live price check before accepting or placing a bid; reads back the max Mac.bid recorded. Names drop the
  Telegram chat id; "bid for someone" is refused on items you're bidding on yourself.

## 1.3.0 – 1.3.4
- For others on GovAuctions (they agree to pay for N bids, win or lose, plus the final price), cancelling properly
  (booked bids cancelled), "stop" replies, and asking to bid while just watching. Reload banner when the page is
  older than the plugin. Telegram chat picker.

## 1.2.0
- For others: bid on Mac.bid items for people on your list after they confirm on Telegram, or just keep them posted.

## 1.1.x
- Global ⚙ Settings tab (where alerts go, which alerts), per-site settings on each site tab, one spending cap across
  all sites (weekly or monthly, block or warn), ☆ add search results to the site's watchlist / bookmarks, Mac.bid
  sign-in recovery (Reconfigure instead of retrying forever).

## 1.0.x
- Saved searches across sites with Mac.bid keyword sync and pickup-location filters, auto-rebook top-up, faster
  Mac.bid condition notes, point packs grouped by size.
