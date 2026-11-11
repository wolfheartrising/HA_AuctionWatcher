"""Read-only OLA API client based on app-discovered endpoints.

Examples:
    python tools/ola_reader.py summary --email you@example.com --password "..."
    python tools/ola_reader.py summary --access-token "<jwt>"
    python tools/ola_reader.py endpoint /auction-items/my/winning --access-token "<jwt>"

Notes:
- This is a reader/inspector tool. It does not place bids or change account data.
- Authentication flow and host names are inferred from the mobile app bundle.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from typing import Any

import aiohttp

BACKEND_HOST = "https://auction-api.olabid.com/api/v2"
# OLA's app API key — required on every request or the API answers 401 (override with --api-key).
API_KEY = "1BAF2123-00D7-421E-B999-FB0B222D6577"


class OLAError(RuntimeError):
    """Raised when OLA API calls fail."""


@dataclass(slots=True)
class AuthBundle:
    """Normalized auth payload observed in app bytecode."""

    email: str | None
    user_id: str | int | None
    access_token: str
    token_type: str | None
    access_token_expires_in: int | None
    refresh_token: str | None
    refresh_token_expires_in: int | None

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "AuthBundle":
        return cls(
            email=payload.get("email"),
            user_id=payload.get("userId"),
            access_token=str(payload.get("accessToken") or ""),
            token_type=payload.get("tokenType"),
            access_token_expires_in=_as_int(payload.get("accessTokenExpiresIn")),
            refresh_token=payload.get("refreshToken"),
            refresh_token_expires_in=_as_int(payload.get("refreshTokenExpiresIn")),
        )


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class OLAClient:
    """Minimal OLA API client for read-only account/auction endpoints."""

    def __init__(self, session: aiohttp.ClientSession, access_token: str | None = None,
                 api_key: str | None = None) -> None:
        self._session = session
        self._access_token = access_token
        self._api_key = (api_key or "").strip() or API_KEY

    @property
    def access_token(self) -> str | None:
        return self._access_token

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "X-Api-Key": self._api_key}
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        if not path.startswith("/"):
            raise OLAError(f"Path must start with '/': {path}")
        url = f"{BACKEND_HOST}{path}"
        async with self._session.request(
            method,
            url,
            headers=self._headers(),
            params=params,
            json=json_body,
        ) as resp:
            text = await resp.text()
            try:
                body = json.loads(text) if text else None
            except json.JSONDecodeError:
                body = text
            if resp.status >= 400:
                raise OLAError(f"{method} {path} failed ({resp.status}): {body}")
            return body

    async def sign_in_email(self, email: str, password: str) -> AuthBundle:
        data = await self._request("POST", "/account/sign-in/email", json_body={"email": email, "password": password})
        payload = data.get("data") if isinstance(data, dict) and "data" in data else data
        if not isinstance(payload, dict):
            raise OLAError(f"Unexpected sign-in response shape: {type(payload).__name__}")
        auth = AuthBundle.from_api(payload)
        if not auth.access_token:
            raise OLAError(f"Sign-in succeeded but no accessToken was returned: {payload}")
        self._access_token = auth.access_token
        return auth

    async def submit_info(self, email: str) -> Any:
        return await self._request("POST", "/account/submit-info", json_body={"email": email})

    async def submit_passcode(self, passcode: str, confirmation_token: str) -> AuthBundle:
        data = await self._request(
            "POST",
            "/account/submit-passcode",
            json_body={"passcode": passcode, "confirmationToken": confirmation_token},
        )
        payload = data.get("data") if isinstance(data, dict) and "data" in data else data
        if not isinstance(payload, dict):
            raise OLAError(f"Unexpected submit-passcode response shape: {type(payload).__name__}")
        auth = AuthBundle.from_api(payload)
        if not auth.access_token:
            raise OLAError(f"Submit-passcode succeeded but no accessToken was returned: {payload}")
        self._access_token = auth.access_token
        return auth

    async def refresh(self, refresh_token: str) -> AuthBundle:
        data = await self._request("POST", "/account/refresh", json_body={"refreshToken": refresh_token})
        payload = data.get("data") if isinstance(data, dict) and "data" in data else data
        if not isinstance(payload, dict):
            raise OLAError(f"Unexpected refresh response shape: {type(payload).__name__}")
        auth = AuthBundle.from_api(payload)
        if not auth.access_token:
            raise OLAError(f"Refresh succeeded but no accessToken was returned: {payload}")
        self._access_token = auth.access_token
        return auth

    async def account_current(self) -> Any:
        return await self._request("GET", "/account/current")

    async def my_winning(self) -> Any:
        return await self._request("GET", "/auction-items/my/winning")

    async def my_watchlist(self) -> Any:
        return await self._request("GET", "/auction-items/my/watchlist")

    async def my_bids_history(self) -> Any:
        return await self._request("GET", "/auction-items/my/bids/history")

    async def endpoint(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", path, params=params)


def _parse_params(raw: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in raw:
        if "=" not in part:
            raise OLAError(f"Invalid --param '{part}'. Use key=value.")
        key, value = part.split("=", 1)
        key = key.strip()
        if not key:
            raise OLAError(f"Invalid --param '{part}': key is empty.")
        out[key] = value
    return out


def _mask_token(token: str | None) -> str:
    if not token:
        return "(none)"
    if len(token) <= 12:
        return "*" * len(token)
    return f"{token[:6]}...{token[-6:]}"


def _extract_items_count(payload: Any) -> int | None:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict):
            for key in ("items", "auctionItems", "results"):
                value = data.get(key)
                if isinstance(value, list):
                    return len(value)
    return None


async def _run(args: argparse.Namespace) -> None:
    timeout = aiohttp.ClientTimeout(total=args.timeout)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = OLAClient(session, access_token=args.access_token, api_key=args.api_key)

        if args.email and args.password:
            auth = await client.sign_in_email(args.email, args.password)
            print("Signed in.")
            print(f"  user: {auth.user_id!r}  email: {auth.email!r}")
            print(f"  access token: {_mask_token(auth.access_token)}")
            if args.show_tokens:
                print(
                    json.dumps(
                        {
                            "accessToken": auth.access_token,
                            "refreshToken": auth.refresh_token,
                            "tokenType": auth.token_type,
                            "accessTokenExpiresIn": auth.access_token_expires_in,
                            "refreshTokenExpiresIn": auth.refresh_token_expires_in,
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
        elif args.email and args.passcode and args.confirmation_token:
            auth = await client.submit_passcode(args.passcode, args.confirmation_token)
            print("Passcode accepted.")
            print(f"  user: {auth.user_id!r}  email: {auth.email!r}")
            print(f"  access token: {_mask_token(auth.access_token)}")
            if args.show_tokens:
                print(
                    json.dumps(
                        {
                            "accessToken": auth.access_token,
                            "refreshToken": auth.refresh_token,
                            "tokenType": auth.token_type,
                            "accessTokenExpiresIn": auth.access_token_expires_in,
                            "refreshTokenExpiresIn": auth.refresh_token_expires_in,
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
        elif args.email and args.command == "request-passcode":
            result = await client.submit_info(args.email)
            print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
            return
        elif not client.access_token:
            raise OLAError(
                "Provide --access-token, or --email/--password, "
                "or request passcode with 'request-passcode', then use --passcode + --confirmation-token."
            )

        if args.command == "summary":
            current, winning, watchlist, bids = await asyncio.gather(
                client.account_current(),
                client.my_winning(),
                client.my_watchlist(),
                client.my_bids_history(),
            )
            print("\nSummary:")
            print(f"  account/current: {'ok' if current is not None else 'empty'}")
            print(f"  my/winning count: {_extract_items_count(winning)}")
            print(f"  my/watchlist count: {_extract_items_count(watchlist)}")
            print(f"  my/bids/history count: {_extract_items_count(bids)}")
            if args.dump_json:
                print("\naccount/current JSON:")
                print(json.dumps(current, indent=2, sort_keys=True, ensure_ascii=False))
        elif args.command == "endpoint":
            params = _parse_params(args.param or [])
            result = await client.endpoint(args.path, params=params or None)
            print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
        else:
            raise OLAError(f"Unknown command: {args.command}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only OLA API client")
    parser.add_argument("--email", help="Sign in using /account/sign-in/email")
    parser.add_argument("--password", help="Password for --email")
    parser.add_argument("--access-token", help="Existing access token (Bearer)")
    parser.add_argument("--api-key", help="Override OLA's X-Api-Key (defaults to the built-in app key)")
    parser.add_argument("--show-tokens", action="store_true", help="Print raw tokens after login")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout seconds")
    parser.add_argument("--passcode", help="Passcode for /account/submit-passcode flow")
    parser.add_argument("--confirmation-token", help="Confirmation token from /account/submit-info response")

    sub = parser.add_subparsers(dest="command", required=True)

    p_summary = sub.add_parser("summary", help="Fetch current account + key list endpoints")
    p_summary.add_argument("--dump-json", action="store_true", help="Print /account/current JSON")

    p_endpoint = sub.add_parser("endpoint", help="Call any GET endpoint path")
    p_endpoint.add_argument("path", help="API path, e.g. /auction-items/list")
    p_endpoint.add_argument(
        "--param",
        action="append",
        default=[],
        help="Query parameter key=value (repeatable)",
    )

    sub.add_parser("request-passcode", help="Call /account/submit-info with --email")

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        asyncio.run(_run(args))
    except OLAError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
