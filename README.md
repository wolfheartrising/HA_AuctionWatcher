# Auction Watcher for Home Assistant

Follow the online auctions you're bidding on or watching, with a dashboard,
phone alerts and spending limits. **v0.1 supports GovAuctions**; more sites
(eBay, Mac.bid, GovDeals, ...) are planned, one handler per site.

Auction Watcher only **reads** your auctions. It never bids for you.

## What it does (GovAuctions)

- **Follows everything you're in**: your My Bids list, your watch list (favorites), plus any extra auction IDs you add.
- **One sensor per auction**: state is the price. Attributes hold the clock, leader, **active bidders in the last minute** (and who they are), whether it's closed to new bidders, your bids and what they cost, booked bids left, labels (1¢, Win-Win, ...), entry limit, Buy It Now, and **what winning right now would cost you**.
- **Price history**: past wins of the same product, with median final price, the median number of bids winners used, and the site's own cost estimate.
- **Polls only as fast as needed**: every 10 s while something you follow is live, every minute shortly before one starts, every 45 min otherwise. The slow check also keeps the login fresh, and refreshed cookies are saved automatically.
- **Alerts** as events, so you can build any automation (or use the included blueprint):
  - `auction_watcher_opportunity`: an auction is down to ≤ N active bidders (default 2), under your max price, and you're allowed to bid.
  - `auction_watcher_bid_limit_reached`: you've spent your limit on an auction.
  - `auction_watcher_auction_ended`, `auction_watcher_auction_won`
- **Bid limits**: a default limit per auction in the options, and `auction_watcher.set_bid_limit` for a single auction. At the limit you get one alert and no more "few bidders left" alerts for that auction.

## Install

1. Copy `custom_components/auction_watcher` into `/config/custom_components/` (or add this repo to HACS as a custom repository).
2. Restart Home Assistant.
3. Settings → Devices & services → **Add integration** → *Auction Watcher*.
4. Pick the site and paste your cookies (see below).
5. Optional: copy `blueprints/automation/auction_watcher/alerts.yaml` to `/config/blueprints/automation/auction_watcher/`, then create an automation from it and pick your phone.
6. Optional: create a dashboard from `dashboards/auction_watcher.yaml`. It has three tabs: Active, Watching, GovAuctions.

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
