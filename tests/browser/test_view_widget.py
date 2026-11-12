"""The seat-map / bus-table page (extension/view.html) in headless Chromium, fed like ChatGPT feeds it.

Skipped when Python's playwright (with its Chromium) isn't installed.
"""

import asyncio
import json

import pytest
from page_fixtures import REDBUS_RESULTS, REDBUS_SEATS

from mcp_route_master.extension.view import page_view, view_html
from mcp_route_master.server import read_page

pw = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def views():
    async def build():
        seats_text = await read_page(
            capture=json.dumps(REDBUS_SEATS), operator="ReoBus Pro", departure="22:25"
        )
        buses_text = await read_page(capture=json.dumps(REDBUS_RESULTS))
        return (
            page_view(json.dumps(REDBUS_SEATS), seats_text),
            page_view(json.dumps(REDBUS_RESULTS), buses_text),
        )

    return asyncio.run(build())


@pytest.fixture(scope="module")
def page(views):  # views first: sync Playwright runs its own event loop
    try:
        with pw.sync_playwright() as p:
            browser = p.chromium.launch()
            pg = browser.new_page()
            yield pg
            browser.close()
    except Exception as exc:  # no browser installed here
        pytest.skip(f"no Chromium: {exc}")


def _show(page, view):
    page.set_content(view_html())
    page.evaluate(
        """v => { window.__sent = [];
          const orig = window.postMessage.bind(window);
          window.addEventListener("message", e => window.__sent.push(e.data));
          orig({jsonrpc: "2.0", method: "ui/notifications/tool-result", params: {structuredContent: v}}, "*"); }""",
        view,
    )
    page.wait_for_timeout(100)


def test_widget_draws_the_seat_map_and_asks_for_a_tapped_seat(page, views):
    _show(page, views[0])
    assert page.locator("h2").inner_text() == "Seats: ReoBus Pro, 22:25 (redBus)"
    assert page.locator("button.seat").count() == 12
    assert page.locator("button.seat.sold").count() == 6
    assert page.locator("button.seat.pick").count() == 1
    page.locator("button.seat.pick").click()
    page.wait_for_timeout(100)
    asks = [m for m in page.evaluate("window.__sent") if m.get("method") == "ui/message"]
    assert asks and "seat 4L (lower deck)" in asks[-1]["params"]["content"][0]["text"]


def test_widget_draws_every_bus_and_escapes_text(page, views):
    buses = json.loads(json.dumps(views[1]))
    buses["sections"][0]["rows"][0]["bus"] = "<img src=x onerror=alert(1)>"
    _show(page, buses)
    assert page.locator("tbody tr, table tr").count() == len(buses["sections"][0]["rows"]) + 1
    assert page.locator("img").count() == 0  # page data is shown as text, never as HTML
