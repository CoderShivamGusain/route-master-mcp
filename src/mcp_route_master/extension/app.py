"""Signed-in mode: Google sign-in, the browser_* tools, and the combined web app.

One process serves both modes:

    /mcp          open server, no sign-in (guided tools, as before)
    /app/mcp      signed-in server: every open tool plus browser_* tools that drive the user's
                  own Chrome through the route-master extension
    /ext/ws       WebSocket the extension keeps open (see relay.py)
    /ext/token    the extension trades a Google ID token for its own token (see tokens.py)
    /ext/config   public settings the extension needs (the Google client ID)
    OAuth routes  /authorize, /token, /register, /auth/callback, /.well-known/... for /app/mcp

Signed-in mode turns on only when GOOGLE_CLIENT_ID is set (plus GOOGLE_CLIENT_SECRET, BASE_URL
and JWT_SIGNING_KEY; REDIS_URL keeps sign-ins across restarts). Without it the server runs
exactly as before.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from functools import partial
from typing import Annotated, Any, Literal, get_args
from urllib.parse import urlsplit

from fastmcp import FastMCP
from fastmcp.apps.config import AppConfig
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.tools.base import ToolResult
from mcp.types import ImageContent, TextContent
from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.websockets import WebSocket

from mcp_route_master import __version__
from mcp_route_master import server as base
from mcp_route_master.booking.ranking import SortBy, TimeFormatError, fmt_clock, parse_clock
from mcp_route_master.booking.seats import Preference
from mcp_route_master.browser.pages import (
    CaptureError,
    load_capture,
    page_kind,
    parse_mmt_results,
    parse_redbus_results,
    route_of,
    site_of,
)
from mcp_route_master.common.schema import opt, opt_list
from mcp_route_master.extension.relay import (
    ExtensionBusy,
    ExtensionFailed,
    ExtensionOffline,
    ExtensionTimeout,
    Hub,
)
from mcp_route_master.extension.tokens import ExtensionTokens, SignInError, verify_google_id_token
from mcp_route_master.extension.view import (
    SKYBRIDGE_MIME,
    SKYBRIDGE_URI,
    VIEW_MIME,
    VIEW_URI,
    page_view,
    view_html,
)
from mcp_route_master.http_options import build_middleware
from mcp_route_master.places.dates import IST, mmt_date, redbus_date
from mcp_route_master.places.links import build_links
from mcp_route_master.trust.bus_types import BusPreference

APP_PATH = "/app/mcp"
_BROWSER = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}

APP_INSTRUCTIONS = """\
route-master (signed in): finds intercity buses in India and books them on the official site in the
user's own Chrome, through the route-master extension. The user pays on that site themselves.

Flow (one tool call per step; every browser_* tool returns what the page now shows):
1. plan_bus_trip for the route and date (YYYY-MM-DD, India time). Then browser_open with its
   MakeMyTrip link (it lists every bus); it also reads redBus for the same trip and ranks both together
   (the Book column says where each bus is); pass around_time="HH:MM" when the user gives a time ("around 9 am" → 09:00)
   and bus_type when they ask for one: you get the ranked buses, those near their time first.
2. User picks a bus → browser_action(action="open_bus", operator=..., departure=..., arrival=...,
   bus_site=<its Book column site>): the seat map. One call: route-master opens the right site itself,
   so never call browser_open again for this. Use the operator and HH:MM times exactly as in the table (arrival tells apart two buses
   of one operator at the same time). Never send the user to the site
   instead: if it fails, follow the tool's error message.
3. User picks seats → browser_action(action="select_seats", seats=[...]); then "continue".
4. Boarding/drop points → browser_action(action="choose_points", boarding=..., dropping=...); "continue".
5. Passenger page: ask before typing the user's details, then browser_fill and browser_choose
   (gender, with occurrence=2 for passenger 2; "Don't add ..." for paid extras the user didn't ask
   for). Show the summary it returns.
6. Only after the summary has no "Fix before paying" items and the user replies "confirm":
   browser_action(action="continue", user_confirmed=true, requested_seats=[...]). You get the total, the hold time and the
   payment page opens in the user's Chrome. Tell them to pay there.
If browser_status says the extension isn't connected, use the guided tools instead (plan_bus_trip
links, rank_pasted_results, booking_steps with for_traveller=true).

The seat map and bus table also appear as a picture in apps that can show one; still give the text.
If an answer lacks something the page should show, call browser_look (a screenshot) to see it.
If a step fails or times out, call browser_read before trying again; never switch to web search or send
the user to the site instead.

Seat maps come as tables (one per deck, a price in every free cell): show them to the user as they are.

