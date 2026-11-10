"""eBay parsing, on samples shaped like eBay's documented API responses.
(Not yet checked against live eBay: the developer keys were still under review.)"""

import pytest

from aw.sites import ebay as eb
from aw.sites.base import AuthError, SiteError

NOW = 1_794_300_000.0  # 2026-11-10 ~13:20 UTC

BROWSE = {
    "itemId": "v1|187654321098|0", "legacyItemId": "187654321098", "title": "Vintage Seiko Watch",
    "currentBidPrice": {"value": "41.00", "currency": "USD"}, "bidCount": 7,
    "itemEndDate": "2026-11-10T20:00:00.000Z", "buyingOptions": ["AUCTION"],
    "image": {"imageUrl": "https://i.ebayimg.com/x.jpg"}, "itemWebUrl": "https://www.ebay.com/itm/187654321098",
    "condition": "Used", "minimumPriceToBid": {"value": "42.00", "currency": "USD"},
    "shippingOptions": [{"shippingCost": {"value": "0.00", "currency": "USD"}}], "seller": {"username": "watchguy"},
}

TRADING = """<?xml version="1.0" encoding="UTF-8"?>
<GetMyeBayBuyingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <WatchList><ItemArray>
    <Item>
      <ItemID>111111111111</ItemID><Title>Camera Lens</Title>
      <ListingDetails><EndTime>2026-11-11T02:00:00.000Z</EndTime><ViewItemURL>https://www.ebay.com/itm/111111111111</ViewItemURL></ListingDetails>
      <SellingStatus><CurrentPrice currencyID="USD">85.0</CurrentPrice><BidCount>3</BidCount>
        <HighBidder><UserID>someone_else</UserID></HighBidder><ListingStatus>Active</ListingStatus></SellingStatus>
      <PictureDetails><GalleryURL>https://i.ebayimg.com/lens.jpg</GalleryURL></PictureDetails>
    </Item>
    <Item>
      <ItemID>222222222222</ItemID><Title>Drone</Title>
      <ListingDetails><EndTime>2026-11-10T15:00:00.000Z</EndTime></ListingDetails>
      <SellingStatus><CurrentPrice currencyID="USD">120.0</CurrentPrice><BidCount>9</BidCount>
        <HighBidder><UserID>bloodoggo</UserID></HighBidder><ListingStatus>Active</ListingStatus></SellingStatus>
    </Item>
  </ItemArray></WatchList>
  <BidList><ItemArray>
    <Item>
      <ItemID>222222222222</ItemID><Title>Drone</Title>
      <ListingDetails><EndTime>2026-11-10T15:00:00.000Z</EndTime></ListingDetails>
      <SellingStatus><CurrentPrice currencyID="USD">120.0</CurrentPrice><BidCount>9</BidCount>
        <HighBidder><UserID>bloodoggo</UserID></HighBidder><ListingStatus>Active</ListingStatus></SellingStatus>
      <BiddingDetails><MaxBid currencyID="USD">150.0</MaxBid></BiddingDetails>
    </Item>
  </ItemArray></BidList>
</GetMyeBayBuyingResponse>"""


def test_browse_item():
    s = eb.state_from_browse(BROWSE, False, NOW)
    assert (s.auction_id, s.status, s.price, s.extra["total_bids"]) == ("187654321098", "live", 41.0, 7)
    assert s.extra["min_next_bid"] == 42.0 and s.extra["shipping"] == 0.0 and s.extra["auction"]
    assert s.ends_at.isoformat().startswith("2026-11-10T20:00")
    assert eb.state_from_browse(BROWSE, False, NOW + 86400).status == "ended"


def test_my_ebay_watch_and_bids():
    items = eb.parse_my_ebay_buying(TRADING, "bloodoggo", NOW)
    lens, drone = items["111111111111"], items["222222222222"]
    assert lens.category == "watching" and not lens.extra["winning"] and lens.leader == "someone_else"
    # on both lists: the bid-list version wins, and you're the high bidder
    assert drone.category == "bidding" and drone.extra["winning"] and drone.leader == "you"
    assert drone.extra["your_max_bid"] == 150.0 and drone.price == 120.0


