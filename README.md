# Auction Watcher for Home Assistant

> **Unofficial hobby project.** Not affiliated with, endorsed by, or supported by Home Assistant,
> GovAuctions, Mac.bid, eBay or any other auction site. Built for my own personal use. See the [Disclaimer](#disclaimer).

Follow the online auctions you're bidding on or watching, with a dashboard,
phone alerts and spending limits. **Supports GovAuctions, Mac.bid and eBay**; more sites
(GovDeals, ...) are planned, one handler per site.

Auction Watcher reads your auctions. It only **books, cancels or places bids**
(GovAuctions booked bids, Mac.bid max bids) when you press a button, call a
service, or turn on auto-booking / auto-bidding for a specific auction, and never
past the limit you set for that auction.

## What it does (GovAuctions)

- **Its own "Auctions" page in the sidebar** (admins only): Active / Watching / Ended / per-site tabs and a sort menu (name, price, fewest bidders, your bids, clock). A card per auction with picture, price, clock, other bidders, your bids and cost, past winners, what each special label (1¢, Win-Win, No Re-Entry, ...) means, and **Max bids** and **Max price** with a calculated **total max cost**. What you type is kept until you save, even while the page updates. Long countdowns tick every second; the short penny-auction clock shows its value "as of" the last check, since every bid resets it.
- **Follows everything you're in**: your My Bids list, your watch list (favorites), plus any extra auction IDs you add.
- **One sensor per auction**: state is the price. Attributes hold the clock, leader, **other active bidders** (everyone except you seen bidding in the last 3 minutes, configurable, so people who pause and jump back in still count), whether it's closed to new bidders, your bids and what they cost, booked bids left, labels (1¢, Win-Win, ...), entry limit, Buy It Now, and **what winning right now would cost you**.
- **Price history**: past wins of the same product, with median final price, the median number of bids winners used, and the site's own cost estimate.
- **Polls only as fast as needed**: every 10 s while something you follow is live, every minute shortly before one starts, every 45 min otherwise. The slow check also keeps the login fresh, and refreshed cookies are saved automatically. While the Auctions page is open and visible and a penny auction is in its 5-second phase, it checks every 3 s.
- **Original end and overtime**: the card shows when the first countdown ran out (the site's own start time) and a live **overtime** counter after that (also an `overtime_seconds` attribute).
- **Alerts**, each sent once (not every poll):
  - **Few bidders left**: down to ≤ N *other* bidders (default 1: you against one person), in the real 5-second phase (not during the long first countdown), under your max price, and you're allowed to bid. Optionally only after it has stayed that low for N seconds (default 0: right away, since 2 bidders can end an auction in seconds).
  - **Bid limit reached**: you've spent your limit on an auction.
  - **Auction ended** and **auction won**.
- **Built-in delivery**, picked in ⚙️:
  - **Phone push** (`notify.mobile_app_…`): tapping it opens the auction.
  - **Telegram**: its notify service or notify entities, with the auction link in the message.
  - **Alexa**: Alexa Media Player services are sent as announcements; for the official Alexa Devices integration pick its *Announce* or *Speak* entity. Alexa gets a short spoken version.
  - Anything else Home Assistant can notify (email, the bell-icon notification, ...).
- **Ended auctions stay for 3 days** (a setting) with the final price, winner and what you spent, then they're removed along with their sensors. Auctions you stop following are kept too, marked *no longer followed*. This survives restarts.
- **Events** too (`auction_watcher_opportunity`, `…_bid_limit_reached`, `…_auction_ended`, `…_auction_won`), so you can build your own automations, like flashing a light. The old blueprint in `blueprints/` still works but isn't needed.
- **Bid limits**: a default limit per auction in the options, and `auction_watcher.set_bid_limit` for a single auction. At the limit you get one alert and no more "few bidders left" alerts for that auction.

## Point packs (GovAuctions)

A **Point packs** tab, from the site's own Packs filter (`/api/v1/browse?cat=7`), checked **hourly** (live price and entry state every 10 minutes). About 45 light requests per hour; turn it off in the GovAuctions settings.

For each pack (1¢ auctions: break-even = the pack size in bids):
- **Grade** from that pack's own past winners, by time of day. Cautious: it uses the worse of the window the auction starts in and the next one, since late-night fights cost much more. Fewer than 15 past wins: no grade.
- **Stars**: ★★★ winners usually needed ≤35% of the pack, ★★ ≤45%, ★ ≤60%, *skip* above that.
- **Suggested max**: covers most past wins (~60%) but never more than half the pack, so a win at the max always pays. Shows its cost, what a win nets, and how many past winners fit under it.
- **Affordable?** Packs whose suggested max is more than your point balance are flagged and listed last.
- **Entry**: still open to new bidders, and roughly how many bids until it locks.
- When you join a pack, its **Max bids** is set to the suggested max automatically, unless you set one.
- **Alert** (*Good point pack open to join*): once per ★★★ pack you can afford that's still open.

## Mac.bid

Timed liquidation auctions: you pay what you bid, plus fees, and pick up in person.

- **Login**: your Mac.bid email and password (recommended; the first time, Mac.bid texts or emails a verification code, which the setup asks for), or your browser's saved login (`mac_auths` + `device_id` from the browser's Local Storage). Mac.bid's login lasts 30 minutes and is renewed automatically; the renewal chain lasts 60 days and extends itself, and with email + password it can always log in fresh. Mac.bid hands out a new renewal token every time, so a pasted browser login is taken over by Home Assistant: log in again in your browser afterwards.
- **Follows** your watchlist and the lots in your recent bid history (plus any extra lot IDs you add). Each lot is checked only as often as it needs: every check in its last 15 minutes, every 2 minutes in its last 2 hours, every 15 minutes otherwise. If Mac.bid says "too many requests", the last known data is kept and it backs off.
- **Cards** show a real countdown to the end (with an *extended* badge when late bids push it back, and the original end time), **you're winning / outbid**, other bidders, watchers, retail and Buy Now prices, and **what winning costs**: the bid plus Mac.bid's 15% buyer's premium, $3 lot fee and the pickup building's sales tax.
- **Condition notes** like the item page: 🟩 TESTED, 🟥 DAMAGED, 🟨 PARTIAL with the inspector's note, the full description in a sub-menu, and what the condition label means (Like New / Open Box / Damaged, from Mac.bid's FAQ).
- **Location**: pickup spot with address and hours, the pickup window (3 business days after it closes), and a **Transfers** sub-menu: whether the item can be moved, and to which locations, each marked **free (pickup outpost)** or **$10** (free for Buyers Club members; up to 7 days to arrive, then 7 days to pick up).
- **Bidding**, like the site: type your **Max bid** and press **Bid now**. Mac.bid raises your bid for you up to that max ("set your max and done"). It shows the minimum next bid, asks you to confirm (with what a win at your max costs including fees and tax, and a reminder that bids can't be cancelled), then offers **Buyer's Assurance with its price** (return guarantee; not offered on Damaged items, per Mac.bid's FAQ). Each bid is checked against Mac.bid's own data before it counts as placed.
  - **Auto-bid (optional)**: place your max automatically N minutes before the end (default 3), optionally with Buyer's Assurance. Never past your max, and no further bids once someone's max beats yours.
  - **Mac.bid budget** (setting): the most dollars tied up in max bids at once (placed bids still live + scheduled late bids).
- **Pause switch** (*Auto-bidding*) and an alert for every bid placed or failed.

- **Awaiting pickup** tab: won items with a countdown to the pickup deadline, checked every 6 hours.
- **Alerts**: outbid, ending soon (default 10 minutes before), price passed your max, won / ended, and a pickup reminder (default 24 hours before the deadline).

## eBay

Uses eBay's **official APIs** only (no page scraping, which eBay blocks and its user agreement forbids).

- **Keys**: create a free account at developer.ebay.com and a **Production keyset** (App ID + Cert ID). eBay also asks every app to handle "account deletion" notices or apply for an exemption; this integration stores no other users' data, so apply for the exemption.
- **Level A, keys only**: follows the eBay items you add (item numbers or ebay.com/itm links): price, bids, minimum next bid, shipping, end time.
- **Level B, sign in**: add your **RuName** (developer portal, User Tokens) and the setup shows a sign-in link. Sign in on eBay, then paste back the address of the page eBay shows afterwards. Your **watchlist and everything you've bid on** are then followed automatically, with **winning / outbid** and your max bid. The sign-in lasts about 18 months and renews itself until then.
- **Alerts**: outbid, ending soon, price passed your max, won, ended. Checks every 10 s in an item's last 15 minutes, every 2 minutes in its last 2 hours, otherwise every 15 minutes.
- **No bidding**: eBay only lets approved apps bid through its API. Use eBay's own max bid.
- *Status*: built from eBay's documented formats and tested on sample data; not yet run against live eBay.

## Saved searches

The **Saved searches** tab looks for keywords on every site you've set up, once a day
(8:00 AM by default; change the time on the tab), optionally again every 1–12 hours for fresher prices and
quicker alerts, or when you tap **Search now**. The open page is told when results change (no constant polling). It uses each
site's own search: what typing in its search box does.

- **☆ Star** a result to add it to that site's own **bookmarks** (GovAuctions) or **watchlist** (Mac.bid, eBay); it then shows on the Watching tab. ★ means you already follow it.
- **Add** a keyword, pick the sites, optionally a max price. Tick *Also save to my Mac.bid alerts* to add it there too.
- Your **Mac.bid keyword alerts** (Account → Notifications) come in automatically. Colored dots show where a search
  is also saved: red GovAuctions, green Mac.bid, blue eBay. The same keyword from two places is one search.
- **Deleting** a search here also deletes it on Mac.bid (same as the trash can on its Notifications page).
- **Results** from all sites are in one list, soonest-ending first, with **NEW** on matches since the last run.
  Filter by search, by site, or new only. Each new match is alerted once (alert type *New matches for saved searches*).
  A search's first run only records what's already listed, so you don't get alerts for everything at once.
  The alert lists each search with new matches ("bluey - 2 new"), then the ones with nothing new; no message when nothing is new.
- **Mac.bid pickup locations**: a sub-menu of toggles, starting from the locations on your Mac.bid account.
  Results include items at those buildings or transferable to them ("transfer needed"); the tab notes how many results
  the location filter excluded. Changing a toggle searches again a few seconds later.
- GovAuctions results skip finished auctions and flag ones already **closed to new bidders**.
- Automations: the action `auction_watcher.search_now`, and the event `auction_watcher_search_matches` (with the new items).

## For others (household bidding, opt-in)

Off unless you switch on **Bid for others** on the ⚙ Settings tab and add people (name + Telegram chat id). Needs Home
Assistant's **Telegram bot** integration; each person must have pressed /start with your bot and be an allowed chat. Only
people on your list are listened to. Bids are placed from **your** account (like a household account).

- **Bid for them (Mac.bid):** on an item, *Bid for someone* → pick the person, set **your max**, *Ask them*. They get a
  Telegram message (item photo, "🔔 Auction Watcher" on top), reply with their max (up to yours), then tap **I agree to pay**
  (the message shows the estimate with the 15% premium, $3 lot fee and tax). Only then is a max bid placed: the lower of their
  max and yours. Your budgets and spending cap still apply. They hear when they're outbid (and can reply with a higher max,
  which needs a new "I agree"), when it ends soon, and how it ends. Every step is logged on the card.
- **Just keep them posted (any site):** they get a message now, when it's about to end, and when it ends. Nothing is bid.
- **For others tab:** everything you're doing for someone, with a sub-tab per person.
- eBay only lets approved partners bid through its API, so eBay is "keep them posted" only.

## Money overview and budgets

**Spending cap (all sites)** on the ⚙ Settings tab: one dollar cap per **week** (Mon–Sun) or **month**, either
**stopping** bids and bookings that would go past it or **warning** only. It counts GovAuctions points used plus points held
in booked bids (at your point cost), Mac.bid max bids on live items plus scheduled late bids, and Mac.bid items won this
period (winning price, before fees and tax). The Active tab shows a bar with what's used per site; one alert when it's
reached. Each site's own budget below still applies too.

The **Active** tab starts with a tally per site:
- **GovAuctions**: balance (per the site; booked bids are already taken out of it), **booked**, **earmarked** (what auto-rebook could still book under your max bids), **at risk**, spent today and this week. Set a **weekly budget** in the settings: when it's used up, auto-booking stops and the Book button refuses until Monday. Several auctions booking at once draw from one shared balance, so they can't spend the same points twice.
- **Mac.bid**: max bids placed on live items, scheduled late bids, your Mac.bid budget, and won items awaiting pickup.
- A **pause/resume** button per site (also a switch entity for automations).
- *Item + points* bundles show the bonus points' value and the net cost of winning.

## Booking and auto-booking (GovAuctions)

Each auction card on the **Auctions** page has a *Booking & auto* section. These spend real points.

- **Book bids / Cancel booked**: the same calls as the site's own buttons (`auction_watcher.book_bids`, `auction_watcher.cancel_booked_bids`). Manual booking is clamped to the auction's bid limit and your balance. The site places the first booked bid right away, so booking 1 is the same as bidding once; cancelling returns only the bids not yet placed.
- **Max bids** and **Max price** (top of each card, saved separately): Max bids is the total you'll spend there including bids already placed; Max price is handed to the site's own price cap. The card shows the **total max cost** = max bids × point cost + what you'd pay to win at your max price (1¢ on 1¢ auctions).
- **Auto-cancel when crowded** (off by default): when the auction has more bidders than your alert number, your booked bids are cancelled and the unused points come back.
- **Auto-rebook when it thins out** (off by default): when it's back down to your alert number and you have nothing booked, it books again: either a **fixed amount** or **what was left when it auto-cancelled**.
- **Top up early** (optional, per auction): "Top up when 3 or fewer booked bids are left" books more *before* you run out,
  so you don't drop out mid-fight. Booking while bids are still booked adds to them (same as the site's own button).
  Bids still booked count toward Max bids, and if your balance changed it books what it can instead of failing.