Rules: never invent buses, prices or seats; show the tools' tables as they are, all rows (up to 10
buses, plus tomorrow's when shown), never a shortened list; ask before typing
personal details; never type OTPs, passwords, card or UPI details and never pay; keep replies short.
"""

OFFLINE = (
    "Your route-master extension isn't connected. Open Chrome on your computer (with the extension "
    "signed in to the same Google account), then try again. Without it, I can still give you the booking "
    "links and check the buses you paste or screenshot."
)
Action = Literal["select_seats", "choose_points", "decline_addons", "open_bus", "continue"]
_CLICKS = {"open_bus", "continue"}  # steps the site ignores unless the click is real
_FREE_CONTINUE = {"seats", "points"}  # continue from these never reaches payment; the passenger page is gated


class FieldInput(BaseModel):
    field: str = Field(description="The field's label, placeholder or id, e.g. 'Name', 'Age', 'Email ID'.")
    value: str = Field(description="What to type, exactly as the user gave it.", max_length=200)


def _user() -> str:
    token = get_access_token()
    sub = token and ((token.claims or {}).get("sub") or token.subject)
    if not sub:
        raise ToolError("Sign in to route-master first (the connector's Sign in button).")
    return str(sub)


class StepTimeout(ToolError):
    """The extension didn't answer in time; the page may still be loading."""


PEEK_BUDGET = 30  # seconds for reading the other site / tomorrow's page in background tabs
RETRY_WAIT = 3  # seconds to give a slow site before reading the page again


async def _send(
    hub: Hub, op: str, args: dict[str, Any] | None = None, timeout: float = 45, exclusive: bool = True
) -> Any:
    try:
        return await hub.send(_user(), op, args, timeout, exclusive=exclusive)
    except ExtensionOffline:
        raise ToolError(OFFLINE) from None
    except ExtensionTimeout as exc:
        raise StepTimeout(f"The browser step didn't work: {exc}") from None
    except ExtensionBusy as exc:  # never sent, so there's nothing to recover: just say so
        raise ToolError(str(exc)) from None
    except ExtensionFailed as exc:
        if "took too long" in str(exc):  # the extension's own step limit: the page may still be loading
            raise StepTimeout(f"The browser step didn't work: {exc}") from None
        raise ToolError(f"The browser step didn't work: {exc}") from None


async def _recapture(hub: Hub, tries: int = 2, timeout: float = 40) -> str | None:
    """After a timeout, don't give up: wait for the site, then read the page again (the step has
    often finished by then). None if the browser still doesn't answer; other errors pass through."""
    for _ in range(tries):
        await asyncio.sleep(RETRY_WAIT)
        try:
            return (await _send(hub, "capture", timeout=timeout))["capture"]
        except StepTimeout:
            continue
    return None


def _same_trip_page(capture: str, url: str) -> bool:
    """The re-read page is the results page that was asked for (not the previous page)."""
    try:
        c = load_capture(capture)
    except CaptureError:
        return False
    site = site_of(c.url)
    if site is None or site != site_of(url) or page_kind(c, site) != "results":
        return False
    want = route_of(load_capture(json.dumps({"v": 1, "url": url, "text": "buses found"})), site)
    return route_of(c, site).travel_date == want.travel_date


SLOW = (
    "The site is very slow right now. Wait a few seconds, then call browser_read to see the page; don't "
    "send the user to the site or search the web instead."
)


log = logging.getLogger(__name__)

# Tools whose answer the chat app can draw (seat map, bus table); ChatGPT also reads the alias.
_VIEW = {
    "meta": {
        "ui": AppConfig(resource_uri=VIEW_URI).model_dump(by_alias=True, exclude_none=True),
        "openai/outputTemplate": SKYBRIDGE_URI,
    }
}


def _with_view(text: str, capture: str, **read: Any) -> Any:
    """The text answer, plus the drawable view when this is a seat or results page."""
    try:
        view = page_view(capture, text, read.get("seats_wanted"), read.get("preferences"))
    except Exception:  # the picture is extra; never lose the text answer over it
        log.warning("route-master view failed", exc_info=True)
        view = None
    if view is None:
        return text
    return ToolResult(content=[TextContent(type="text", text=text)], structured_content=view)


def _version(v: Any) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return ()


def _outdated(hub: Hub) -> str:
    """A reload hint when the user's extension is older than this server (its page steps may be
    missing fixes, e.g. telling apart two buses at one time). Empty when it's up to date."""
    state = hub.status(_user())
    have = state and state.get("version")
    if not have or not _version(have) or _version(have) >= _version(__version__):
        return ""
    return (
        f" Note: the user's route-master extension is v{have} and this server is v{__version__}; ask the "
        "user to reload it once (chrome://extensions → route-master → ↻), then try again."
    )


def _site(capture: str) -> str | None:
    try:
        return site_of(load_capture(capture).url)
    except CaptureError:
        return None


