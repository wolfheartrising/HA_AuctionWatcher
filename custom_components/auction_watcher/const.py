"""Constants for Auction Watcher."""

DOMAIN = "auction_watcher"

CONF_SITE = "site"
CONF_COOKIE = "cookie"

SITE_GOVAUCTIONS = "govauctions"
SITES = {SITE_GOVAUCTIONS: "GovAuctions"}

# Options
CONF_FAST_INTERVAL = "fast_interval_seconds"
CONF_IDLE_INTERVAL = "idle_interval_minutes"
CONF_ALERT_MAX_BIDDERS = "alert_max_bidders"
CONF_ALERT_MAX_PRICE = "alert_max_price"
CONF_DEFAULT_BID_LIMIT = "default_bid_limit"
CONF_POINT_COST = "point_cost"
CONF_EXTRA_IDS = "extra_auction_ids"

DEFAULT_FAST_INTERVAL = 10       # seconds, while something you follow is live
DEFAULT_IDLE_INTERVAL = 45       # minutes, when nothing is live (keeps the login fresh)
UPCOMING_INTERVAL = 60           # seconds, when something starts within UPCOMING_WINDOW
UPCOMING_WINDOW = 30 * 60
LISTS_REFRESH_SECONDS = 60       # how often to re-read "my auctions" while in fast mode
HISTORY_REFRESH_SECONDS = 6 * 3600
HISTORY_FETCHES_PER_UPDATE = 2   # be gentle: fetch price history a little at a time
DEFAULT_ALERT_MAX_BIDDERS = 2
DEFAULT_ALERT_MAX_PRICE = 0.0    # 0 = no price limit
DEFAULT_BID_LIMIT = 0            # 0 = no per-auction limit
DEFAULT_POINT_COST = 0.13        # GovAuctions points are 13 cents

# Events (use these in automations, or the included blueprint)
EVENT_OPPORTUNITY = f"{DOMAIN}_opportunity"
EVENT_BID_LIMIT = f"{DOMAIN}_bid_limit_reached"
EVENT_ENDED = f"{DOMAIN}_auction_ended"
EVENT_WON = f"{DOMAIN}_auction_won"

SERVICE_SET_BID_LIMIT = "set_bid_limit"
SERVICE_REFRESH = "refresh"

STORAGE_VERSION = 1