Safety rules, always on:
- Auto-booking can't be turned on for an auction without a **bid limit**, and never books past it or past your balance.
- It only acts in the real 5-second phase, never during the first long countdown.
- It **never auto-cancels where that would lock you out**: *No Re-Entry* auctions, or auctions closed to new bidders that you haven't bid in.
- At most one action per auction every 30 seconds; every booking, cancel or failure sends an alert (*Bids booked or cancelled* in the alert settings).

**Who can use it:** the Auctions page and every action that books, cancels or changes limits are admin-only (automations and scripts you write still work). Home Assistant has no per-user permissions for sensors, so non-admin users could still find the auction sensors if they go looking.

This is automated use of your account. Check the site's terms and decide for yourself; you are responsible for what it spends.

## Install

1. Copy `custom_components/auction_watcher` into `/config/custom_components/` (or add this repo to HACS as a custom repository).
2. Restart Home Assistant.
3. Settings → Devices & services → **Add integration** → *Auction Watcher*.
4. Pick the site, then log in:
   - **GovAuctions**: upload or paste your cookies (see below).
   - **Mac.bid**: email and password; the first time, type in the code Mac.bid texts or emails you. (Or paste the browser's saved login.)
   - **eBay**: your developer App ID + Cert ID, plus your RuName to sign in to your account (see the eBay section).
5. Open the **Auctions** page in the sidebar → **⚙ Settings** to choose where alerts go (phone, Telegram, Alexa) and which ones; each site's tab → ⚙ Settings for its rules, budgets and timing.
6. Open **Auctions** in the sidebar. (A YAML dashboard is also in `dashboards/` if you prefer building your own.)

**After every update**: restart Home Assistant if the release changed Python code, then hard-refresh the Auctions page (Ctrl+F5; in the phone app: Settings → Companion app → Troubleshooting → Reset frontend cache). Browsers sometimes keep the old page.

### Getting the cookies

Log in to the site in your browser, then either:
- export cookies with a "cookies.txt" browser extension and **upload that file** in the setup form, or
- open developer tools (F12) → Network, click any request to the site, copy the **Cookie** request header, and paste it.

Only the cookies for the site you picked are kept. You can upload a file that holds every site's cookies. After setup, delete the exported file: it works like a password.

Why cookies and not a password: GovAuctions' login uses a CAPTCHA, which an integration can't (and shouldn't) get around. The "remember me" cookie lasts about a year. If it ever stops working, Home Assistant shows a re-login prompt where you paste fresh cookies.

The cookies are stored in your Home Assistant config like any other integration's credentials. Treat backups accordingly.

## Options

Settings live on the **Auctions page**:

- **⚙ Settings tab** (shared by every site and saved searches): **where alerts go** (phone app, Telegram, Alexa, notify entities),
  **which alerts** to send (each shows colored dots for the sites that send it), and **Send test alert**. Upgrading brings your
  old per-site alert targets over automatically.
- **Each site's tab → ⚙ Settings**: that site's own options below (the same form as Devices & services → Configure).

| GovAuctions | Default |
|---|---|
| Check point packs hourly | on |
| Alert when *other* active bidders are at most | 1 (you against one) |
| Count anyone who bid within the last N minutes | 3 |
| …only after it stays that low for N seconds | 0 (right away) |
| Only alert below this price (0 = any) | 0 |
| Default max bids per auction (0 = none) | 0 |
| Weekly budget in points (0 = none) | 0 |
| What one point costs | $0.13 |
| Check every N s while live / every N min when idle | 10 s / 45 min |
| Keep ended auctions | 3 days |

| Mac.bid | Default |
|---|---|
| Ending-soon alert | 10 min before |
| Pickup reminder | 24 h before the deadline |
| Default max bid ($, 0 = none) | 0 |
| Most dollars tied up in max bids (0 = no limit) | 0 |
| Check every N s in an item's last 15 min / idle | 10 s / 45 min (max 15) |
| Keep ended auctions | 3 days |

| eBay | Default |
|---|---|
| Ending-soon alert | 10 min before |
| Default max price ($, 0 = none) | 0 |
| Check every N s in an item's last 15 min / idle | 10 s / 45 min |
| Keep ended auctions | 3 days |

All sites: extra auction IDs to follow.

## Notes and limits

- **Mac.bid sign-in:** Mac.bid replaces its login token on every renewal and the old one stops working. If Home Assistant
  is restored from a backup or rolled back, it has an old token: you'll see "needs you to sign in again" (a banner on the
  Auctions page and *Reconfigure* in Devices & services). Sign in there; **don't delete the account**, or its saved max
  bids and settings are lost. With your email and password saved, it signs itself back in when it can.

- GovAuctions "other bidders" counts everyone except you seen bidding within the window (3 minutes by default). The site only shows the last 9 bids, so the integration remembers names across checks.
- People with booked bids only show up once their bids actually fire; anyone who bid before the entry limit can come back (except in No Re-Entry auctions).
- Mac.bid accepts a bid request first and processes it after, so a bid only counts as placed once Mac.bid's own data shows you as a bidder.
- Respect each site's terms. Auction Watcher identifies itself in its user agent and polls modestly; it does not try to look like a person.

## Development

The site handlers and alert logic don't depend on Home Assistant:

```bash
python -m pytest tests                              # includes a pyflakes check of every file for undefined names
python tools/live_check.py path/to/cookies.txt      # read-only check of your real GovAuctions account
```

The Home Assistant parts (`coordinator.py`, `config_flow.py`, ...) can't be imported without Home Assistant, so the pyflakes test is what catches a missing import before it reaches a real install.

## Disclaimer

Auction Watcher is an unofficial hobby project I built for my own personal use and share as-is.
It is **not affiliated with, endorsed by, sponsored by or supported by** Home Assistant / the Open Home
Foundation, GovAuctions, Mac.bid, eBay, GovDeals, Capital City Online Auctions, OLA or any other auction
site. Those names and logos are trademarks of their respective owners and appear only to show which sites
the plugin works with.

The sites can change how they work at any time, which may break features without notice. You're responsible
for how you use it: follow each site's terms, and check your limits and settings before turning on anything
that books or places bids. There's no warranty, and I'm not responsible for missed auctions, unwanted bids,
spending, or account issues.
