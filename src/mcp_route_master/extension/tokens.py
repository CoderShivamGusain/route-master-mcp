"""Extension sign-in: verify a Google ID token, then issue route-master's own extension token.

Flow: the extension signs in with Google (chrome.identity) and gets an ID token. It posts that to
/ext/token once; we check it with Google and return an extension token signed with
JWT_SIGNING_KEY. The extension sends that token on every WebSocket connect, so nothing about the
user is stored on the server. Rotating JWT_SIGNING_KEY signs everyone out.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

import httpx2

GOOGLE_TOKENINFO = "https://oauth2.googleapis.com/tokeninfo"
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
TOKEN_DAYS = 30
ID_TOKEN_MAX_AGE = 600  # a Google sign-in older than 10 minutes isn't accepted


class SignInError(Exception):
    """The sign-in couldn't be verified (message is safe to show the user)."""


@dataclass(frozen=True)
class Identity:
    sub: str  # Google's stable account ID
    email: str


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class ExtensionTokens:
    """Signs and checks extension tokens: `v1.<payload>.<hmac>` (HMAC-SHA256)."""

    def __init__(self, signing_key: str) -> None:
        if len(signing_key) < 32:
            raise ValueError("JWT_SIGNING_KEY must be at least 32 characters.")
        # A key of its own, so these tokens can never be confused with FastMCP's OAuth tokens.
        self._key = hmac.new(signing_key.encode(), b"route-master-extension-token", hashlib.sha256).digest()

    def issue(self, who: Identity, now: float | None = None) -> str:
        now = time.time() if now is None else now
        payload = _b64(
            json.dumps(
                {"sub": who.sub, "email": who.email, "iat": int(now), "exp": int(now + TOKEN_DAYS * 86400)},
                separators=(",", ":"),
            ).encode()
        )
        return f"v1.{payload}.{_b64(hmac.new(self._key, payload.encode(), hashlib.sha256).digest())}"

    def verify(self, token: str, now: float | None = None) -> Identity | None:
        try:
            version, payload, sig = token.split(".")
            if version != "v1":
                return None
            good = hmac.new(self._key, payload.encode(), hashlib.sha256).digest()
            if not hmac.compare_digest(good, _unb64(sig)):
                return None
            data = json.loads(_unb64(payload))
            if data["exp"] < (time.time() if now is None else now):
                return None
            return Identity(sub=str(data["sub"]), email=str(data["email"]))
        except (ValueError, KeyError, TypeError):
            return None


async def verify_google_id_token(
    id_token: str, client_id: str, *, http: httpx2.AsyncClient | None = None
) -> Identity:
    """Check a Google ID token with Google and return who it belongs to.

    Accepts it only if it was issued by Google, for our OAuth client, and the email is verified.
    """
    if not id_token or len(id_token) > 4096:
        raise SignInError("Missing or malformed Google sign-in.")
    own = http is None
    client = http or httpx2.AsyncClient(timeout=10)
    try:
        resp = await client.get(GOOGLE_TOKENINFO, params={"id_token": id_token})
    except httpx2.HTTPError as exc:
        raise SignInError("Couldn't reach Google to check the sign-in; try again.") from exc
    finally:
        if own:
            await client.aclose()
    if resp.status_code != 200:
        raise SignInError("Google didn't accept this sign-in; sign in again.")
    info = resp.json()
    if info.get("aud") != client_id or info.get("iss") not in GOOGLE_ISSUERS:
        raise SignInError("This sign-in wasn't made for route-master.")
    if info.get("azp") not in (None, client_id):
        raise SignInError("This sign-in wasn't made for route-master.")
    try:
        fresh = time.time() - int(info.get("iat", 0)) <= ID_TOKEN_MAX_AGE
    except (TypeError, ValueError):
        fresh = False
    if not fresh:
        raise SignInError("This sign-in is too old; sign in again.")
    if str(info.get("email_verified")).lower() != "true" or not info.get("email") or not info.get("sub"):
        raise SignInError("Your Google account's email isn't verified.")
    return Identity(sub=str(info["sub"]), email=str(info["email"]).lower())