async def _explain(capture: str, note: str = "", **read: Any) -> str:
    """Turn a capture from the user's tab into the answer for that step (same as read_page)."""
    read = {k: v for k, v in read.items() if v is not None}
    try:
        text = await base.read_page(capture=capture, **read)
    except ToolError as exc:
        text = f"_{exc}_"
    return f"{note}\n\n{text}".strip() if note else text


def create_app_server(hub: Hub, auth: Any) -> FastMCP:
    """The signed-in MCP server: all open tools (mounted) plus the browser_* tools."""
    app = FastMCP(name="mcp-route-master", instructions=APP_INSTRUCTIONS, version=__version__, auth=auth)
    app.mount(base.mcp)
    # These two hand the AI a script to run in its own browser. Here the user's Chrome does that
    # (browser_read / browser_action); offering both made ChatGPT pick page_action and give up.
    app.disable(names={"page_action", "read_page"}, components={"tool"})

    @app.resource(
        VIEW_URI, mime_type=VIEW_MIME, name="route-master view", description="Seat map and bus table."
    )
    def route_master_view() -> str:
        return view_html()

    @app.resource(SKYBRIDGE_URI, mime_type=SKYBRIDGE_MIME, name="route-master view (ChatGPT)")
    def route_master_view_chatgpt() -> str:
        return view_html()

    @app.resource(
        "ui://route-master/view-v1.html", mime_type=VIEW_MIME, name="route-master view (old address)"
    )
    def route_master_view_v1() -> str:  # hosts that cached the v0.9-v0.10 tool list still ask for it
        return view_html()

    tool: Callable[..., Any] = partial(base._tool, server=app)

    @tool(
        annotations={**_BROWSER, "readOnlyHint": True, "title": "Is my browser connected?"},
        output_schema=None,
    )
    async def browser_status() -> str:
        """Say whether the user's route-master extension is connected right now."""
        state = hub.status(_user())
        if state is None:
            return OFFLINE
        return (
            f"Connected: Chrome signed in as {state['email']} (extension {state['version'] or '?'})."
            + _outdated(hub)
        )

    @tool(annotations={**_BROWSER, "title": "Open a booking page in my browser"}, output_schema=None, **_VIEW)
    async def browser_open(
        url: Annotated[
            str,
            Field(description="An https redBus or MakeMyTrip link, e.g. from plan_bus_trip.", max_length=600),
        ],
        bus_type: Annotated[BusPreference, Field(description="Results page: what the user wants.")] = "any",
        around_time: opt(str, description="Results page: preferred departure, HH:MM.", max_length=12) = None,
        sort_by: Annotated[SortBy, Field(description="Results page: how to sort.")] = "best",
    ) -> Any:
        """Open a redBus or MakeMyTrip page in the user's Chrome and return what it shows (e.g. ranked buses).

        Use this for live buses, seats and fares, never web search: only the page in the user's Chrome
        is current."""
        parts = urlsplit(url.strip())
        if parts.scheme != "https" or site_of(url.strip()) is None:
            raise ToolError("Only https redbus.in and makemytrip.com pages can be opened.")
        try:
            out = await _send(hub, "open", {"url": url.strip()}, timeout=75)
        except StepTimeout:
            cap = await _recapture(hub, tries=1, timeout=25)
            if cap is None or not _same_trip_page(cap, url.strip()):
                raise ToolError(SLOW) from None
            out = {"capture": cap}
        read = {"bus_type": bus_type, "around_time": around_time, "sort_by": sort_by}
        text = await _explain(out["capture"], **read)
        # Every option: the other site's page for this trip, and tomorrow's when few buses are left
        # today. Both are read at the same time in background tabs, within PEEK_BUDGET; whatever
        # doesn't come back in time is left out (the booking tab is never touched).
        other = _other_site_link(out["capture"])
        nxt = _next_day_if_few_left(out["capture"])
        urls = [u for u in (other, nxt) if u]
        got: dict[str, str] = {}
        if urls:
            answers = await asyncio.gather(
                *(_send(hub, "peek", {"url": u}, timeout=PEEK_BUDGET, exclusive=False) for u in urls),
                return_exceptions=True,
            )
            got = {
                u: a["capture"]
                for u, a in zip(urls, answers, strict=True)
                if isinstance(a, dict) and "capture" in a
            }
        if other in got:
            try:
                merged = await base.read_results_pages(
                    [out["capture"], got[other]], bus_type, around_time, sort_by
                )
                if merged:
                    here = _site(out["capture"])
                    text = (
                        f"{merged}\n\n_Buses from redBus and MakeMyTrip (the booking window shows {here}). For "
                        "any bus, call browser_action open_bus with bus_site = the site in its Book column; "
                        "route-master switches site itself._"
                    )
            except Exception:  # the merged list is extra; never lose this site's answer over it
                log.warning("merging the other site's buses failed", exc_info=True)
        if nxt in got:
            tomorrow = await _explain(got[nxt], **read)
            text = (
                f"{text}\n\n---\n**Only a few buses are still to leave today, so here are tomorrow's too.** "
                f"To book one of these, call browser_open with {nxt} first.\n\n{tomorrow}"
            )
        return _with_view(text, out["capture"])

    @tool(
        annotations={**_BROWSER, "readOnlyHint": True, "title": "Read the page in my browser"},
        output_schema=None,
        **_VIEW,
    )
    async def browser_read(
        bus_type: Annotated[BusPreference, Field(description="Results page: what the user wants.")] = "any",
        sort_by: Annotated[SortBy, Field(description="Results page: how to sort.")] = "best",
        around_time: opt(str, description="Results page: preferred departure, HH:MM.", max_length=12) = None,
        operator: opt(str, description="Seat page: the bus's operator.", max_length=120) = None,
        departure: opt(str, description="Seat page: the bus's departure time.", max_length=12) = None,
        seats_wanted: opt(int, description="Seat page: how many seats the user needs.", ge=1, le=6) = None,
        preferences: opt_list(
            Preference,
            {"type": "string", "enum": list(get_args(Preference))},
            description="Seat page: window, aisle, front, back, together, lower, upper, cheapest, sleeper, seater.",
            max_length=8,
        ) = None,
        requested_seats: opt_list(
            str,
            {"type": "string", "maxLength": 8},
            description="Passenger page: the seats the user chose.",
            max_length=6,
        ) = None,
        approved_fare: opt(float, description="Payment page: the fare the user confirmed.", ge=0) = None,
    ) -> Any:
        """Read the booking page open in the user's Chrome again (buses, seats, stops, form or payment)."""
        try:
            out = await _send(hub, "capture")
        except StepTimeout:
            cap = await _recapture(hub)
            if cap is None:
                raise ToolError(SLOW) from None
            out = {"capture": cap}
        out["capture"] = await _await_seats(hub, out["capture"])
        if _seats_pending(out["capture"]):
            raise ToolError(STILL_LOADING)
        text = await _explain(
            out["capture"],
            bus_type=bus_type,
            sort_by=sort_by,
            around_time=around_time,
            operator=operator,
            departure=departure,
            seats_wanted=seats_wanted,
            preferences=preferences,
            requested_seats=requested_seats,
            approved_fare=approved_fare,
        )
        return _with_view(text, out["capture"], seats_wanted=seats_wanted, preferences=preferences)

    @tool(
        annotations={**_BROWSER, "title": "Do the next booking step in my browser"},
        output_schema=None,
        **_VIEW,
    )
    async def browser_action(
        action: Annotated[Action, Field(description="The step to do on the page open in the user's Chrome.")],
        operator: opt(str, description="open_bus: operator name as shown.", max_length=120) = None,
        departure: opt(str, description="open_bus: departure time as shown, HH:MM.", max_length=12) = None,
        arrival: opt(
            str,
            description="open_bus: arrival time as shown, HH:MM. Always pass it: it tells apart two buses of one "
            "operator at the same time.",
            max_length=12,
        ) = None,
        bus_site: opt(
            str,
            description="open_bus: the site in the bus's Book column, redBus or MakeMyTrip. route-master switches "
            "the booking window to that site itself.",
            max_length=20,
        ) = None,
        seats: opt_list(
            str,
            {"type": "string", "maxLength": 8},
            description="select_seats on redBus: seat numbers.",
            max_length=6,
        ) = None,
        seat_indexes: opt_list(
            int,
            {"type": "integer", "minimum": 0},
            description="select_seats on MakeMyTrip: the click numbers shown.",
            max_length=6,
        ) = None,
        boarding: opt(str, description="choose_points: boarding point number or name.", max_length=80) = None,
        dropping: opt(str, description="choose_points: drop point number or name.", max_length=80) = None,
        user_confirmed: Annotated[
            bool,
            Field(
                description="continue from the passenger page: true only after the user replied 'confirm' to the summary."
            ),
        ] = False,
        requested_seats: opt_list(
            str,
            {"type": "string", "maxLength": 8},
            description="continue from the passenger page: the seats the user chose, as the site numbers them.",
            max_length=6,
        ) = None,
        seats_wanted: opt(
            int, description="Seat page shown next: how many seats the user needs.", ge=1, le=6
        ) = None,
        approved_fare: opt(
            float, description="Payment page shown next: the fare the user confirmed.", ge=0
        ) = None,
    ) -> Any:
        """Do one booking step in the user's Chrome (open a bus, pick seats or stops, decline extras,
        continue) and return what the page shows afterwards."""
        try:
            now = (await _send(hub, "capture"))["capture"]
        except StepTimeout:
            now = await _recapture(hub)  # the previous step was still finishing
            if now is None:
                raise ToolError(SLOW) from None
        site = _site(now)
        if site is None:
            raise ToolError("The route-master tab isn't on redBus or MakeMyTrip; use browser_open first.")
        if action == "continue":
            kind = page_kind(load_capture(now), site)
            if kind == "payment":
                raise ToolError("You're already on the payment page: the user pays there themselves.")
            if kind == "passenger":
                if not requested_seats:
                    raise ToolError(
                        "Pass requested_seats (the seats the user chose) so the summary can check them."
                    )
                summary = await _explain(now, requested_seats=list(requested_seats))
                if "**confirm**" not in summary or "Fix before paying" in summary:
                    raise ToolError("The form isn't ready yet:\n\n" + summary)
                if not user_confirmed:
                    raise ToolError(
                        "Show the user this summary and wait for their 'confirm' before continuing:\n\n"
                        + summary
                    )
            elif kind not in _FREE_CONTINUE:
                # Fail closed: on a page we can't place, continuing might go straight to payment.
                raise ToolError(
                    "I can't tell which booking step this page is, so I won't press continue. Use browser_read, "
                    "or ask the user to press the button themselves."
                )
        args: dict[str, Any] = {"site": site}
        if action == "open_bus":
            if not operator or not departure:
                raise ToolError("open_bus needs the operator and departure exactly as shown.")
            try:
                departure = fmt_clock(parse_clock(departure.strip()))
            except TimeFormatError:
                raise ToolError(
                    f"Can't read the departure time '{departure}'; pass it as HH:MM, e.g. 00:50."
                ) from None
            args.update(operator=operator, departure=departure)
            if arrival:
                try:
                    args["arrival"] = fmt_clock(
                        parse_clock(re.sub(r"\s*\+\d+$", "", arrival.strip()))
                    )  # "03:25 +1"
                except TimeFormatError:
                    pass  # only a tie-breaker; a bad value just isn't used
        elif action == "select_seats":
            if not (seats or seat_indexes):
                raise ToolError("Say which seats: seats (redBus) or seat_indexes (MakeMyTrip).")
            args.update(seats=list(seats or []), seat_indexes=list(seat_indexes or []))
        elif action == "choose_points":
            if not boarding or not dropping:
                raise ToolError("choose_points needs both the boarding and the drop point.")
            args.update(boarding=boarding, dropping=dropping)

        async def step() -> tuple[dict[str, Any], str]:
            try:
                return await _send(
                    hub, "action", {"name": action, "args": args, "click": action in _CLICKS}, timeout=60
                ), ""
            except StepTimeout:
                # The click may well have worked and the site is just slow: wait, then read the page.
                cap = await _recapture(hub)
                if cap is None:
                    raise ToolError(SLOW) from None
                return {"result": {}, "click": {"clicked": "unknown", "timed_out": True}, "capture": cap}, (
                    f"_The '{action}' step was slow; this is the page now. Check it shows what was asked "
                    "before repeating the step (repeating a seat click would undo it)._"
                )

        if action == "open_bus":
            # The bus list mixes redBus and MakeMyTrip, but the booking window shows one site. Go to
            # the list on the bus's site first (live, 12 Nov 2026: the AI didn't, and gave up).
            links = _trip_links(now)
            want = _SITES.get((bus_site or "").strip().lower().replace(" ", ""))
            goto = None
            if want and want != site and want in links:
                goto = want
            elif site == "redBus" and site in links and page_kind(load_capture(now), site) == "seats":
                goto = site  # another bus's seat page is open: back to the list
            if goto:
                now, site = await _open_list(hub, links[goto], goto), goto
                args["site"] = site
            out, slow_note = await step()
            first = out.get("result")
            other = next((x for x in links if x != site), None)
            if (
                isinstance(first, dict)
                and re.search(r"not found|no bus named", str(first.get("error") or ""))
                and other
                and not goto
            ):
                # Not on this site's page: it's probably the other site's bus. Look there once.
                await _open_list(hub, links[other], other)
                args["site"] = other
                second, note2 = await step()
                if not (isinstance(second.get("result"), dict) and second["result"].get("error")):
                    out, slow_note, site = second, note2, other
                else:
                    first["error"] = f"{first['error']} (also not found on {other})"
        else:
            out, slow_note = await step()
        result = out.get("result")
        if isinstance(result, dict) and result.get("error"):
            # Say plainly that the step didn't happen, so the AI doesn't guess why.
            raise ToolError(
                f"The '{action}' step didn't work: {result['error']}. No bus was opened." + _outdated(hub)
            )
        note = " ".join(x for x in (_action_note(result), slow_note) if x)
        after = await _await_seats(hub, out["capture"])
        if _seats_pending(after):
            raise ToolError(STILL_LOADING)
        if action == "open_bus" and not _seat_map_open(after, site):
            # Don't hand back the unchanged bus list: the AI then guesses and sends the user away.
            click = out.get("click") or {}
            raise ToolError(
                f"Found {operator} {departure} and clicked it, but no seat map opened"
                + (
                    " (the extension is older than this server: reload it in chrome://extensions)"
                    if not click
                    else ""
                )
                + ". Call browser_read first: if it shows the seat map, carry on from there. If not, call "
                "browser_action open_bus once more; if that fails too, call browser_show so the user can open the "
                "bus in the route-master window, then call browser_read."
            )
        text = await _explain(
            after,
            note,
            operator=operator,
            departure=departure,
            seats_wanted=seats_wanted,
            requested_seats=list(requested_seats or seats or []) or None,
            approved_fare=approved_fare,
        )
        if _site(after) and page_kind(load_capture(after), _site(after) or site) == "payment":
            with contextlib.suppress(ToolError):
                await _send(hub, "show", timeout=10)
            text += "\n\n_The payment page is now open in your Chrome window. Pay there yourself._"
        return _with_view(text, after, seats_wanted=seats_wanted)

    @tool(annotations={**_BROWSER, "title": "Type the user's details on the form"}, output_schema=None)
    async def browser_fill(
        fields: Annotated[
            list[FieldInput], Field(description="Fields to fill, after the user agreed.", max_length=20)
        ],
        requested_seats: opt_list(
            str, {"type": "string", "maxLength": 8}, description="The seats the user chose.", max_length=6
        ) = None,
    ) -> str:
        """Type the user's own details (name, age, mobile, email...) into the passenger form, only after
        they agreed. Never card, UPI, OTP or password fields: those are refused."""
        out = await _send(hub, "fill", {"fields": [f.model_dump() for f in fields]})
        missed = out.get("missing") or []
        note = f"_Couldn't find: {', '.join(missed)}._" if missed else ""
        return await _explain(out["capture"], note, requested_seats=requested_seats)

    @tool(annotations={**_BROWSER, "title": "Pick an option on the form"}, output_schema=None)
    async def browser_choose(
        option: Annotated[
            str,
            Field(
                description="The option's text exactly as shown, e.g. 'Male', 'Female', \"Don't add Free Cancellation\".",
                max_length=80,
            ),
        ],
        occurrence: Annotated[
            int,
            Field(
                description="Which one, when the option appears more than once (e.g. 2 = passenger 2's 'Male').",
                ge=1,
                le=6,
            ),
        ] = 1,
        requested_seats: opt_list(
            str, {"type": "string", "maxLength": 8}, description="The seats the user chose.", max_length=6
        ) = None,
    ) -> str:
        """Pick one option on the passenger form (gender, or declining a paid extra). It can't press
        continue, book or pay buttons."""
        out = await _send(hub, "choose", {"text": option, "occurrence": occurrence})
        return await _explain(out["capture"], requested_seats=requested_seats)

    @tool(
        annotations={**_BROWSER, "readOnlyHint": True, "title": "Look at the page in my browser"},
        output_schema=None,
    )
    async def browser_look() -> Any:
        """A screenshot of the booking page open in the user's Chrome. Use it when a browser_* answer
        lacks something the page should show (stops, a button, an error), to see where it is, before
        ever telling the user to open the site themselves. Not available on the payment page."""
        out = await _send(hub, "shot", timeout=45)
        image = out.get("image") if isinstance(out, dict) else None
        if not isinstance(image, str) or not image or len(image) > 1_500_000:
            raise ToolError("Couldn't get a picture of the page. Call browser_read instead.")
        return ToolResult(
            content=[
                TextContent(
                    type="text",
                    text=f"Screenshot of the booking window ({out.get('url', '')}). Say what it shows; to press "
                    "an option on it use browser_choose or browser_action, never ask the user for OTPs or payment "
                    "details.",
                ),
                ImageContent(type="image", data=image, mimeType="image/jpeg"),
            ]
        )

    @tool(annotations={**_BROWSER, "title": "Show the booking window"}, output_schema=None)
    async def browser_show() -> str:
        """Bring the route-master window in the user's Chrome to the front (e.g. to pay)."""
        await _send(hub, "show", timeout=10)
        return "The booking window is now in front in your Chrome."

    return app


