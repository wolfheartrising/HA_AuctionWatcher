"""Constants for Auction Watcher."""

DOMAIN = "auction_watcher"
VERSION = "0.4.0"  # keep in sync with manifest.json (a test checks this)

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
CONF_NOTIFY_SERVICES = "notify_services"   # e.g. mobile_app_pixel_8, telegram, alexa_media_kitchen
CONF_NOTIFY_ENTITIES = "notify_entities"   # e.g. notify.kitchen_echo_announce, Telegram chat entities
CONF_ALERT_KINDS = "alert_kinds"           # which alerts to send: opportunity, bid_limit, ended, won
CONF_ACTIVE_WINDOW = "active_window_minutes"  # count bidders seen within this many minutes
CONF_STABLE_SECONDS = "stable_seconds"        # must stay at <= N bidders this long before alerting
DEFAULT_ACTIVE_WINDOW = 3
DEFAULT_STABLE_SECONDS = 0
DEFAULT_ALERT_KINDS = ["opportunity", "bid_limit", "ended", "won", "booking"]

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
EVENT_BOOKING = f"{DOMAIN}_booking"  # data.action: booked / cancelled / booking_failed

SERVICE_BOOK = "book_bids"
SERVICE_CANCEL = "cancel_booked_bids"
SERVICE_SET_AUTO = "set_auto_booking"

SERVICE_SET_BID_LIMIT = "set_bid_limit"
SERVICE_REFRESH = "refresh"

STORAGE_VERSION = 1
