# Auction Watcher for Home Assistant

Follow the online auctions you're bidding on or watching, with a dashboard,
phone alerts and spending limits. **Supports GovAuctions**; more sites
(eBay, Mac.bid, GovDeals, ...) are planned, one handler per site.

Auction Watcher reads your auctions. It only **books or cancels bids** when you
press a button, call a service, or turn on auto-booking for a specific auction,
and never past the bid limit you set for that auction.

## What it does (GovAuctions)

- **Its own "Auctions" page in the sidebar**: Active / Watching / Ended / per-site tabs, a card per auction with picture, price, clock, bidders, your bids and cost, past winners, and a bid limit you can set right there. Long countdowns tick every second; the short penny-auction clock shows its value "as of" the last check, since every bid resets it.
- **Follows everything you're in**: your My Bids list, your watch list (favorites), plus any extra auction IDs you add.
- **One sensor per auction**: state is the price. Attributes hold the clock, leader, **active bidders** (everyone seen bidding in the last 3 minutes, configurable, so people who pause and jump back in still count), whether it's closed to new bidders, your bids and what they cost, booked bids left, labels (1¢, Win-Win, ...), entry limit, Buy It Now, and **what winning right now would cost you**.
- **Price history**: past wins of the same product, with median final price, the median number of bids winners used, and the site's own cost estimate.
- **Polls only as fast as needed**: every 10 s while something you follow is live, every minute shortly before one starts, every 45 min otherwise. The slow check also keeps the login fresh, and refreshed cookies are saved automatically.
- **Alerts**, each sent once (not every poll):
  - **Few bidders left**: down to ≤ N active bidders (default 2), in the real 5-second phase (not during the long first countdown), under your max price, and you're allowed to bid. Optionally only after it has stayed that low for N seconds (default 0: right away, since 2 bidders can end an auction in seconds).
  - **Bid limit reached**: you've spent your limit on an auction.
  - **Auction ended** and **auction won**.
- **Built-in delivery**, picked in ⚙️:
  - **Phone push** (`notify.mobile_app_…`): tapping it opens the auction.
  - **Telegram**: its notify service or notify entities, with the auction link in the message.
  - **Alexa**: Alexa Media Player services are sent as announcements; for the official Alexa Devices integration pick its *Announce* or *Speak* entity. Alexa gets a short spoken version.
  - Anything else Home Assistant can notify (email, the bell-icon notification, ...).
- **Events** too (`auction_watcher_opportunity`, `…_bid_limit_reached`, `…_auction_ended`, `…_auction_won`), so you can build your own automations, like flashing a light. The old blueprint in `blueprints/` still works but isn't needed.
- **Bid limits**: a default limit per auction in the options, and `auction_watcher.set_bid_limit` for a single auction. At the limit you get one alert and no more "few bidders left" alerts for that auction.

## Booking and auto-booking (GovAuctions)

Each auction card on the **Auctions** page has a *Booking & auto* section. These spend real points.

- **Book bids / Cancel booked**: the same calls as the site's own buttons (`auction_watcher.book_bids`, `auction_watcher.cancel_booked_bids`). Manual booking is clamped to the auction's bid limit and your balance. The site places the first booked bid right away, so booking 1 is the same as bidding once; cancelling returns only the bids not yet placed.
- **Max price**: handed to the site's own price cap, so your booked bids stop above it.
- **Auto-cancel when crowded** (off by default): when the auction has more bidders than your alert number, your booked bids are cancelled and the unused points come back.
- **Auto-rebook when it thins out** (off by default): when it's back down to your alert number and you have nothing booked, it books again: either a **fixed amount** or **what was left when it auto-cancelled**.

Safety rules, always on:
- Auto-booking can't be turned on for an auction without a **bid limit**, and never books past it or past your balance.
- It only acts in the real 5-second phase, never during the first long countdown.
- It **never auto-cancels where that would lock you out**: *No Re-Entry* auctions, or auctions closed to new bidders that you haven't bid in.
- At most one action per auction every 30 seconds; every booking, cancel or failure sends an alert (*Bids booked or cancelled* in the alert settings).

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
