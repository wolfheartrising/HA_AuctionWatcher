"""Point-pack scoring (GovAuctions). Plain Python, no Home Assistant, so it's unit-tested.

A point pack's value is exact: winning an N-point pack in a 1¢ auction costs
1¢ plus your bids, so break-even is N bids. Each pack size has its own history
of past winners; how many bids they needed (overall, and by time of day the
auction ended) says how likely a pack is to be worth fighting for.
"""

from __future__ import annotations

import re
import statistics
from datetime import datetime, tzinfo
from typing import Any

BLOCKS = ("night", "morning", "afternoon", "evening")  # 0-6, 6-12, 12-18, 18-24 local
BLOCK_LABEL = {"night": "midnight–6 AM", "morning": "6 AM–noon", "afternoon": "noon–6 PM", "evening": "6 PM–midnight"}
GREEN, YELLOW, RED = 0.35, 0.6, 1.0  # median winner bids as a share of the pack size


def parse_points(name: str) -> int | None:
    """'2500 Point Pack!' -> 2500. Only real point packs (not multipliers etc.)."""
    m = re.search(r"([\d,]+)\s*(?:point|pt)s?\s*pack", name or "", re.I)
    return int(m.group(1).replace(",", "")) if m else None


def block_of(ts: float, tz: tzinfo | None) -> str:
    return BLOCKS[datetime.fromtimestamp(ts, tz).hour // 6]


def pack_stats(previous_winners: list[dict[str, Any]], points: int, tz: tzinfo | None) -> dict[str, Any] | None:
    """Summarize past wins of one pack size."""
    rows = []
    for w in previous_winners or []:
        try:
            rows.append((float(w["timestamp"]), int(w["bids_placed"])))
        except (KeyError, TypeError, ValueError):
            continue
    if not rows:
        return None
    bids = [b for _, b in rows]
    by_block: dict[str, list[int]] = {b: [] for b in BLOCKS}
    for ts, b in rows:
        by_block[block_of(ts, tz)].append(b)
    return {
        "wins": len(bids),
        "median": int(statistics.median(bids)),
        "under_half": round(sum(b < points / 2 for b in bids) / len(bids), 2),
        "lost_money": round(sum(b > points for b in bids) / len(bids), 2),
        "blocks": {k: {"wins": len(v), "median": int(statistics.median(v))} for k, v in by_block.items() if v},
    }


MIN_WINS = 15  # fewer past wins than this: not enough history to grade


def score_pack(points: int, stats: dict[str, Any] | None, start_ts: float | None, tz: tzinfo | None) -> dict[str, Any]:
    """Grade a pack: green / yellow / red / unknown.

    What matters is when the auction ENDS, which isn't known in advance; it ends
    some time after its 5-second phase starts. So this is cautious: it takes the
    worse of the time block it starts in and the block after it (each needs at
    least 5 past wins to count), falling back to the overall median.
    """
    if not stats or not points:
        return {"grade": "unknown", "ratio": None, "expected_bids": None, "block": None}
    block = block_of(start_ts, tz) if start_ts else None
    candidates = []
    if block:
        nxt = BLOCKS[(BLOCKS.index(block) + 1) % len(BLOCKS)]
        for k in (block, nxt):
            b = stats["blocks"].get(k)
            if b and b["wins"] >= 5:
                candidates.append(b["median"])
    expected = max(candidates) if candidates else stats["median"]
    if stats["wins"] < MIN_WINS:
        return {"grade": "unknown", "ratio": round(expected / points, 2), "expected_bids": expected, "block": block,
                "cheapest_block": None}
    ratio = expected / points
    grade = "green" if ratio <= GREEN else "yellow" if ratio <= YELLOW else "red"
    return {"grade": grade, "ratio": round(ratio, 2), "expected_bids": expected, "block": block,
            "cheapest_block": min(stats["blocks"], key=lambda k: stats["blocks"][k]["median"]) if stats["blocks"] else None}


MAX_SHARE = 0.5     # never suggest spending more than half the pack: a win at the max still nets 50%+
TARGET_COVER = 0.6  # aim to cover 60% of past winning amounts


def suggest_max(points: int, previous_winners: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A max-bids suggestion for one pack, from what its past winners needed.

    max     = enough to cover ~60% of past wins, capped at half the pack size
    cover   = share of past winners who needed no more than that max
              (an upper bound on your chances: you still have to outlast the others)
    net_win = points gained if you win right at the max
    (stars come from stars_for(), using the time-of-day aware grade)
    """
    bids = sorted(int(w["bids_placed"]) for w in previous_winners or [] if str(w.get("bids_placed", "")).isdigit())
    if len(bids) < MIN_WINS or not points:
        return None
    at_target = bids[min(len(bids) - 1, int(len(bids) * TARGET_COVER))]
    cap = int(points * MAX_SHARE)
    suggested = max(10, round(min(at_target, cap), -1) or 10)
    cover = sum(b <= suggested for b in bids) / len(bids)
    return {"max": suggested, "cover": round(cover, 2), "net_win": points - suggested}


def stars_for(ratio: float | None) -> int | None:
    """How cheaply this pack usually goes at this time of day (cautious median / pack size)."""
    if ratio is None:
        return None
    return 3 if ratio <= 0.35 else 2 if ratio <= 0.45 else 1 if ratio <= 0.6 else 0


def entry_open(price: float | None, limit: float | None, closed: bool | None) -> tuple[bool, int | None]:
    """(still open to new bidders, roughly how many bids until it locks)."""
    if closed:
        return False, 0
    if price is None or not limit:
        return True, None
    left = round((limit - price) * 100)
    return left > 0, max(left, 0)
