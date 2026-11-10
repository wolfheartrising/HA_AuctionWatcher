"""Show the shape of a saved "Copy as cURL (POSIX)" request with every secret masked.

    python curl_inspect.py <curl.txt> [--secrets-out FILE --prefix NAME]

Prints method, URL (sensitive query values masked), headers (sensitive values
replaced by their length) and the body (sensitive fields masked).
With --secrets-out, appends the sensitive header values to FILE as
secrets.yaml lines named <prefix>_<header>, without printing them.
"""

import json
import re
import shlex
import sys
from urllib.parse import parse_qsl, urlsplit

SENSITIVE = re.compile(r"auth|token|cookie|csrf|xsrf|session|secret|key|sig|pass|bearer|jwt", re.I)


def mask(v: str) -> str:
    return f"<hidden, {len(v)} chars{', starts with Bearer' if v.lower().startswith('bearer ') else ''}>"


def mask_body(body: str) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        pairs = parse_qsl(body, keep_blank_values=True)
        if pairs:
            return "&".join(f"{k}={mask(v) if SENSITIVE.search(k) else v}" for k, v in pairs)
        return mask(body) if SENSITIVE.search(body) else body

    def walk(o):
        if isinstance(o, dict):
            return {k: (mask(str(v)) if SENSITIVE.search(k) and not isinstance(v, (dict, list)) else walk(v)) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(x) for x in o]
        return o
    return json.dumps(walk(data))


def main() -> None:
    args = sys.argv[1:]
    out = prefix = None
    if "--secrets-out" in args:
        i = args.index("--secrets-out"); out = args[i + 1]; del args[i:i + 2]
    if "--prefix" in args:
        i = args.index("--prefix"); prefix = args[i + 1]; del args[i:i + 2]
    text = open(args[0], encoding="utf-8-sig").read().replace("\\\r\n", " ").replace("\\\n", " ")
    tokens = shlex.split(text)
    if not tokens or tokens[0] != "curl":
        sys.exit("Doesn't look like a cURL command (POSIX). Re-copy with 'Copy as cURL (POSIX)'.")

    method, url, body, headers = None, None, None, []
    it = iter(range(1, len(tokens)))
    for i in it:
        t = tokens[i]
        if t in ("-X", "--request"):
            method = tokens[i + 1]; next(it)
        elif t in ("-H", "--header"):
            name, _, value = tokens[i + 1].partition(":"); headers.append((name.strip(), value.strip())); next(it)
        elif t in ("-b", "--cookie"):
            headers.append(("Cookie", tokens[i + 1])); next(it)
        elif t.startswith("--data") or t == "-d":
            body = tokens[i + 1]; next(it)
        elif not t.startswith("-") and url is None:
            url = t
    method = method or ("POST" if body is not None else "GET")

    u = urlsplit(url)
    q = "&".join(f"{k}={mask(v) if SENSITIVE.search(k) else v}" for k, v in parse_qsl(u.query, keep_blank_values=True))
    print(f"METHOD  {method}")
    print(f"URL     {u.scheme}://{u.netloc}{u.path}{'?' + q if q else ''}")
    print("HEADERS")
    for n, v in headers:
        print(f"  {n}: {mask(v) if SENSITIVE.search(n) else v}")
    if body is not None:
        print(f"BODY    {mask_body(body)}")

    if out and prefix:
        lines = []
        for n, v in headers:
            if SENSITIVE.search(n):
                key = f"{prefix}_{re.sub(r'[^a-z0-9]+', '_', n.lower()).strip('_')}"
                lines.append(f"{key}: '" + v.replace("'", "''") + "'")
                print(f"  -> wrote secret '{key}'")
        with open(out, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))


if __name__ == "__main__":
    main()
