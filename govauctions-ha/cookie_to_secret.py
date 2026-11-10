"""Turn a Netscape cookies.txt export into one line for Home Assistant's secrets.yaml.

Usage (run it yourself, on your own computer):
    python cookie_to_secret.py <cookies.txt> <domain> <secret_name>

Example:
    python cookie_to_secret.py govauctions_cookies.txt govauctions.com govauctions_cookie

It prints a line like:
    govauctions_cookie: 'name1=value1; name2=value2'
Copy that line into /config/secrets.yaml.

- Keeps only cookies for <domain> (and its subdomains).
- Skips cookies that have already expired.
- If the same cookie name appears twice, keeps the one for the most specific
  domain (the one a browser would send first).
"""

import sys
import time


def main() -> None:
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)

    path, domain, secret_name = sys.argv[1], sys.argv[2].lower().lstrip("."), sys.argv[3]
    now = time.time()
    chosen: dict[str, tuple[int, str]] = {}  # name -> (domain specificity, value)
    skipped_expired = 0

    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\r\n")
            # "#HttpOnly_" prefix marks HttpOnly cookies; they're still needed.
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif not line.strip() or line.startswith("#"):
                continue

            parts = line.split("\t")
            if len(parts) < 7:
                continue
            cookie_domain, _sub, _path, _secure, expires, name = parts[:6]
            value = "\t".join(parts[6:])

            d = cookie_domain.lower().lstrip(".")
            if not (d == domain or d.endswith("." + domain)):
                continue
            try:
                exp = int(float(expires))
            except ValueError:
                exp = 0
            if exp and exp < now:  # 0 = session cookie, keep it
                skipped_expired += 1
                continue

            specificity = len(d) + (0 if cookie_domain.startswith(".") else 1)
            if name not in chosen or specificity >= chosen[name][0]:
                chosen[name] = (specificity, value)

    if not chosen:
        print(f"No usable cookies for {domain} in {path}. "
              "Is the domain right, and are you logged in when exporting?", file=sys.stderr)
        sys.exit(2)

    header = "; ".join(f"{n}={v}" for n, (_s, v) in chosen.items())
    yaml_value = "'" + header.replace("'", "''") + "'"  # YAML single-quote escaping
    print(f"{secret_name}: {yaml_value}")
    print(f"# {len(chosen)} cookies kept, {skipped_expired} expired skipped", file=sys.stderr)


if __name__ == "__main__":
    main()
