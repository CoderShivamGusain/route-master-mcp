"""Signed-in mode end to end: a real HTTP server, a fake Chrome extension on the WebSocket relay,
and an MCP client calling the browser_* tools at /app/mcp.

Google sign-in is replaced by a static test verifier (two users), and the extension's Google ID
token check is replaced by a stub, so no network is needed.
"""

import asyncio
import contextlib
import json
import socket
from datetime import datetime, timedelta

import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from page_fixtures import MMT_RESULTS, REDBUS_FORM, REDBUS_PAYMENT, REDBUS_RESULTS, REDBUS_SEATS, TRAVEL
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from mcp_route_master.extension import app as ext_app
from mcp_route_master.extension.app import Settings, build_combined_app
from mcp_route_master.extension.tokens import ExtensionTokens, Identity
from mcp_route_master.places.dates import IST, mmt_date, redbus_date

pytestmark = pytest.mark.anyio
KEY = "k" * 40
RAM = Identity(sub="sub-ram", email="ram@example.com")
SITA = Identity(sub="sub-sita", email="sita@example.com")
USERS = {
    "tok-ram": {"client_id": "chatgpt", "sub": RAM.sub, "scopes": []},
    "tok-sita": {"client_id": "claude", "sub": SITA.sub, "scopes": []},
}


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def server():
    port = _port()
    cfg = Settings(
        client_id="cid", client_secret="secret", base_url=f"http://127.0.0.1:{port}", signing_key=KEY
    )
    app = build_combined_app(cfg, auth=StaticTokenVerifier(tokens=USERS))
    srv = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, lifespan="on", ws="websockets-sansio", log_level="warning"
        )
    )
    task = asyncio.create_task(srv.serve())
    for _ in range(100):
        if srv.started:
            break
        await asyncio.sleep(0.05)
    yield f"127.0.0.1:{port}"
    srv.should_exit = True
    await task


class FakeExtension:
    """Answers relay commands the way the real extension does, from recorded page captures."""

    def __init__(self, pages: dict[str, dict]):
        self.pages, self.page, self.calls = pages, "results", []

    async def run(self, host: str, who: Identity, ready: asyncio.Event):
        async with connect(f"ws://{host}/ext/ws") as ws:
            await ws.send(
                json.dumps({"type": "hello", "token": ExtensionTokens(KEY).issue(who), "version": "test"})
            )
            assert json.loads(await ws.recv())["type"] == "welcome"
            ready.set()
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") != "cmd":
                    continue
                self.calls.append((msg["op"], msg["args"]))
                try:
                    reply = {"ok": True, "result": self.answer(msg["op"], msg["args"])}
                except Exception as e:  # the real extension reports a failed command the same way
                    reply = {"ok": False, "error": str(e)}
                await ws.send(json.dumps({"type": "result", "id": msg["id"], **reply}))

    def answer(self, op, args):
        if op == "action":
            self.page = {"open_bus": "seats", "continue": "payment"}.get(args["name"], self.page)
            return {"result": {"marked": True}, "capture": json.dumps(self.pages[self.page])}
        if op == "show":
            return {}
        if op == "peek":
            return {"capture": json.dumps(self.pages["tomorrow"])}
        return {"capture": json.dumps(self.pages[self.page])}


@contextlib.asynccontextmanager
async def extension(host, who, page="results"):
    fake = FakeExtension(
        {"results": REDBUS_RESULTS, "seats": REDBUS_SEATS, "form": REDBUS_FORM, "payment": REDBUS_PAYMENT}
    )
    fake.page = page
    ready = asyncio.Event()
    task = asyncio.create_task(fake.run(host, who, ready))
    await asyncio.wait_for(ready.wait(), 5)
    try:
        yield fake
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        await asyncio.sleep(0.05)


def app_client(host, token="tok-ram"):
    return Client(
        StreamableHttpTransport(f"http://{host}/app/mcp", headers={"Authorization": f"Bearer {token}"})
    )


async def call(client, name, args=None):
    r = await client.call_tool(name, args or {}, raise_on_error=False)
    return r.is_error, r.content[0].text


