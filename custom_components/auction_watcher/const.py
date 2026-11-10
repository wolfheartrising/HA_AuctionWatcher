"""Constants for Auction Watcher."""

DOMAIN = "auction_watcher"
VERSION = "1.3.3"  # keep in sync with manifest.json (a test checks this)

CONF_SITE = "site"
CONF_COOKIE = "cookie"

SITE_GOVAUCTIONS = "govauctions"
SITES = {SITE_GOVAUCTIONS: "GovAuctions", "macbid": "Mac.bid", "ebay": "eBay"}

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
DEFAULT_ALERT_KINDS = ["opportunity", "bid_limit", "ended", "won", "booking",
                       "outbid", "ending_soon", "over_max", "pickup", "pack", "budget", "search", "proxy"]

DEFAULT_FAST_INTERVAL = 10       # seconds, while something you follow is live
DEFAULT_IDLE_INTERVAL = 45       # minutes, when nothing is live (keeps the login fresh)
UPCOMING_INTERVAL = 60           # seconds, when something starts within UPCOMING_WINDOW
UPCOMING_WINDOW = 30 * 60
LISTS_REFRESH_SECONDS = 60       # how often to re-read "my auctions" while in fast mode
HISTORY_REFRESH_SECONDS = 6 * 3600
HISTORY_FETCHES_PER_UPDATE = 2   # be gentle: fetch price history a little at a time
DEFAULT_ALERT_MAX_BIDDERS = 1  # OTHER bidders (you're not counted): 1 = you against one person
DEFAULT_ALERT_MAX_PRICE = 0.0    # 0 = no price limit
DEFAULT_BID_LIMIT = 0            # 0 = no per-auction limit
DEFAULT_POINT_COST = 0.13        # GovAuctions points are 13 cents

# Events (use these in automations, or the included blueprint)
EVENT_OPPORTUNITY = f"{DOMAIN}_opportunity"
EVENT_BID_LIMIT = f"{DOMAIN}_bid_limit_reached"
EVENT_ENDED = f"{DOMAIN}_auction_ended"
EVENT_WON = f"{DOMAIN}_auction_won"
EVENT_BOOKING = f"{DOMAIN}_booking"  # data.action: booked / cancelled / booking_failed
# Timed auctions (Mac.bid)
EVENT_OUTBID = f"{DOMAIN}_outbid"
EVENT_ENDING_SOON = f"{DOMAIN}_ending_soon"
EVENT_OVER_MAX = f"{DOMAIN}_over_max"
EVENT_PICKUP = f"{DOMAIN}_pickup_reminder"
EVENT_PACK = f"{DOMAIN}_good_point_pack"
EVENT_SEARCH = f"{DOMAIN}_search_matches"
EVENT_SEARCHES_UPDATED = f"{DOMAIN}_searches_updated"  # tells the open Auctions page to fetch (no polling)
EVENT_BUDGET = f"{DOMAIN}_budget_reached"
CONF_PACKS_ENABLED = "scan_point_packs"
CONF_WEEKLY_BUDGET = "weekly_budget_points"  # 0 = no budget
CONF_MACBID_BUDGET = "macbid_budget_dollars"  # most dollars tied up in Mac.bid max bids at once (0 = none)
SERVICE_PLACE_BID = "place_bid"
SERVICE_SET_MACBID = "set_macbid_bidding"
PACKS_SCAN_SECONDS = 3600   # full Packs list + history, hourly
PACKS_LIVE_SECONDS = 600    # live price / entry state of known packs

SITE_MACBID = "macbid"
SITE_EBAY = "ebay"
CONF_APP_ID = "app_id"
CONF_CERT_ID = "cert_id"
CONF_RUNAME = "runame"
CONF_EBAY_TOKENS = "ebay_tokens"
CONF_EBAY_USER = "ebay_user_id"
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_AUTH = "auth"            # Mac.bid tokens (renewed automatically)
CONF_DEVICE_ID = "device_id"
CONF_ENDING_MINUTES = "ending_soon_minutes"
CONF_PICKUP_HOURS = "pickup_reminder_hours"
DEFAULT_ENDING_MINUTES = 10
DEFAULT_PICKUP_HOURS = 24
PICKUP_REFRESH_SECONDS = 6 * 3600
WATCHLIST_REFRESH_SECONDS = 120
CONF_KEEP_ENDED_DAYS = "keep_ended_days"
DEFAULT_KEEP_ENDED_DAYS = 3

SERVICE_BOOK = "book_bids"
SERVICE_CANCEL = "cancel_booked_bids"
SERVICE_SET_AUTO = "set_auto_booking"

SERVICE_SET_BID_LIMIT = "set_bid_limit"
SERVICE_REFRESH = "refresh"

STORAGE_VERSION = 1
