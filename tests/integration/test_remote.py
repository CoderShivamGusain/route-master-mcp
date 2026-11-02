"""ChatGPT / Gemini compatibility: remote HTTP options, paste mode, search/fetch, traveller steps."""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import timedelta

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from page_fixtures import MMT_RESULTS

from mcp_route_master.http_options import build_middleware
from mcp_route_master.places.dates import today_ist
from mcp_route_master.server import mcp

pytestmark = pytest.mark.anyio
DAY = (today_ist() + timedelta(days=2)).isoformat()
TOKEN = "test-token-0123456789abcdef"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def secured_server(isolated_data_dir):
    port = _free_port()
    env = {
        **os.environ,
        "MCP_ROUTE_MASTER_DATA_DIR": str(isolated_data_dir),
        "MCP_AUTH_TOKEN": TOKEN,
        "MCP_CORS_ORIGINS": "https://inspector.example",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_route_master", "--transport", "http", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/health", timeout=1)
            break
        except OSError:
            if proc.poll() is not None:
                raise RuntimeError(proc.stderr.read().decode()) from None
            time.sleep(0.1)
    yield base
    proc.terminate()
    proc.wait(timeout=10)


def _post(url, headers):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            **headers,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


async def test_bearer_token_guards_mcp_but_not_health(secured_server):
    assert _post(secured_server + "/mcp", {}) == 401
    assert _post(secured_server + "/mcp", {"authorization": "Bearer wrong-token-xxxxxxxx"}) == 401
    with urllib.request.urlopen(secured_server + "/health") as r:
        assert r.status == 200
    transport = StreamableHttpTransport(secured_server + "/mcp", headers={"Authorization": f"Bearer {TOKEN}"})
    async with Client(transport) as client:
        names = {t.name for t in await client.list_tools()}
    assert {"search", "fetch", "rank_pasted_results"} <= names


async def test_cors_preflight_for_allowed_origin(secured_server):
    req = urllib.request.Request(
        secured_server + "/mcp",
        method="OPTIONS",
        headers={
            "origin": "https://inspector.example",
            "access-control-request-method": "POST",
            "access-control-request-headers": "content-type,authorization",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.headers["access-control-allow-origin"] == "https://inspector.example"


def test_middleware_is_off_by_default_and_rejects_short_tokens():
    assert build_middleware({}) == []
    with pytest.raises(SystemExit):
        build_middleware({"MCP_AUTH_TOKEN": "short"})


async def call(name, args):
    async with Client(mcp) as c:
        r = await c.call_tool(name, args, raise_on_error=False)
        return r.is_error, r.content[0].text


REDBUS_PASTE = (
    "Delhi Manali\n104 buses\nHRTC Buses\nहिमाचल रोड ट्रान्सपोर्ट कॉर्पोरेशन\n9 buses\nstarting from\n₹895\n"
    "20:00\n08:00\n12h\n36 Seats\n₹1,600\nOnwards\nHPTDC - 49531\nVolvo A/C Seater (2+2)\n3.7\n29\n"
    "Special offers just for you\nFIRST\nCopy\n22:25\n09:55\n11h 30m\n41 Seats\n₹797\nOnwards\nReoBus Pro\n"
    "Volvo 9600 Multi Axle Semi-Sleeper (2+2)\n4.8\n658\nTry new\n10% OFF\n22:15\n09:15\n11h\n27 Seats\n₹709\n"
    "₹638\nOnwards\nBedi Travels\nVolvo 9600 Multi Axle Semi-Sleeper (2+2)\n4.8\n656\n"
)


async def test_rank_pasted_redbus_and_mmt_text():
    err, text = await call(
        "rank_pasted_results",
        {"text": REDBUS_PASTE, "origin": "Delhi", "destination": "Manali", "travel_date": DAY},
    )
    assert not err and "3 buses" in text and "Bedi Travels" in text and "₹638" in text
    assert "HRTC (9 buses, from ₹895)" in text
    err, text = await call(
        "rank_pasted_results",
        {
            "text": MMT_RESULTS["text"],
            "origin": "Dehradun",
            "destination": "Agra",
            "travel_date": DAY,
            "bus_type": "sleeper",
        },
    )
    assert not err and "RTH STAR BUS" in text and "UPSRTC (2 buses" in text and "makemytrip.com" in text
    err, text = await call(
        "rank_pasted_results",
        {"text": "hello", "origin": "Delhi", "destination": "Manali", "travel_date": DAY},
    )
    assert err and "Copy the whole results page" in text


async def test_search_and_fetch_follow_the_standard_shape():
    err, text = await call("search", {"query": "bus from Delhi to Manali tomorrow"})
    results = json.loads(text)["results"]
    route = results[0]
    assert set(route) == {"id", "title", "url"} and route["id"].startswith("route:Delhi|Manali|")
    assert route["url"].startswith("https://")
    err, text = await call("fetch", {"id": route["id"]})
    doc = json.loads(text)
    assert set(doc) == {"id", "title", "text", "url", "metadata"} and "redbus.in" in doc["text"]
    safety = json.loads((await call("search", {"query": "refund scam on WhatsApp"}))[1])["results"]
    assert any(r["id"] == "guide:safety" for r in safety)
    doc = json.loads((await call("fetch", {"id": "guide:safety"}))[1])
    assert "1930" in doc["text"]
    ops = json.loads((await call("search", {"query": "HRTC"}))[1])["results"]
    assert any(r["id"] == "operator:HRTC" for r in ops)
    assert json.loads((await call("search", {"query": "zzzz"}))[1]) == {"results": []}
    err, text = await call("fetch", {"id": "nope"})
    assert err


async def test_booking_steps_for_the_traveller_have_no_page_code():
    err, text = await call(
        "booking_steps",
        {
            "origin": "Dehradun",
            "destination": "Varanasi",
            "travel_date": DAY,
            "operator": "Tiwari and Brothers",
            "departure": "17:00",
            "for_traveller": True,
        },
    )
    assert not err and "For the assistant" not in text and "page_action" not in text and "⏸" not in text
    assert "Never share an OTP" in text and "fromCityId=777" in text and text.count("\n") <= 12


async def test_every_tool_is_annotated_for_chatgpt():
    async with Client(mcp) as c:
        tools = await c.list_tools()
    for t in tools:
        a = t.annotations
        assert a.read_only_hint is True and a.destructive_hint is False and a.open_world_hint is False, t.name
        assert t.description and len(t.description) < 1024
        schema = json.dumps(t.input_schema)
        assert "anyOf" not in schema and "$ref" not in schema and "null" not in schema, t.name
