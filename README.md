# Auction Watcher for Home Assistant

Follow the online auctions you're bidding on or watching, with a dashboard,
phone alerts and spending limits. **Supports GovAuctions, Mac.bid and eBay**; more sites
(GovDeals, ...) are planned, one handler per site.

Auction Watcher reads your auctions. It only **books or cancels bids** when you
press a button, call a service, or turn on auto-booking for a specific auction,
and never past the bid limit you set for that auction.

## What it does (GovAuctions)

- **Its own "Auctions" page in the sidebar** (admins only): Active / Watching / Ended / per-site tabs and a sort menu (name, price, fewest bidders, your bids, clock). A card per auction with picture, price, clock, other bidders, your bids and cost, past winners, what each special label (1¢, Win-Win, No Re-Entry, ...) means, and **Max bids** and **Max price** with a calculated **total max cost**. What you type is kept until you save, even while the page updates. Long countdowns tick every second; the short penny-auction clock shows its value "as of" the last check, since every bid resets it.
- **Follows everything you're in**: your My Bids list, your watch list (favorites), plus any extra auction IDs you add.
- **One sensor per auction**: state is the price. Attributes hold the clock, leader, **other active bidders** (everyone except you seen bidding in the last 3 minutes, configurable, so people who pause and jump back in still count), whether it's closed to new bidders, your bids and what they cost, booked bids left, labels (1¢, Win-Win, ...), entry limit, Buy It Now, and **what winning right now would cost you**.
- **Price history**: past wins of the same product, with median final price, the median number of bids winners used, and the site's own cost estimate.
- **Polls only as fast as needed**: every 10 s while something you follow is live, every minute shortly before one starts, every 45 min otherwise. The slow check also keeps the login fresh, and refreshed cookies are saved automatically.
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
- **Cards** show a real countdown to the end (with an *extended* badge when late bids push it back), **you're winning / outbid**, other bidders, watchers, retail and Buy Now prices, and **what winning costs**: the bid plus Mac.bid's 15% buyer's premium and $3 lot fee (before tax).
- **Condition notes** like the item page: 🟩 TESTED, 🟥 DAMAGED, 🟨 PARTIAL with the inspector's note, and the full description in a sub-menu.
- **Location**: pickup spot with address and hours, and a **Transfers** sub-menu (whether the item can be moved, and to which locations). "If you win" includes that building's sales tax.
- **Bidding**, like the site: type your **Max bid** and press **Bid now**. Mac.bid raises your bid for you up to that max ("set your max and done"). It shows the minimum next bid, asks you to confirm (with what a win at your max costs including fees and tax), then offers **Buyer's Assurance with its price** (5-business-day return guarantee). Each bid is checked against Mac.bid's own data before it counts as placed.
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

## Money overview and budgets

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
4. Pick the site and upload or paste your cookies (see below).
5. Click the ⚙️ on the account: one page with alert rules, where alerts go (phone, Telegram, Alexa) and timing.
6. Open **Auctions** in the sidebar. (A YAML dashboard is also in `dashboards/` if you prefer building your own.)

### Getting the cookies

Log in to the site in your browser, then either:
- export cookies with a "cookies.txt" browser extension and **upload that file** in the setup form, or
- open developer tools (F12) → Network, click any request to the site, copy the **Cookie** request header, and paste it.

Only the cookies for the site you picked are kept. You can upload a file that holds every site's cookies. After setup, delete the exported file: it works like a password.

Why cookies and not a password: GovAuctions' login uses a CAPTCHA, which an integration can't (and shouldn't) get around. The "remember me" cookie lasts about a year. If it ever stops working, Home Assistant shows a re-login prompt where you paste fresh cookies.

The cookies are stored in your Home Assistant config like any other integration's credentials. Treat backups accordingly.

## Options

| Option | Default |
|---|---|
| Alert when active bidders are at most | 2 |
| Only alert below this price (0 = any) | 0 |
| Default bid limit per auction (0 = none) | 0 |
| What one point costs | $0.13 |
| Fast check interval | 10 s |
| Idle check interval | 45 min |
| Extra auction IDs | none |

## Notes and limits

- "Active bidders" counts distinct names in the last 60 s among the last 9 bids the site exposes, so it tops out at 9.
- People with booked bids only show up once their bids actually fire.
- Respect each site's terms. Auction Watcher identifies itself in its user agent and polls modestly; it does not try to look like a person.

## Development

The site handlers and alert logic don't depend on Home Assistant:

```bash
python -m pytest tests
python tools/live_check.py path/to/cookies.txt   # read-only check of your real account
```
