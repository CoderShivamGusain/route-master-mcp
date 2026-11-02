"""Optional protections for the remote (HTTP) mode, set by environment variables.

Remote AI apps differ in what they can send:

- ChatGPT (developer-mode apps/connectors) and the Gemini app connect without credentials
  (or with OAuth, which this read-only server doesn't need). Leave MCP_AUTH_TOKEN unset for
  them; for light protection, serve MCP on a hard-to-guess path (--path / MCP_PATH).
- Gemini CLI, the Gemini API (managed agents with custom headers), Claude Code and scripts can
  send a header: set MCP_AUTH_TOKEN and they must send `Authorization: Bearer <token>`.
- Browser-based clients (web inspectors, custom web apps) need CORS: list their origins in
  MCP_CORS_ORIGINS (comma-separated, or * for any).

/health is always open so hosting platforms can check the server.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

Scope = MutableMapping[str, Any]
ASGIApp = Callable[[Scope, Callable[[], Awaitable[Any]], Callable[[Any], Awaitable[None]]], Awaitable[None]]


class BearerTokenMiddleware:
    """Reject HTTP requests without `Authorization: Bearer <token>` (except /health and CORS preflight)."""

    def __init__(self, app: ASGIApp, token: str, open_paths: tuple[str, ...] = ("/health",)) -> None:
        self.app, self.token, self.open_paths = app, token.encode(), open_paths

    async def __call__(self, scope: Scope, receive, send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("path") in self.open_paths
            or scope.get("method") == "OPTIONS"
        ):
            await self.app(scope, receive, send)
            return
        header = dict(scope.get("headers") or []).get(b"authorization", b"")
        scheme, _, value = header.partition(b" ")
        if scheme.lower() == b"bearer" and hmac.compare_digest(value.strip(), self.token):
            await self.app(scope, receive, send)
            return
        body = b'{"error":"unauthorized","detail":"send Authorization: Bearer <token>"}'
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b'Bearer realm="mcp-route-master"'),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def build_middleware(env: MutableMapping[str, str] | None = None) -> list[Middleware]:
    """Middleware for the HTTP app from MCP_CORS_ORIGINS and MCP_AUTH_TOKEN (both optional)."""
    env = os.environ if env is None else env
    stack: list[Middleware] = []
    origins = [o.strip() for o in env.get("MCP_CORS_ORIGINS", "").split(",") if o.strip()]
    if origins:  # outermost, so preflight requests get CORS headers before any auth check
        stack.append(
            Middleware(
                CORSMiddleware,
                allow_origins=origins,
                allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
                allow_headers=["*"],
                expose_headers=["mcp-session-id", "mcp-protocol-version"],
            )
        )
    token = env.get("MCP_AUTH_TOKEN", "").strip()
    if token:
        if len(token) < 16:
            raise SystemExit("MCP_AUTH_TOKEN must be at least 16 characters.")
        stack.append(Middleware(BearerTokenMiddleware, token=token))
    return stack
