"""Turn whatever the user pastes into a cookie dict.

Accepts any of:
  * a Netscape cookies.txt export (tab-separated, what browser extensions save)
  * a raw "Cookie:" header value copied from the browser's Network tab
  * the same with the leading "Cookie:" still attached
"""

from __future__ import annotations

import time


def parse_cookie_input(text: str, domain: str | None = None, now: float | None = None) -> dict[str, str]:
    """Return {name: value}. Skips expired and other-domain cookies (Netscape format only)."""
    text = (text or "").strip()
    if not text:
        return {}
    if "\t" in text:
        return _parse_netscape(text, domain, time.time() if now is None else now)
    if text.lower().startswith("cookie:"):
        text = text[len("cookie:"):]
    cookies: dict[str, str] = {}
    for part in text.replace("\n", ";").split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            cookies[name.strip()] = value.strip()
    return cookies


def _parse_netscape(text: str, domain: str | None, now: float) -> dict[str, str]:
    want = domain.lower().lstrip(".") if domain else None
    chosen: dict[str, tuple[int, str]] = {}
    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        cookie_domain, _sub, _path, _secure, expires, name = parts[:6]
        value = "\t".join(parts[6:])
        dom = cookie_domain.lower().lstrip(".")
        if want and not (dom == want or dom.endswith("." + want) or want.endswith("." + dom)):
            continue
        try:
            exp = int(float(expires))
        except ValueError:
            exp = 0
        if exp and exp < now:
            continue
        # Most specific domain wins, like a browser
        specificity = len(dom) + (0 if cookie_domain.startswith(".") else 1)
        if name not in chosen or specificity >= chosen[name][0]:
            chosen[name] = (specificity, value)
    return {name: value for name, (_s, value) in chosen.items()}


def cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())
