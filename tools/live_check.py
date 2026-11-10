"""Check your GovAuctions login and see what Auction Watcher would see. No Home Assistant needed.

    python tools/live_check.py <cookies.txt or file holding a Cookie header>

Read-only: it only loads your auction lists, live state and price history.
Cookie values are never printed.
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aw_loader  # noqa: E402,F401

import aiohttp  # noqa: E402

from aw.cookies import parse_cookie_input  # noqa: E402
from aw.sites import govauctions as ga  # noqa: E402


async def main(path: str) -> None:
    cookies = ga_cookies = parse_cookie_input(Path(path).read_text(encoding="utf-8"), domain=ga.COOKIE_DOMAIN)
    print(f"{len(ga_cookies)} cookies loaded for {ga.COOKIE_DOMAIN}: {', '.join(cookies)}")
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as session:
        client = ga.GovAuctionsClient(session, cookies)
        lists = await client.lists()
        print({k: len(v) for k, v in lists.items()})
        ids = sorted(set(lists["all"]) | set(lists["watching"]))
        live, credits = await client.live(ids)
        static = await client.static(ids)
        now = time.time()
        print(f"Point balance: {credits}\n")
        for aid in ids:
            s = static.get(aid, {})
            hist = await client.history(aid) if live.get(aid, {}).get("live") else None
            st = ga.build_state(aid, live.get(aid), s, hist, set(lists["watching"]), 0.13, now)
            line = (f"{st.title[:40]:40} {st.status:8} {st.category:8} "
                    f"${st.price if st.price is not None else 0:>7.2f}  active={st.active_bidders}  "
                    f"you={st.your_bids} bids (${st.your_points_cost:.2f})  booked_left={st.your_booked_left}")
            if hist:
                line += f"  | past {hist.wins} wins: median ${hist.median_final_price}, median winner bids {hist.median_winner_bids}"
            print(line)
        if client.cookies_changed:
            print("\n(The site refreshed some cookies; the integration saves those automatically.)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1]))
