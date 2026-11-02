"""The server is asynchronous end to end.

* every tool, resource, template and prompt handler is a coroutine;
* CPU-bound page parsing runs in a worker thread, so a large capture never blocks the event loop;
* many requests at once give the same answers as one at a time.
"""

import asyncio
import inspect
import json
import threading
import time

import pytest
from fastmcp import Client
from page_fixtures import MMT_RESULTS, REDBUS_RESULTS, REDBUS_SEATS, TRAVEL

from mcp_route_master import server

pytestmark = pytest.mark.anyio
DAY = TRAVEL.isoformat()


async def test_every_handler_is_a_coroutine():
    tools = await server.mcp.list_tools()
    resources = await server.mcp.list_resources()
    templates = await server.mcp.list_resource_templates()
    prompts = await server.mcp.list_prompts()
    assert len(tools) == 12 and resources and templates and prompts
    for item in [*tools, *resources, *templates, *prompts]:
        assert inspect.iscoroutinefunction(item.fn), f"{item.name} is not async"


async def test_parsing_runs_off_the_event_loop(monkeypatch):
    loop_thread = threading.get_ident()
    seen: list[int] = []
    real = server.parse_mmt_results

    def spy(c):
        seen.append(threading.get_ident())
        return real(c)

    monkeypatch.setattr(server, "parse_mmt_results", spy)
    async with Client(server.mcp) as c:
        r = await c.call_tool(
            "rank_pasted_results",
            {"text": MMT_RESULTS["text"], "origin": "Dehradun", "destination": "Agra", "travel_date": DAY},
        )
    assert "RTH STAR BUS" in r.content[0].text
    assert seen and all(t != loop_thread for t in seen)


async def test_slow_parse_does_not_block_other_work(monkeypatch):
    real = server.parse_redbus_results

    def slow(c):
        time.sleep(0.3)  # a very large capture
        return real(c)

    monkeypatch.setattr(server, "parse_redbus_results", slow)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        async with Client(server.mcp) as c:
            r = await c.call_tool("read_page", {"capture": json.dumps(REDBUS_RESULTS)})
    finally:
        task.cancel()
    assert "Bedi" in r.content[0].text or "buses" in r.content[0].text
    assert ticks >= 10  # the loop kept running while the parser slept


async def test_concurrent_requests_match_sequential_answers():
    calls = [
        ("plan_bus_trip", {"origin": "Delhi", "destination": "Manali", "travel_date": DAY}),
        ("plan_bus_trip", {"origin": "Dehradun", "destination": "Varanasi", "travel_date": DAY}),
        ("check_operator", {"operator_name": "HRTC"}),
        ("check_booking_safety", {"content": "Pay ₹500 to refund-help@upi to get your refund"}),
        ("read_page", {"capture": json.dumps(REDBUS_RESULTS)}),
        ("read_page", {"capture": json.dumps(REDBUS_SEATS), "seats_wanted": 1}),
        (
            "rank_pasted_results",
            {"text": MMT_RESULTS["text"], "origin": "Dehradun", "destination": "Agra", "travel_date": DAY},
        ),
        ("search", {"query": "bus from Delhi to Manali tomorrow"}),
    ]
    async with Client(server.mcp) as c:

        async def run(name, args):
            r = await c.call_tool(name, args, raise_on_error=False)
            return r.is_error, r.content[0].text

        one_by_one = [await run(n, a) for n, a in calls]
        at_once = await asyncio.gather(*(run(n, a) for _ in range(4) for n, a in calls))
    assert all(not err for err, _ in one_by_one)
    assert at_once == one_by_one * 4
