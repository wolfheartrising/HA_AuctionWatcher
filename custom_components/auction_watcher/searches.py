"""Saved searches across sites. Plain Python (no Home Assistant), unit-tested.

A saved search is one keyword the plugin looks for on every site you pick, once a day
(default 8 AM) or on demand. Searches imported from a site remember where they came
from (and that site's own id, so deleting in the app can delete it there too); the
same keyword imported from two sites is one search with two sources.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

SITE_ORDER = ("govauctions", "macbid", "ebay", "ola")
MAX_HITS_PER_SITE = 60


def norm(keyword: str) -> str:
    return re.sub(r"\s+", " ", (keyword or "").strip().lower())


@dataclass
class SavedSearch:
    keyword: str
    sites: list[str] = field(default_factory=lambda: list(SITE_ORDER))
    max_price: float = 0.0                                  # 0 = any
    title_only: bool = True                                 # every word must be in the item's title
    sources: dict[str, str] = field(default_factory=dict)   # site -> that site's id for this saved search
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    created: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SavedSearch":
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Hit:
    site: str
    item_id: str
    title: str
    url: str
    price: float | None = None
    ends_at: str | None = None        # ISO time
    location: str | None = None
    image: str | None = None
    condition: str | None = None
    bids: int | None = None
    transfer: str | None = None       # Mac.bid: sits at this building, transferable to yours

    @property
    def key(self) -> str:
        return f"{self.site}:{self.item_id}"


def add_search(searches: list[SavedSearch], keyword: str, sites: list[str] | None = None,
               max_price: float = 0.0, source_site: str | None = None, source_id: str | None = None,
               title_only: bool = True) -> SavedSearch:
    """Add a search, or merge into the existing one with the same keyword (case/spacing-insensitive)."""
    k = norm(keyword)
    if not k:
        raise ValueError("empty keyword")
    for s in searches:
        if norm(s.keyword) == k:
            if source_site:
                s.sources[source_site] = str(source_id or "")
                if source_site not in s.sites:
                    s.sites.append(source_site)
            if sites:
                s.sites = sorted(set(s.sites) | set(sites), key=_site_rank)
            if max_price and not s.max_price:
                s.max_price = max_price
            return s
    new = SavedSearch(keyword=keyword.strip(), sites=sorted(set(sites or SITE_ORDER), key=_site_rank),
                      max_price=float(max_price or 0), title_only=title_only)
    if source_site:
        new.sources[source_site] = str(source_id or "")
    searches.append(new)
    return new


def _site_rank(site: str) -> int:
    return SITE_ORDER.index(site) if site in SITE_ORDER else 99


def title_matches(title: str, keyword: str) -> bool:
    """Every word of the keyword appears in the title (sites' own searches are fuzzy)."""
    t = norm(title)
    return all(w in t for w in norm(keyword).split())


def filter_hits(hits: list[Hit], max_price: float, keyword: str | None = None) -> list[Hit]:
    out = [h for h in hits if (not max_price or h.price is None or h.price <= max_price)
           and (not keyword or title_matches(h.title, keyword))]
    out.sort(key=lambda h: h.ends_at or "9999")
    return out[:MAX_HITS_PER_SITE]


def new_matches(results: dict[str, list[Hit]], seen: dict[str, list[str]], first_run: set[str]) -> dict[str, list[Hit]]:
    """Per search id, the hits not seen before. A search's very first run only records
    what exists (no flood of 'new' alerts for everything already listed)."""
    out: dict[str, list[Hit]] = {}
    for sid, hits in results.items():
        before = set(seen.get(sid, []))
        fresh = [h for h in hits if h.key not in before]
        if sid not in first_run and fresh:
            out[sid] = fresh
        seen[sid] = sorted(before | {h.key for h in hits})[-2000:]
    return out


def to_dict(s: SavedSearch) -> dict[str, Any]:
    return asdict(s)


def hit_dict(h: Hit, search_ids: list[str], new: bool) -> dict[str, Any]:
    d = asdict(h)
    d.update({"key": h.key, "searches": search_ids, "new": new})
    return d
