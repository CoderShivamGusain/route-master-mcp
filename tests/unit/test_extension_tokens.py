"""Extension sign-in: route-master's own extension tokens and the Google ID-token check."""

import httpx2
import pytest

from mcp_route_master.extension.app import settings_from_env
from mcp_route_master.extension.tokens import (
    ExtensionTokens,
    Identity,
    SignInError,
    verify_google_id_token,
)

KEY = "k" * 40
WHO = Identity(sub="1234567890", email="ram@example.com")


def test_issue_and_verify_round_trip():
    tokens = ExtensionTokens(KEY)
    assert tokens.verify(tokens.issue(WHO)) == WHO


def test_rejects_tampered_expired_and_foreign_tokens():
    tokens = ExtensionTokens(KEY)
    good = tokens.issue(WHO, now=1_000_000)
    version, payload, sig = good.split(".")
    assert tokens.verify(f"{version}.{payload}x.{sig}") is None  # payload changed
    assert tokens.verify(good, now=1_000_000 + 31 * 86400) is None  # older than 30 days
    assert ExtensionTokens("z" * 40).verify(tokens.issue(WHO)) is None  # another server's key
    for junk in ["", "v1", "v2.a.b", "v1.@@.##", "a.b.c.d"]:
        assert tokens.verify(junk) is None


def test_short_key_is_refused():
    with pytest.raises(ValueError):
        ExtensionTokens("short")


def _google(reply: dict, status: int = 200) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(lambda req: httpx2.Response(status, json=reply)))


@pytest.mark.anyio
async def test_google_id_token_checks():
    import time

    ok = {
        "aud": "cid",
        "azp": "cid",
        "iat": str(int(time.time()) - 5),
        "iss": "https://accounts.google.com",
        "sub": "42",
        "email": "Ram@Example.com",
        "email_verified": "true",
    }
    who = await verify_google_id_token("tok", "cid", http=_google(ok))
    assert who == Identity(sub="42", email="ram@example.com")
    stale = str(int(time.time()) - 3600)
    bad_tokens = [
        {**ok, "aud": "other"},
        {**ok, "azp": "other"},
        {**ok, "iss": "evil.com"},
        {**ok, "email_verified": "false"},
        {**ok, "sub": ""},
        {**ok, "iat": stale},
    ]
    for bad in bad_tokens:
        with pytest.raises(SignInError):
            await verify_google_id_token("tok", "cid", http=_google(bad))
    with pytest.raises(SignInError):
        await verify_google_id_token("tok", "cid", http=_google({"error": "invalid_token"}, 400))
    with pytest.raises(SignInError):
        await verify_google_id_token("", "cid", http=_google(ok))


def test_settings_from_env():
    assert settings_from_env({}) is None  # open mode only
    with pytest.raises(SystemExit, match="GOOGLE_CLIENT_SECRET"):
        settings_from_env({"GOOGLE_CLIENT_ID": "cid"})
    env = {
        "GOOGLE_CLIENT_ID": "cid",
        "GOOGLE_CLIENT_SECRET": "s",
        "BASE_URL": "https://x.example/",
        "JWT_SIGNING_KEY": "short",
    }
    with pytest.raises(SystemExit, match="32"):
        settings_from_env(env)
    cfg = settings_from_env({**env, "JWT_SIGNING_KEY": KEY, "REDIS_URL": " rediss://u:p@h:6379 "})
    assert cfg.base_url == "https://x.example" and cfg.redis_url == "rediss://u:p@h:6379"