FEW_LEFT = 10  # fewer buses than this still to leave today → also show tomorrow's


_SITES = {"redbus": "redBus", "makemytrip": "MakeMyTrip", "mmt": "MakeMyTrip"}


def _trip_links(capture: str) -> dict[str, str]:
    """Each site's dated results link for the trip this page (bus list or seat map) is about."""
    try:
        c = load_capture(capture)
    except CaptureError:
        return {}
    site = site_of(c.url)
    if site is None or page_kind(c, site) not in ("results", "seats"):
        return {}
    route = route_of(c, site)
    if not route.origin or not route.destination or not route.travel_date:
        return {}
    r = base.get_resolver()
    ro, rd = r.resolve(route.origin), r.resolve(route.destination)
    if not all(x.ok and not x.fuzzy and not x.assumed for x in (ro, rd)):
        return {}
    return {
        x.platform: x.url
        for x in build_links(r, ro.hub, rd.hub, route.travel_date)
        if x.platform in ("redBus", "MakeMyTrip") and x.kind == "dated_search"
    }


async def _open_list(hub: Hub, url: str, site: str) -> str:
    """Show this site's bus list in the booking window; returns the page read."""
    try:
        return (await _send(hub, "open", {"url": url}, timeout=60))["capture"]
    except StepTimeout:
        cap = await _recapture(hub, tries=1, timeout=25)
        if cap is None or _site(cap) != site:
            raise ToolError(SLOW) from None
        return cap


