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


def winner_prices(previous_winners: list[dict[str, Any]], *, price_key: str = "price") -> list[float]:
    """Extract winning prices from raw winner records."""
    out: list[float] = []
    for w in previous_winners or []:
        try:
            p = float(w.get(price_key, 0) or 0)
        except (TypeError, ValueError):
            continue
        if p > 0:
            out.append(p)
    return out


def collapse_rate(previous_winners: list[dict[str, Any]], threshold: float = 150.0) -> float | None:
    """Share of past wins that closed cheap enough to be worth a risk."""
    prices = winner_prices(previous_winners)
    if not prices:
        return None
    return round(sum(p <= threshold for p in prices) / len(prices), 2)


def gov_phase(clock_seconds: int | float | None, *, active_bidders: int | None = None,
             non_leader_count: int | None = None) -> str | None:
    """Classify a live penny-auction fight: claiming -> mid-war -> thinning -> end-game."""
    if clock_seconds is None:
        return None
    sec = float(clock_seconds)
    if sec > 60:
        return "claiming"
    if sec > 20:
        return "mid-war"
    if sec > 8:
        return "thinning"
    return "end-game"


def war_collapse(recent_bidders: list[str] | None, *, leader: str | None = None,
                active_bidders: int | None = None,
                clock_seconds: int | float | None = None) -> dict[str, Any] | None:
    """Detect when the field has narrowed to a near-1 horse race."""
    if not recent_bidders:
        return None
    names = [str(b).strip() for b in recent_bidders if str(b).strip()]
    if leader:
        names = [b for b in names if b != str(leader).strip()]
    unique = list(dict.fromkeys(names))
    count = len(unique)
    phase = gov_phase(clock_seconds, active_bidders=active_bidders, non_leader_count=count)
    collapse = count <= 1
    return {
        "phase": phase,
        "non_leader_count": count,
        "collapse": collapse,
        "detail": (
            "round-robin collapse: only one non-leader is still firing"
            if collapse else
            "steady field" if count > 1 else "leader-only"
        ),
    }


def chase_ability(
    previous_winners: list[dict[str, Any]], *,
    your_cap: float | None = None,
    blocked_count: int = 0,
    veteran_count: int = 0,
    cheap_threshold: float = 150.0,
    your_bids: int | None = None,
    median_winner_bids: float | None = None,
    cap_label: str = "your cap",
) -> dict[str, Any] | None:
    """Phase-1 decision support: is this auction worth chasing?

    The main signal is whether the auction's historical finish is realistically
    within your remaining budget. A cheap close matters, but the far stronger
    warning is "the reference cap is far below the normal winning cost" and the
    room is full of seasoned bidders who tend to outlast you.
    """
    prices = winner_prices(previous_winners)
    if not prices:
        return None
    median = statistics.median(prices)
    cheap = collapse_rate(previous_winners, cheap_threshold)
    your_cap_ratio = None if your_cap in (None, 0) else round(median / your_cap, 2) if your_cap > 0 else None

    verdict = "green"
    label = "Chase"

    if your_cap not in (None, 0):
        if median > your_cap * 1.2:
            verdict = "red"
            label = "Skip"
        elif median > your_cap * 0.95:
            verdict = "yellow"
            label = "Borderline"

    if median_winner_bids is not None and your_bids is not None:
        if median_winner_bids > your_bids * 3:
            if verdict == "green":
                verdict = "yellow"
                label = "Borderline"
        if median_winner_bids > your_bids * 6:
            verdict = "red"
            label = "Skip"

    if cheap is not None and cheap <= 0.1:
        verdict = "red" if verdict != "green" else "yellow"
        label = "Skip" if verdict == "red" else "Borderline"
    elif cheap is not None and cheap <= 0.35 and verdict == "green":
        verdict = "yellow"
        label = "Borderline"

    if blocked_count and not veteran_count and verdict == "green":
        verdict = "yellow"
        label = "Borderline"
    if veteran_count and blocked_count == 0 and cheap is not None and cheap <= 0.15:
        verdict = "yellow"
        label = "Borderline"

    summary_bits = []
    if your_cap_ratio is not None:
        summary_bits.append(f"{your_cap_ratio}× {cap_label}")
    summary_bits.append(f"median ${median:.2f}")
    if cheap is not None:
        summary_bits.append(f"{cheap * 100:.0f}% close cheap")
    if blocked_count:
        summary_bits.append(f"{blocked_count} blocked whales")
    if veteran_count:
        summary_bits.append(f"{veteran_count} veterans")
    summary = ". ".join(summary_bits)
    if verdict == "red":
        summary = f"Money pit: {summary}."
    elif verdict == "yellow":
        summary = f"Borderline: {summary}."
    else:
        summary = f"Good value: {summary}."
    return {
        "verdict": verdict,
        "label": label,
        "summary": summary,
        "median_final_price": round(median, 2),
        "collapse_rate": cheap,
        "your_cap_vs_median": your_cap_ratio,
        "blocked_count": blocked_count,
        "veteran_count": veteran_count,
    }


