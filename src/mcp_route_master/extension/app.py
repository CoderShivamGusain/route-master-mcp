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

import contextlib
import os
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from functools import partial
from typing import Annotated, Any, Literal, get_args
from urllib.parse import urlsplit

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
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
from mcp_route_master.extension.relay import ExtensionFailed, ExtensionOffline, Hub
from mcp_route_master.extension.tokens import ExtensionTokens, SignInError, verify_google_id_token
from mcp_route_master.http_options import build_middleware
from mcp_route_master.places.dates import IST, mmt_date, redbus_date
from mcp_route_master.trust.bus_types import BusPreference

APP_PATH = "/app/mcp"
_BROWSER = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}

APP_INSTRUCTIONS = """\
route-master (signed in): finds intercity buses in India and books them on the official site in the
user's own Chrome, through the route-master extension. The user pays on that site themselves.

Flow (one tool call per step; every browser_* tool returns what the page now shows):
1. plan_bus_trip for the route and date (YYYY-MM-DD, India time). Then browser_open with its redBus
   or MakeMyTrip link (sleeper / time filters as the user asked): you get the ranked buses.
2. User picks a bus → browser_action(action="open_bus", operator=..., departure=...): the seat map.
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


async def _send(hub: Hub, op: str, args: dict[str, Any] | None = None, timeout: float = 45) -> Any:
    try:
        return await hub.send(_user(), op, args, timeout)
    except ExtensionOffline:
        raise ToolError(OFFLINE) from None
    except ExtensionFailed as exc:
        raise ToolError(f"The browser step didn't work: {exc}") from None


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
        return f"Connected: Chrome signed in as {state['email']} (extension {state['version'] or '?'})."

    @tool(annotations={**_BROWSER, "title": "Open a booking page in my browser"}, output_schema=None)
    async def browser_open(
        url: Annotated[
            str,
            Field(description="An https redBus or MakeMyTrip link, e.g. from plan_bus_trip.", max_length=600),
        ],
        bus_type: Annotated[BusPreference, Field(description="Results page: what the user wants.")] = "any",
        around_time: opt(str, description="Results page: preferred departure, HH:MM.", max_length=12) = None,
        sort_by: Annotated[SortBy, Field(description="Results page: how to sort.")] = "best",
    ) -> str:
        """Open a redBus or MakeMyTrip page in the user's Chrome and return what it shows (e.g. ranked buses).

        Use this for live buses, seats and fares, never web search: only the page in the user's Chrome
        is current."""
        parts = urlsplit(url.strip())
        if parts.scheme != "https" or site_of(url.strip()) is None:
            raise ToolError("Only https redbus.in and makemytrip.com pages can be opened.")
        out = await _send(hub, "open", {"url": url.strip()}, timeout=75)
        read = {"bus_type": bus_type, "around_time": around_time, "sort_by": sort_by}
        text = await _explain(out["capture"], **read)
        nxt = _next_day_if_few_left(out["capture"])
        if nxt is None:
            return text
        # Few buses still to leave today: read tomorrow's page in a background tab, so the user gets
        # enough choices. The booking tab stays on today's page.
        try:
            more = await _send(hub, "peek", {"url": nxt}, timeout=60)
        except ToolError:
            return text  # an older extension without "peek", or the page didn't load: today's list is enough
        tomorrow = await _explain(more["capture"], **read)
        return (
            f"{text}\n\n---\n**Only a few buses are still to leave today, so here are tomorrow's too.** "
            f"To book one of these, call browser_open with {nxt} first.\n\n{tomorrow}"
        )

    @tool(
        annotations={**_BROWSER, "readOnlyHint": True, "title": "Read the page in my browser"},
        output_schema=None,
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
    ) -> str:
        """Read the booking page open in the user's Chrome again (buses, seats, stops, form or payment)."""
        out = await _send(hub, "capture")
        return await _explain(
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

    @tool(annotations={**_BROWSER, "title": "Do the next booking step in my browser"}, output_schema=None)
    async def browser_action(
        action: Annotated[Action, Field(description="The step to do on the page open in the user's Chrome.")],
        operator: opt(str, description="open_bus: operator name as shown.", max_length=120) = None,
        departure: opt(str, description="open_bus: departure time as shown, HH:MM.", max_length=12) = None,
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
    ) -> str:
        """Do one booking step in the user's Chrome (open a bus, pick seats or stops, decline extras,
        continue) and return what the page shows afterwards."""
        now = (await _send(hub, "capture"))["capture"]
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
        elif action == "select_seats":
            if not (seats or seat_indexes):
                raise ToolError("Say which seats: seats (redBus) or seat_indexes (MakeMyTrip).")
            args.update(seats=list(seats or []), seat_indexes=list(seat_indexes or []))
        elif action == "choose_points":
            if not boarding or not dropping:
                raise ToolError("choose_points needs both the boarding and the drop point.")
            args.update(boarding=boarding, dropping=dropping)
        out = await _send(
            hub, "action", {"name": action, "args": args, "click": action in _CLICKS}, timeout=60
        )
        result = out.get("result")
        if isinstance(result, dict) and result.get("error"):
            # Say plainly that the step didn't happen, so the AI doesn't guess why.
            raise ToolError(f"The '{action}' step didn't work: {result['error']}. The page wasn't changed.")
        note = _action_note(result)
        after = out["capture"]
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
        return text

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

    @tool(annotations={**_BROWSER, "title": "Show the booking window"}, output_schema=None)
    async def browser_show() -> str:
        """Bring the route-master window in the user's Chrome to the front (e.g. to pay)."""
        await _send(hub, "show", timeout=10)
        return "The booking window is now in front in your Chrome."

    return app


FEW_LEFT = 10  # fewer buses than this still to leave today → also show tomorrow's


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