def _other_site_link(capture: str) -> str | None:
    """The other site's dated results link for the same trip (redBus <-> MakeMyTrip), if it has one."""
    try:
        c = load_capture(capture)
    except CaptureError:
        return None
    site = site_of(c.url)
    if site is None or page_kind(c, site) != "results":
        return None
    route = route_of(c, site)
    if not route.origin or not route.destination or not route.travel_date:
        return None
    r = base.get_resolver()
    ro, rd = r.resolve(route.origin), r.resolve(route.destination)
    # Only exact matches: a guessed town could put another route's buses in the list.
    if not all(x.ok and not x.fuzzy and not x.assumed for x in (ro, rd)):
        return None
    o, d = ro.hub, rd.hub
    want = "MakeMyTrip" if site == "redBus" else "redBus"
    link = next((x for x in build_links(r, o, d, route.travel_date) if x.platform == want), None)
    return link.url if link is not None and link.kind == "dated_search" else None


def _next_day_if_few_left(capture: str) -> str | None:
    """Tomorrow's page link when this is today's results page and fewer than FEW_LEFT buses are
    still to leave; otherwise None."""
    try:
        c = load_capture(capture)
    except CaptureError:
        return None
    site = site_of(c.url)
    if site is None or page_kind(c, site) != "results":
        return None
    day = route_of(c, site).travel_date
    now = datetime.now(IST)
    if day != now.date():
        return None
    buses = (parse_redbus_results if site == "redBus" else parse_mmt_results)(c)[0]
    soon = now.hour * 60 + now.minute + 5
    left = 0
    for b in buses:
        try:
            if b.day not in (None, day.day) or parse_clock(b.departure) > soon:
                left += 1  # after midnight (MakeMyTrip shows its day), or later today
        except TimeFormatError:
            continue
    if left >= FEW_LEFT:
        return None
    nxt = day + timedelta(days=1)
    old, new = (redbus_date(day), redbus_date(nxt)) if site == "redBus" else (mmt_date(day), mmt_date(nxt))
    return c.url.replace(old, new) if old in c.url else None