def suggested_cap(previous_winners: list[dict[str, Any]], *, weekly_budget: float | None = None,
                 target_share: float = 0.7, max_cap: float | None = None,
                 cheap_threshold: float = 150.0) -> dict[str, Any] | None:
    """Budget-aware cap for a product using the past winners already loaded."""
    prices = sorted(winner_prices(previous_winners))
    if not prices:
        return None
    idx = min(len(prices) - 1, max(0, int(len(prices) * target_share)))
    cap = prices[idx]
    if max_cap is not None:
        cap = min(cap, max_cap)
    cap = max(5.0, round(cap, -1) if cap > 10 else round(cap, 2))
    cover = sum(p <= cap for p in prices) / len(prices)
    if weekly_budget is None:
        fights = None
    else:
        fights = 0 if cap <= 0 else max(0, int(weekly_budget // cap))
    chase = (weekly_budget is None) or (cap <= weekly_budget)
    return {
        "cap": round(cap, 2),
        "cover": round(cover, 2),
        "fights": fights,
        "chase": chase,
        "cheap_rate": collapse_rate(previous_winners, cheap_threshold),
    }


def decision_summary(previous_winners: list[dict[str, Any]], *, your_cap: float | None = None,
                    blocked_count: int = 0, veteran_count: int = 0,
                    weekly_budget: float | None = None,
                    cheap_threshold: float = 150.0,
                    your_bids: int | None = None,
                    median_winner_bids: float | None = None,
                    cap_label: str = "your cap") -> dict[str, Any] | None:
    """Bundle the phase-1 chase verdict and budget cap into one object."""
    proposal = chase_ability(previous_winners,
                            your_cap=your_cap,
                            blocked_count=blocked_count,
                            veteran_count=veteran_count,
                            cheap_threshold=cheap_threshold,
                            your_bids=your_bids,
                            median_winner_bids=median_winner_bids,
                            cap_label=cap_label)
    cap = suggested_cap(previous_winners, weekly_budget=weekly_budget, cheap_threshold=cheap_threshold)
    if proposal is None and cap is None:
        return None
    return {
        **(proposal or {}),
        "suggested_cap": cap["cap"] if cap else None,
        "suggested_cap_cover": cap["cover"] if cap else None,
        "suggested_cap_chase": cap["chase"] if cap else None,
        "suggested_cap_fights": cap["fights"] if cap else None,
        "budget_cap_note": (
            f"Suggested cap ${cap['cap']:.2f}; {cap['cover'] * 100:.0f}% of wins are at or below it"
            if cap else None
        ),
    }


def best_bet_score(previous_winners: list[dict[str, Any]], *, current_price: float | None = None,
                  your_cap: float | None = None, active_bidders: int | None = None,
                  recent_bidders: list[str] | None = None) -> dict[str, Any] | None:
    """Phase-1 ranking: the auction is a strong bet when it is cheap by history and the field is thinned."""
    prices = winner_prices(previous_winners)
    if not prices:
        return None
    median = statistics.median(prices)
    cheap = collapse_rate(previous_winners)
    score = 50.0
    if your_cap and your_cap > 0:
        score -= max(0.0, min(35.0, (median - your_cap) / max(1.0, your_cap) * 35.0))
    if cheap is not None:
        score += cheap * 30.0
    if active_bidders is not None:
        score += max(0.0, min(20.0, (6 - active_bidders) * 4.0))
    if current_price is not None:
        score += max(0.0, min(15.0, (median - current_price) / max(1.0, median) * 15.0))
    if recent_bidders:
        unique = list(dict.fromkeys(str(b).strip() for b in recent_bidders if str(b).strip()))
        score += max(0.0, min(10.0, (4 - len(unique)) * 3.5))
    score = max(0.0, min(100.0, score))
    if score >= 75:
        label = "Best bet"
    elif score >= 60:
        label = "Strong"
    elif score >= 42:
        label = "Okay"
    else:
        label = "Skip"
    return {
        "score": round(score, 1),
        "label": label,
        "median_final_price": round(median, 2),
        "collapse_rate": cheap,
        "price_gap": round((median - (current_price or median)), 2) if current_price is not None else None,
    }


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
