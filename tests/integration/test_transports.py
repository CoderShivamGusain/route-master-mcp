"""End-to-end: start the real CLI over stdio and over Streamable HTTP."""

import os
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import timedelta

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from mcp_route_master.places.dates import today_ist

pytestmark = pytest.mark.anyio
ARGS = {
    "origin": "Bangalore",
    "destination": "Hyderabad",
    "travel_date": (today_ist() + timedelta(days=2)).isoformat(),
}


# Desktop hosts (Claude Desktop, Gemini CLI, ChatGPT) may still speak the classic
# initialize-based protocol, so test both negotiation modes.
MODES = ["auto", "legacy"]


@pytest.mark.parametrize("mode", MODES)
async def test_stdio_transport(isolated_data_dir, mode):
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "mcp_route_master"],
        env={**os.environ, "MCP_ROUTE_MASTER_DATA_DIR": str(isolated_data_dir)},
    )
    async with Client(transport, mode=mode) as client:
        assert len(await client.list_tools()) == 12
        result = await client.call_tool("plan_bus_trip", ARGS)
    assert "bangalore-to-hyderabad" in result.content[0].text


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def http_server(isolated_data_dir):
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_route_master", "--transport", "http", "--port", str(port)],
        env={**os.environ, "MCP_ROUTE_MASTER_DATA_DIR": str(isolated_data_dir)},
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


@pytest.mark.parametrize("mode", MODES)
async def test_http_transport_and_health(http_server, mode):
    with urllib.request.urlopen(http_server + "/health") as resp:
        assert b'"status":"ok"' in resp.read()
    async with Client(http_server + "/mcp", mode=mode) as client:
        assert len(await client.list_tools()) == 12
        result = await client.call_tool("plan_bus_trip", ARGS)
    assert "bangalore-to-hyderabad" in result.content[0].text


@pytest.mark.parametrize("version", ["2024-11-05", "2025-03-26", "2025-06-18"])
def test_http_accepts_older_protocol_versions(http_server, version):
    """Raw JSON-RPC, as a host on an older MCP version would send it."""
    import json

    def post(payload, headers=None):
        req = urllib.request.Request(
            http_server + "/mcp",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
                **(headers or {}),
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode()

    init = post(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": version,
                "capabilities": {},
                "clientInfo": {"name": "raw", "version": "1"},
            },
        }
    )
    assert f'"protocolVersion":"{version}"' in init
    called = post(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "check_booking_safety", "arguments": {"content": "rebdus.in"}},
        },
        {"mcp-protocol-version": version},
    )
    assert "HIGH RISK" in called


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, "127.0.0.1"),  # local run: never exposed on the network by default
        ({"PORT": "10000"}, "0.0.0.0"),  # Render / Fly.io / Cloud Run assign PORT
        ({"PORT": "10000", "HOST": "127.0.0.1"}, "127.0.0.1"),  # explicit HOST wins
    ],
)
def test_default_bind_host(monkeypatch, env, expected):
    from mcp_route_master import server

    for key in ("HOST", "PORT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert server._parse_args(["--transport", "http"]).host == expected


async def test_starts_like_render_from_env_only(isolated_data_dir):
    """Render runs the container's CMD with MCP_TRANSPORT and PORT set and nothing else."""
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if k not in {"HOST", "MCP_ALLOWED_HOSTS"}}
    env |= {"MCP_TRANSPORT": "http", "PORT": str(port), "MCP_ROUTE_MASTER_DATA_DIR": str(isolated_data_dir)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_route_master"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                urllib.request.urlopen(base + "/health", timeout=1)
                break
            except OSError:
                if proc.poll() is not None:
                    raise RuntimeError(proc.stderr.read().decode()) from None
                time.sleep(0.1)
        # The public host name Render forwards must be accepted (no MCP_ALLOWED_HOSTS needed).
        req = urllib.request.Request(
            base + "/mcp",
            data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
            headers={
                "Host": "mcp-route-master.onrender.com",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-06-18",
            },
        )
        body = urllib.request.urlopen(req, timeout=10).read().decode()
        assert '"plan_bus_trip"' in body
        async with Client(base + "/mcp") as client:
            result = await client.call_tool("plan_bus_trip", ARGS)
        assert "bangalore-to-hyderabad" in result.content[0].text
    finally:
        proc.terminate()
        proc.wait(timeout=10)