async def test_open_server_still_works_without_sign_in(server):
    async with Client(f"http://{server}/mcp") as c:
        assert len(await c.list_tools()) == 12


async def test_signed_in_server_needs_a_token(server):
    import httpx2

    async with httpx2.AsyncClient() as h:
        r = await h.post(
            f"http://{server}/app/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert r.status_code == 401  # (with Google sign-in it also points to the sign-in metadata)
    async with app_client(server) as c:
        names = {t.name for t in await c.list_tools()}
    assert {"browser_open", "browser_action", "browser_fill", "plan_bus_trip", "rank_buses"} <= names
    assert not names & {"page_action", "read_page"}  # the user's Chrome runs the page steps here


async def test_offline_extension_is_explained(server):
    async with app_client(server) as c:
        err, text = await call(c, "browser_status")
        assert not err and "isn't connected" in text
        err, text = await call(c, "browser_read")
        assert err and "isn't connected" in text


async def test_extension_with_bad_token_is_refused(server):
    async with connect(f"ws://{server}/ext/ws") as ws:
        await ws.send(json.dumps({"type": "hello", "token": "v1.bad.token"}))
        with pytest.raises(ConnectionClosed):
            await ws.recv()
    assert ws.close_code == 4401


async def test_browser_flow_through_the_relay(server):
    async with extension(server, RAM) as fake, app_client(server) as c:
        err, text = await call(c, "browser_status")
        assert "Connected: Chrome signed in as ram@example.com" in text
        err, text = await call(
            c,
            "browser_open",
            {"url": "https://www.redbus.in/bus-tickets/delhi-to-manali", "bus_type": "sleeper"},
        )
        assert not err and "HRTC (9 buses" in text
        assert not any(op == "peek" for op, _ in fake.calls)  # not today's page: no extra day
        err, text = await call(c, "browser_open", {"url": "https://redbus-pay.in/x"})
        assert err and "Only https redbus.in and makemytrip.com" in text
        err, text = await call(
            c,
            "browser_action",
            {"action": "open_bus", "operator": "Tiwari and Brothers", "departure": "17:00"},
        )
        assert not err and "## Seats: Tiwari and Brothers, 17:00 (redBus)" in text
        assert (
            "action",
            {
                "name": "open_bus",
                "args": {"site": "redBus", "operator": "Tiwari and Brothers", "departure": "17:00"},
                "click": True,
            },
        ) in fake.calls
        err, text = await call(c, "browser_action", {"action": "select_seats"})
        assert err and "Say which seats" in text


async def test_continue_to_payment_needs_the_users_confirm(server):
    async with extension(server, RAM, page="form") as fake, app_client(server) as c:
        err, text = await call(c, "browser_action", {"action": "continue", "user_confirmed": True})
        assert err and "requested_seats" in text  # the seats must be checked first
        err, text = await call(c, "browser_action", {"action": "continue", "requested_seats": ["3U"]})
        assert err and "wait for their 'confirm'" in text and "## Your booking:" in text
        assert not any(op == "action" for op, _ in fake.calls)  # nothing was clicked
        err, text = await call(
            c,
            "browser_action",
            {"action": "continue", "user_confirmed": True, "requested_seats": ["3U"], "approved_fare": 3499},
        )
        assert (
            not err and "**Total: ₹3,673.95**" in text and "payment page is now open in your Chrome" in text
        )
        assert ("show", {}) in fake.calls
        err, text = await call(
            c, "browser_action", {"action": "continue", "user_confirmed": True, "requested_seats": ["3U"]}
        )
        assert err and "already on the payment page" in text


async def test_each_user_only_reaches_their_own_browser(server):
    async with extension(server, RAM), app_client(server, "tok-sita") as c:
        err, text = await call(c, "browser_status")
        assert "isn't connected" in text  # Ram's Chrome is connected, not Sita's


async def test_newer_extension_connection_replaces_the_older(server):
    async with extension(server, RAM) as first:
        async with extension(server, RAM) as second, app_client(server) as c:
            await call(c, "browser_read")
            assert second.calls and not first.calls


async def test_ext_token_and_config_endpoints(server, monkeypatch):
    import httpx2

    async def fake_verify(id_token, client_id, **_):
        if id_token != "good":
            raise ext_app.SignInError("Google didn't accept this sign-in; sign in again.")
        return RAM

    monkeypatch.setattr(ext_app, "verify_google_id_token", fake_verify)
    async with httpx2.AsyncClient(base_url=f"http://{server}") as h:
        assert (await h.get("/ext/config")).json()["google_client_id"] == "cid"
        r = await h.post("/ext/token", json={"id_token": "good"})
        assert r.status_code == 200 and r.json()["email"] == RAM.email
        assert ExtensionTokens(KEY).verify(r.json()["token"]) == RAM
        r = await h.post("/ext/token", json={"id_token": "bad"})
        assert r.status_code == 401
        r = await h.post("/ext/token", content=b"not json")
        assert r.status_code == 400


async def test_continue_fails_closed_on_an_unrecognised_page(server):
    odd = json.loads(json.dumps(REDBUS_FORM))
    odd["url"] = odd["url"].replace("step=CI", "step=PI")  # the site renamed the step
    async with extension(server, RAM) as fake, app_client(server) as c:
        fake.pages["odd"] = odd
        fake.page = "odd"
        err, text = await call(
            c, "browser_action", {"action": "continue", "user_confirmed": True, "requested_seats": ["3U"]}
        )
        assert err and "can't tell which booking step" in text
        assert not any(op == "action" for op, _ in fake.calls)


async def test_broken_extension_message_only_drops_that_connection(server):
    async with connect(f"ws://{server}/ext/ws") as ws:
        await ws.send(json.dumps({"type": "hello", "token": ExtensionTokens(KEY).issue(SITA)}))
        assert json.loads(await ws.recv())["type"] == "welcome"
        await ws.send(json.dumps({"type": "result", "id": [1]}))  # ignored, not a crash
        await ws.send(json.dumps({"type": "ping"}))
        assert json.loads(await ws.recv())["type"] == "pong"
        await ws.send(b"binary frame")
        with pytest.raises(ConnectionClosed):
            await ws.recv()
    assert ws.close_code == 4400
    async with extension(server, RAM), app_client(server) as c:  # the server is still fine
        err, text = await call(c, "browser_status")
        assert "Connected" in text


async def test_open_bus_accepts_12_hour_times_and_reports_failures(server):
    """Live ChatGPT test, 12 Nov 2026: it passed "00:50 AM", the page script found no bus, and the
    AI only saw the unchanged list. Times are now normalised, and a failed step is an error."""
    async with extension(server, RAM) as fake, app_client(server) as c:
        err, text = await call(
            c, "browser_action", {"action": "open_bus", "operator": "FlixBus", "departure": "00:50 AM"}
        )
        assert not err
        sent = next(a for op, a in fake.calls if op == "action")
        assert sent["args"]["departure"] == "00:50"
        fake.answer = lambda op, args: (
            {"result": {"error": "bus not found on this page"}, "capture": json.dumps(REDBUS_RESULTS)}
            if op == "action"
            else {"capture": json.dumps(REDBUS_RESULTS)}
        )
        err, text = await call(
            c, "browser_action", {"action": "open_bus", "operator": "FlixBus", "departure": "1:00 AM"}
        )
        assert err and "didn't work: bus not found on this page" in text and "wasn't changed" in text
        err, text = await call(
            c, "browser_action", {"action": "open_bus", "operator": "FlixBus", "departure": "late"}
        )
        assert err and "Can't read the departure time" in text


def _evening_of_travel_day(monkeypatch, hour=18):
    """Make TRAVEL "today" at the given IST hour, as the server sees it."""

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(TRAVEL.year, TRAVEL.month, TRAVEL.day, hour, tzinfo=IST)

    monkeypatch.setattr(ext_app, "datetime", Clock)


def test_next_day_link_only_when_few_buses_are_left_today(monkeypatch):
    nxt = TRAVEL + timedelta(days=1)
    assert ext_app._next_day_if_few_left(json.dumps(REDBUS_RESULTS)) is None  # TRAVEL isn't today
    _evening_of_travel_day(monkeypatch)
    url = ext_app._next_day_if_few_left(json.dumps(REDBUS_RESULTS))
    assert url and redbus_date(nxt) in url and redbus_date(TRAVEL) not in url
    url = ext_app._next_day_if_few_left(json.dumps(MMT_RESULTS))
    assert url and mmt_date(nxt) in url
    assert ext_app._next_day_if_few_left(json.dumps(REDBUS_SEATS)) is None  # not a results page
    assert ext_app._next_day_if_few_left("not json") is None
    monkeypatch.setattr(ext_app, "FEW_LEFT", 3)  # 5 buses (all after 18:00) are enough
    assert ext_app._next_day_if_few_left(json.dumps(REDBUS_RESULTS)) is None


async def test_browser_open_adds_tomorrows_buses_when_few_are_left(server, monkeypatch):
    _evening_of_travel_day(monkeypatch)
    tomorrow = json.loads(json.dumps(REDBUS_RESULTS))
    tomorrow["url"] = tomorrow["url"].replace(redbus_date(TRAVEL), redbus_date(TRAVEL + timedelta(days=1)))
    async with extension(server, RAM) as fake, app_client(server) as c:
        fake.pages["tomorrow"] = tomorrow
        err, text = await call(c, "browser_open", {"url": REDBUS_RESULTS["url"]})
        assert not err and "here are tomorrow's too" in text
        assert text.count("ReoBus Pro") >= 2  # in today's table and tomorrow's
        peek = [a for op, a in fake.calls if op == "peek"]
        assert peek == [{"url": tomorrow["url"]}]
        del fake.pages["tomorrow"]  # an older extension, or tomorrow's page didn't load
        err, text = await call(c, "browser_open", {"url": REDBUS_RESULTS["url"]})
        assert not err and "ReoBus Pro" in text and "tomorrow's too" not in text


async def test_open_bus_says_so_when_no_seat_map_opens(server):
    """Live ChatGPT test, 12 Nov 2026: the click didn't open the seat map, the AI got the unchanged
    list back and sent the user to the site. Now it's an error that says what to do."""
    async with extension(server, RAM) as fake, app_client(server) as c:
        fake.answer = lambda op, args: {
            "result": {"marked": True} if op == "action" else None,
            "click": {"clicked": "real", "seat_map": False},
            "capture": json.dumps(REDBUS_RESULTS),
        }
        err, text = await call(
            c, "browser_action", {"action": "open_bus", "operator": "ReoBus Pro", "departure": "22:25"}
        )
        assert err and "no seat map opened" in text and "browser_show" in text
        assert "reload it" not in text
        fake.answer = lambda op, args: {"result": {"marked": True}, "capture": json.dumps(REDBUS_RESULTS)}
        err, text = await call(
            c, "browser_action", {"action": "open_bus", "operator": "ReoBus Pro", "departure": "22:25"}
        )
        assert err and "reload it in chrome://extensions" in text  # an extension without click reports


async def test_seat_map_comes_with_a_drawable_view(server):
    """ChatGPT shows the text seat map poorly; apps that support MCP Apps draw the real map."""
    from mcp_route_master.extension.view import VIEW_MIME, VIEW_URI

    async with extension(server, RAM), app_client(server) as c:
        tools = {t.name: t for t in await c.list_tools()}
        for name in ("browser_open", "browser_read", "browser_action"):
            meta = tools[name].meta or {}
            assert meta["ui"]["resourceUri"] == VIEW_URI and meta["openai/outputTemplate"] == VIEW_URI
        assert "ui" not in (tools["browser_fill"].meta or {})
        page = (await c.read_resource(VIEW_URI))[0]
        assert page.mime_type == VIEW_MIME and "<script>" in page.text
        r = await c.call_tool("browser_open", {"url": "https://www.redbus.in/bus-tickets/delhi-to-manali"})
        assert r.structured_content["kind"] == "buses" and r.structured_content["sections"][0]["rows"]
        r = await c.call_tool(
            "browser_action", {"action": "open_bus", "operator": "ReoBus Pro", "departure": "22:25"}
        )
        assert "## Seats: ReoBus Pro, 22:25" in r.content[0].text  # the text is still there for every AI
        assert r.structured_content["kind"] == "seats" and r.structured_content["decks"]