def test_my_ebay_errors():
    bad_token = ('<GetMyeBayBuyingResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Failure</Ack>'
                 '<Errors><ErrorCode>932</ErrorCode><LongMessage>Auth token is hard expired.</LongMessage></Errors>'
                 '</GetMyeBayBuyingResponse>')
    with pytest.raises(AuthError):
        eb.parse_my_ebay_buying(bad_token, "me", NOW)
    other = bad_token.replace("932", "10007")
    with pytest.raises(SiteError):
        eb.parse_my_ebay_buying(other, "me", NOW)


def test_signin_helpers():
    url = eb.consent_url("MyApp-123", "My_Name-MyApp-abc")
    assert url.startswith("https://auth.ebay.com/oauth2/authorize?client_id=MyApp-123") and "redirect_uri=My_Name-MyApp-abc" in url
    pasted = "https://auth2.ebay.com/oauth2/ThirdPartyAuthSucessFailure.isso?isAuthSuccessful=true&code=v%5E1.1%23i%5E1&expires_in=299"
    assert eb.code_from_pasted(pasted) == "v^1.1#i^1"
    assert eb.code_from_pasted("v^1.1#abc") == "v^1.1#abc"
    with pytest.raises(SiteError):
        eb.code_from_pasted("https://www.ebay.com/")


def test_item_ids_from_links():
    assert eb.parse_item_id("187654321098") == "187654321098"
    assert eb.parse_item_id("https://www.ebay.com/itm/187654321098?hash=x") == "187654321098"
    assert eb.parse_item_id("https://www.ebay.com/itm/Vintage-Seiko/187654321098") == "187654321098"
    assert eb.parse_item_id("not a link") is None


def test_api_cards_get_watchers_and_full_size_picture():
    from aw.sites import ebay as eb
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<GetMyeBayBuyingResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack>
  <WatchList><ItemArray><Item>
    <ItemID>111</ItemID><Title>Camera</Title><WatchCount>17</WatchCount>
    <ListingDetails><EndTime>2099-01-01T00:00:00.000Z</EndTime></ListingDetails>
    <SellingStatus><CurrentPrice currencyID="USD">42.00</CurrentPrice><BidCount>5</BidCount></SellingStatus>
    <PictureDetails><GalleryURL>https://i.ebayimg.com/small.jpg</GalleryURL><PictureURL>https://i.ebayimg.com/big.jpg</PictureURL></PictureDetails>
  </Item></ItemArray></WatchList></GetMyeBayBuyingResponse>"""
    st = eb.parse_my_ebay_buying(xml, "me", 0)["111"]
    assert st.extra["watchers"] == 17 and st.extra["total_bids"] == 5 and st.price == 42.0
    assert st.image == "https://i.ebayimg.com/big.jpg"


def test_cookie_html_falls_back_to_item_links():
    html = """
<html><body>
  <a href="https://www.ebay.com/itm/Fancy-Thing/123456789012">Open item</a>
</body></html>
"""
    items = eb.parse_my_ebay_html(html, NOW, "watching")
    assert "123456789012" in items
    assert items["123456789012"].title == "eBay item 123456789012"


def test_item_from_html_reads_meta_price_and_currency():
    html = """
<html><head>
  <meta property="og:title" content="Super Camera">
  <meta property="og:url" content="https://www.ebay.com/itm/Super-Camera/123456789012">
  <meta property="product:price:amount" content="149.99">
  <meta property="product:price:currency" content="USD">
</head></html>
"""
    item = eb._item_from_html("123456789012", html)
    assert item["title"] == "Super Camera"
    assert item["currentBidPrice"]["value"] == "149.99"
    assert item["currentBidPrice"]["currency"] == "USD"