def _seats_pending(capture: str) -> bool:
    """On the seat step, but the site hasn't drawn the seats yet."""
    try:
        c = load_capture(capture)
    except CaptureError:
        return False
    site = site_of(c.url)
    return site is not None and page_kind(c, site) == "seats" and not (c.seats or c.mmt_seats)


async def _await_seats(hub: Hub, capture: str, tries: int = 4) -> str:
    """Give a slow seat map time to appear: re-read the page (waiting RETRY_WAIT each time) while
    it's on the seat step without seats. Returns the latest capture either way."""
    for _ in range(tries):
        if not _seats_pending(capture):
            break
        await asyncio.sleep(RETRY_WAIT)
        try:
            capture = (await _send(hub, "capture", timeout=30))["capture"]
        except ToolError:
            break
    return capture


STILL_LOADING = (
    "The bus is open but its seat map is still loading. Wait a few seconds, then call browser_read (don't "
    "open the bus again and don't send the user to the site)."
)


def _seat_map_open(capture: str, site: str) -> bool:
    """True when the page shows a seat map (or has moved past it)."""
    try:
        c = load_capture(capture)
    except CaptureError:
        return False
    return bool(c.seats or c.mmt_seats) or page_kind(c, site) != "results"


def _action_note(result: Any) -> str:
    if isinstance(result, dict) and result.get("error"):
        return f"⚠ {result['error']}"
    if isinstance(result, dict) and result.get("declined"):
        return "Declined: " + ", ".join(map(str, result["declined"])) + "."
    return ""


