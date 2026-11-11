from aw.searches import Hit, add_search, filter_hits, new_matches


def test_add_and_merge_duplicates_across_sites():
    s = []
    a = add_search(s, "Greenhouse", source_site="macbid", source_id="960530")
    b = add_search(s, "  greenhouse ", source_site="ebay", source_id="77")
    assert a is b and len(s) == 1
    assert a.sources == {"macbid": "960530", "ebay": "77"}
    add_search(s, "shed", sites=["macbid"])
    assert [x.keyword for x in s] == ["Greenhouse", "shed"] and s[1].sites == ["macbid"]


def test_price_filter_and_order():
    hits = [Hit("macbid", "1", "a", "u", price=50, ends_at="2026-11-12"), Hit("macbid", "2", "b", "u", price=5, ends_at="2026-11-11"),
            Hit("govauctions", "3", "c", "u", price=None)]
    assert [h.item_id for h in filter_hits(hits, 20)] == ["2", "3"]


def test_new_matches_skip_first_run_then_report_only_new():
    seen = {}
    r1 = {"s1": [Hit("macbid", "1", "a", "u"), Hit("macbid", "2", "b", "u")]}
    assert new_matches(r1, seen, first_run={"s1"}) == {}            # first run: just remember
    r2 = {"s1": [Hit("macbid", "2", "b", "u"), Hit("govauctions", "9", "c", "u")]}
    fresh = new_matches(r2, seen, first_run=set())
    assert [h.key for h in fresh["s1"]] == ["govauctions:9"]
    assert new_matches(r2, seen, first_run=set()) == {}               # nothing new the next time


from aw.alerts import format_alert  # noqa: E402
from aw.sites import macbid as mb  # noqa: E402


def test_macbid_keywords_live_on_the_profile_as_one_list():
    kws = mb.keywords_from_profile({"keywords": "Greenhouse,shed, Generator,,bluey"})
    assert kws == ["Greenhouse", "shed", "Generator", "bluey"]
    assert mb.keywords_without(kws, " BLUEY ") == ["Greenhouse", "shed", "Generator"]
    assert mb.keywords_with(kws, "shed") == kws
    assert mb.keywords_with(kws, "drone")[-1] == "drone"
    assert mb.keywords_from_profile({"user": {"keywords": None}}) == []


def test_macbid_search_marks_transfer_items():
    data = {"results": [{"found": 2, "hits": [
        {"document": {"lot_id": 1, "product_name": "Bluey ship", "auction_id": 5, "lot_number": "1A",
                      "current_bid": 3, "auction_location": "Akron", "expected_closing_utc": 1794318282}},
        {"document": {"lot_id": 2, "product_name": "Bluey car", "auction_id": 6, "lot_number": "2B",
                      "auction_location": "Canton", "transfer_locations": ["Akron"]}},
    ]}]}
    hits = mb.search_hits(data, ["Akron"])
    assert [h["transfer"] for h in hits] == [None, "Canton"]
    assert hits[0]["url"] == "https://www.mac.bid/?aid=5&lid=1A" and hits[0]["ends_at"].startswith("2026-")
    assert Hit(site="macbid", **hits[1]).key == "macbid:2"
    assert mb.search_total(data) == 2


def test_search_alert_wording():
    title, msg, spoken = format_alert("search", {"count": 5, "lines": ["bluey - 2 new", "drone - 3 new"],
                                                 "quiet": ["shed", "generator", "greenhouse"]})
    assert title == "You have new matches for your saved searches"
    assert msg == "bluey - 2 new\ndrone - 3 new\n\nshed, generator, greenhouse - no new results"
    assert "5 new auction matches" in spoken
    assert format_alert("search", {"count": 1, "lines": ["bluey - 1 new"]})[1] == "bluey - 1 new"



def test_title_only_drops_the_sites_loose_matches():
    from aw.searches import title_matches
    assert title_matches("Jackery Solar Generator 1500", "generator")
    assert title_matches("Rubbermaid Storage SHED, 7x7", "storage shed")
    assert not title_matches("DJI Air 3S Drone Fly More Combo", "generator")
    assert not title_matches("Honses XD-3420 DC Motor 12V", "generator")
    hits = [Hit("govauctions", "1", "DJI Air 3S Drone", "u"), Hit("macbid", "2", "Champion Generator 3500W", "u")]
    assert [h.item_id for h in filter_hits(hits, 0, "generator")] == ["2"]
    assert len(filter_hits(hits, 0, None)) == 2


def test_same_keyword_from_ola_and_macbid_is_one_search():
    from aw.searches import add_search
    s = []
    a = add_search(s, "wyze", sites=["macbid"], source_site="macbid", source_id="wyze")
    b = add_search(s, " Wyze ", source_site="ola", source_id="731363")   # OLA import, case/space-insensitive
    assert a is b and len(s) == 1                      # merged, not duplicated
    assert a.sources == {"macbid": "wyze", "ola": "731363"}
    assert "macbid" in a.sites and "ola" in a.sites    # now searched on both
