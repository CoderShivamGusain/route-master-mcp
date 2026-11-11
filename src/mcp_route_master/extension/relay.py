"""Live connections to signed-in users' route-master extensions (WebSocket relay).

Each extension opens `wss://<server>/ext/ws` from the user's Chrome (outbound, so it works behind
any home router), sends `{"type": "hello", "token": ...}` and then stays connected, pinging about
every 20 seconds. A tool call becomes one command:

    server → extension  {"type": "cmd", "id": 7, "op": "action", "args": {...}}
    extension → server  {"type": "result", "id": 7, "ok": true, "result": {...}}

One connection per user: a newer one (another Chrome, or a reconnect) replaces the older one.
Commands for one user run one at a time, because they all drive the same browser tab.

Connections live in this process's memory, so run a single server instance (Render's default). To
scale out, a shared message bus (e.g. Redis pub/sub) would be needed.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from mcp_route_master.extension.tokens import ExtensionTokens, Identity

log = logging.getLogger(__name__)

HELLO_TIMEOUT = 10  # seconds the extension has to identify itself
MAX_MESSAGE = 512 * 1024  # a page capture is ~20 KB; refuse anything absurd


class ExtensionOffline(Exception):
    """The user's extension isn't connected right now."""


class ExtensionFailed(Exception):
    """The extension ran the command but reported an error (message comes from the extension)."""


@dataclass
class _Conn:
    ws: WebSocket
    who: Identity
    version: str = ""
    pending: dict[int, asyncio.Future] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False


class Hub:
    """Keeps the live extension connections and sends them commands."""

    def __init__(self, tokens: ExtensionTokens) -> None:
        self._tokens = tokens
        self._conns: dict[str, _Conn] = {}
        self._ids = itertools.count(1)

    def status(self, sub: str) -> dict[str, Any] | None:
        conn = self._conns.get(sub)
        if conn is None or conn.closed:
            return None
        return {"email": conn.who.email, "version": conn.version}

    async def send(self, sub: str, op: str, args: dict[str, Any] | None = None, timeout: float = 40) -> Any:
        """Run one command in the user's extension and return its result.

        `timeout` also covers waiting for an earlier command. If the connection is replaced while
        waiting (the user's Chrome reconnected), the command goes to the new connection."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        for _ in range(3):
            conn = self._conns.get(sub)
            if conn is None or conn.closed:
                raise ExtensionOffline()
            try:
                await asyncio.wait_for(conn.lock.acquire(), max(deadline - loop.time(), 0.01))
            except asyncio.TimeoutError:
                raise ExtensionFailed(
                    "Your browser is still busy with the previous step; try again."
                ) from None
            if not conn.closed:
                break
            conn.lock.release()
        else:
            raise ExtensionOffline()
        try:
            cid = next(self._ids)
            fut: asyncio.Future = loop.create_future()
            conn.pending[cid] = fut
            try:
                await conn.ws.send_json({"type": "cmd", "id": cid, "op": op, "args": args or {}})
                msg = await asyncio.wait_for(fut, max(deadline - loop.time(), 0.01))
            except asyncio.TimeoutError:
                raise ExtensionFailed(
                    f"Your browser didn't finish '{op}' in {int(timeout)} seconds."
                ) from None
            except (RuntimeError, WebSocketDisconnect):
                raise ExtensionOffline() from None
            finally:
                conn.pending.pop(cid, None)
        finally:
            conn.lock.release()
        if not msg.get("ok"):
            raise ExtensionFailed(str(msg.get("error") or "the browser reported an error")[:300])
        return msg.get("result")

    async def handle(self, ws: WebSocket) -> None:
        """The /ext/ws endpoint: authenticate, register, then pump messages until it closes."""
        await ws.accept()
        try:
            hello = await asyncio.wait_for(ws.receive_json(), HELLO_TIMEOUT)
        except (asyncio.TimeoutError, WebSocketDisconnect, ValueError, KeyError):
            await _close(ws, 4400, "say hello first")
            return
        ok = isinstance(hello, dict) and hello.get("type") == "hello"
        who = self._tokens.verify(str(hello.get("token", ""))) if ok else None
        if who is None:
            await _close(ws, 4401, "sign in again")
            return
        conn = _Conn(ws=ws, who=who, version=str(hello.get("version", ""))[:20])
        old = self._conns.get(who.sub)
        self._conns[who.sub] = conn
        if old is not None:
            await self._drop(old, "replaced by a newer connection")
        await ws.send_json({"type": "welcome", "email": who.email})
        log.info("extension connected (%s)", who.email)
        try:
            while True:
                text = await ws.receive_text()
                if len(text) > MAX_MESSAGE:
                    await _close(ws, 4413, "message too large")
                    break
                await self._on_message(conn, text)
        except WebSocketDisconnect:
            pass
        except (KeyError, TypeError, RuntimeError):  # binary frame or broken message: drop this connection
            await _close(ws, 4400, "bad message")
        finally:
            conn.closed = True
            if self._conns.get(who.sub) is conn:
                del self._conns[who.sub]
            for fut in conn.pending.values():
                if not fut.done():
                    fut.set_exception(ExtensionOffline())
            log.info("extension disconnected (%s)", who.email)

    async def _on_message(self, conn: _Conn, text: str) -> None:
        try:
            msg = json.loads(text)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        kind = msg.get("type")
        if kind == "ping":
            await conn.ws.send_json({"type": "pong"})
        elif kind == "result" and isinstance(msg.get("id"), int):
            fut = conn.pending.get(msg["id"])
            if fut is not None and not fut.done():
                fut.set_result(msg)

    async def _drop(self, conn: _Conn, reason: str) -> None:
        conn.closed = True
        for fut in conn.pending.values():
            if not fut.done():
                fut.set_exception(ExtensionOffline())
        await _close(conn.ws, 4000, reason)


async def _close(ws: WebSocket, code: int, reason: str) -> None:
    with contextlib.suppress(Exception):
        await ws.close(code=code, reason=reason)