# ---------------------------------------------------------------- configuration
class Settings(BaseModel):
    client_id: str
    client_secret: str
    base_url: str
    signing_key: str
    redis_url: str | None = None


def settings_from_env(env: Mapping[str, str] | None = None) -> Settings | None:
    """Signed-in mode settings, or None when GOOGLE_CLIENT_ID isn't set (open mode only)."""
    env = os.environ if env is None else env
    cid = env.get("GOOGLE_CLIENT_ID", "").strip()
    if not cid:
        return None
    missing = [
        k for k in ("GOOGLE_CLIENT_SECRET", "BASE_URL", "JWT_SIGNING_KEY") if not env.get(k, "").strip()
    ]
    if missing:
        raise SystemExit(f"Signed-in mode needs {', '.join(missing)} (GOOGLE_CLIENT_ID is set).")
    key = env["JWT_SIGNING_KEY"].strip()
    if len(key) < 32:
        raise SystemExit("JWT_SIGNING_KEY must be at least 32 characters.")
    return Settings(
        client_id=cid,
        client_secret=env["GOOGLE_CLIENT_SECRET"].strip(),
        base_url=env["BASE_URL"].strip().rstrip("/"),
        signing_key=key,
        redis_url=env.get("REDIS_URL", "").strip() or None,
    )


