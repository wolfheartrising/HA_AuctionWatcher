"""Mac.bid replaces the refresh token on every renewal; a stale one must lead to 'sign in again', not endless retries."""

import asyncio
import time

import pytest

from aw.sites import macbid as mb
from aw.sites.base import AuthError


def client(**kw):
    auth = {"access_token": "old", "refresh_token": "r1", "expires": time.time() - 10,
            "expiration_refresh": time.time() + 86400, "user_id": "7"}
    return mb.MacBidClient(None, auth, "dev", **kw)


def test_refused_refresh_without_password_asks_to_sign_in_again():
    c = client()

    async def refused(method, path, body=None, auth=True, any_status=False):
        return {"message": "Not Found", "_status": 404}
    c._call = refused
    with pytest.raises(AuthError, match="sign in again"):
        asyncio.run(c.ensure_fresh())


def test_one_renewal_at_a_time_and_new_login_saved_immediately():
    c = client()
    calls, saved = [], []
    c.on_auth_changed = lambda auth: saved.append(auth["refresh_token"])

    async def ok(method, path, body=None, auth=True, any_status=False):
        calls.append(body["token"])
        await asyncio.sleep(0.01)
        return {"access_token": "new", "refresh_token": "r2", "expires_in": 1800, "user_id": "7"}
    c._call = ok

    async def both():
        await asyncio.gather(c.ensure_fresh(), c.ensure_fresh(), c.ensure_fresh())
    asyncio.run(both())
    assert calls == ["r1"]            # the token was spent once, not three times
    assert saved == ["r2"]            # and the replacement was handed over to be saved right away