def google_auth(cfg: Settings) -> Any:
    """FastMCP's Google sign-in for /app/mcp. Sign-ins live in Redis when REDIS_URL is set
    (encrypted), otherwise in local files (lost on Render's free-plan restarts)."""
    from cryptography.fernet import Fernet
    from fastmcp.server.auth.jwt_issuer import derive_jwt_key
    from fastmcp.server.auth.providers.google import GoogleProvider

    storage = None
    if cfg.redis_url:
        from key_value.aio.stores.redis import RedisStore
        from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

        fernet = Fernet(
            derive_jwt_key(high_entropy_material=cfg.signing_key, salt="route-master-oauth-storage")
        )
        storage = FernetEncryptionWrapper(
            key_value=RedisStore(url=cfg.redis_url), fernet=fernet, raise_on_decryption_error=False
        )
    return GoogleProvider(
        client_id=cfg.client_id,
        client_secret=cfg.client_secret,
        base_url=cfg.base_url,
        required_scopes=["openid", "email"],
        client_storage=storage,
        jwt_signing_key=cfg.signing_key,
        cache_ttl_seconds=300,
    )


# ---------------------------------------------------------------- the combined web app
class CombinedApp:
    """Routes each request to the open app, the signed-in app or the extension endpoints."""

    def __init__(
        self, open_app: Any, open_path: str, app_app: Any, hub: Hub, cfg: Settings, tokens: ExtensionTokens
    ):
        self.open_app, self.app_app, self.hub = open_app, app_app, hub
        self.open_paths = {open_path, open_path.rstrip("/") + "/", "/health"}

        @contextlib.asynccontextmanager
        async def lifespan(_: Starlette):
            async with contextlib.AsyncExitStack() as stack:
                await stack.enter_async_context(open_app.router.lifespan_context(open_app))
                await stack.enter_async_context(app_app.router.lifespan_context(app_app))
                yield

        async def ext_token(request: Request) -> JSONResponse:
            try:
                body = await request.json()
                who = await verify_google_id_token(str(body.get("id_token", "")), cfg.client_id)
            except SignInError as exc:
                return JSONResponse({"error": str(exc)}, status_code=401)
            except (ValueError, AttributeError):
                return JSONResponse({"error": 'send JSON: {"id_token": "..."}'}, status_code=400)
            return JSONResponse({"token": tokens.issue(who), "email": who.email})

        async def ext_config(_: Request) -> JSONResponse:
            return JSONResponse({"google_client_id": cfg.client_id, "version": __version__})

        self.shell = Starlette(
            routes=[Route("/ext/token", ext_token, methods=["POST"]), Route("/ext/config", ext_config)],
            lifespan=lifespan,
        )

    async def __call__(self, scope, receive, send) -> None:
        kind, path = scope["type"], scope.get("path", "")
        if kind == "lifespan" or path.startswith("/ext/") and kind == "http":
            await self.shell(scope, receive, send)
        elif kind == "websocket":
            ws = WebSocket(scope, receive, send)
            if path == "/ext/ws":
                await self.hub.handle(ws)
            else:
                await ws.close(code=4404)
        elif path in self.open_paths:
            await self.open_app(scope, receive, send)
        else:
            await self.app_app(scope, receive, send)


def build_combined_app(
    cfg: Settings,
    *,
    open_path: str = "/mcp",
    allowed_hosts: list[str] | None = None,
    auth: Any = None,
) -> CombinedApp:
    """Both modes in one ASGI app. `auth` can be swapped in tests; it defaults to Google sign-in."""
    tokens = ExtensionTokens(cfg.signing_key)
    hub = Hub(tokens)
    open_app = base.mcp.http_app(
        path=open_path, stateless_http=True, middleware=build_middleware(), allowed_hosts=allowed_hosts
    )
    app_server = create_app_server(hub, auth if auth is not None else google_auth(cfg))
    app_app = app_server.http_app(path=APP_PATH, stateless_http=True, allowed_hosts=allowed_hosts)
    combined = CombinedApp(open_app, open_path, app_app, hub, cfg, tokens)
    combined.app_server = app_server  # type: ignore[attr-defined]  # for tests
    return combined
