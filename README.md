# mcp-route-master

**An MCP server that helps travellers in India find the right intercity bus, book it on the
official site, and avoid booking scams.** It works with Claude, ChatGPT, Gemini and any other
MCP client, it is free to run, and it needs no API keys.

```
You: "Dehradun to Varanasi tonight, sleeper"
AI + route-master: finds the buses on redBus/MakeMyTrip, ranks them, checks the operator,
  shows the seat map, fills the form with your OK, checks everything, and hands you a
  "Pay here" link on the official site.
You: pay.
```

**Hosted server:** `https://mcp-route-master.onrender.com/mcp`. Add it to Claude, ChatGPT or Gemini in a minute, see
[Connect to the hosted server](#connect-to-the-hosted-server).

---

## Contents

- [How it works](#how-it-works)
- [What it can do](#what-it-can-do)
- [Tools](#tools)
- [Resources, templates and prompts](#resources-templates-and-prompts)
- [A booking, step by step](#a-booking-step-by-step)
- [Connect to the hosted server](#connect-to-the-hosted-server)
- [Signed-in mode: the Chrome extension](#signed-in-mode-the-chrome-extension)
- [Run it yourself](#run-it-yourself)
- [Configuration](#configuration)
- [Supported sites](#supported-sites)
- [Safety and privacy](#safety-and-privacy)
- [Project layout](#project-layout)
- [Development](#development)
- [Places, booking-site IDs and link formats](#places-booking-site-ids-and-link-formats)
- [Limitations and roadmap](#limitations-and-roadmap)
- [License](#license)

---

## How it works

route-master never opens booking websites itself. Booking sites forbid scraping, block bots, and
the payment must happen in a browser you control. So the work is split three ways:

```
            ┌────────────────────────── your computer ──────────────────────────┐
  You  ───► │  AI app (Claude, ChatGPT, Gemini…)  ──clicks/reads──►  your browser │ ──► redbus.in /
            │        │   ▲                                         (official site)│     makemytrip.com
            │  tools │   │ answers, scripts, checks                                │
            │        ▼   │                                                         │
            │   route-master (this server: knowledge + checks, no network calls) │
            └────────────────────────────────────────────────────────────────────┘
```

| Who | Does |
|---|---|
| **route-master** | Knows places, booking-link formats and the booking sites' pages. Ranks buses, checks operators and links for fraud, draws seat maps, gives the AI tested browser scripts, reads captured pages, checks the booking before payment. Makes **no network calls** and stores nothing but an optional review list of unknown place names. |
| **The AI app** | Talks to you, calls route-master's tools, and (if it can control a browser) opens the official site and clicks. |
| **You** | Choose the bus, seats and stops, agree before your details are typed, say **confirm**, and **pay yourself**. |

### Works with Claude, ChatGPT and Gemini

| App | Connect | How booking works |
|---|---|---|
| **Claude Desktop** | hosted URL (connector) or local (stdio) | **The AI clicks** in Claude's browser: `page_action` + `read_page`, you confirm and pay |
| **Claude Code / Claude in Chrome** | hosted URL or local (stdio) | The AI clicks (Chrome), or you click |
| **claude.ai** (web, mobile) | hosted URL (connector) | You click |
| **ChatGPT** (developer mode) | hosted URL | **You click**: paste the results page → `rank_pasted_results` → `booking_steps` for you |
| **Gemini app** | hosted URL (custom app; limited availability) | You click (paste mode) |
| **Gemini CLI** | hosted URL or local (stdio) | You click (paste mode) |
| **Gemini API / agents** | hosted URL, or your own server with a Bearer header | Your code decides |

**Quickest start:** add `https://mcp-route-master.onrender.com/mcp` to your AI app, see [Connect to the hosted server](#connect-to-the-hosted-server).

Every app gets the same links, ranking, operator and scam checks, seat maps and the pre-payment
check. Only apps that can control your browser can do the clicking; everywhere else you click
and route-master tells you exactly what to do. Setup guides: `docs/chatgpt.md`,
`docs/gemini.md`, `docs/remote_http.md`.

## What it can do

- **Find buses.** Dated links that open your route and date on redBus, MakeMyTrip and Goibibo
  (plus AbhiBus, Cleartrip, FlixBus, IRCTC Bus). About 260 places with aliases (Bangalore/Bengaluru,
  Banaras/Varanasi) and bus stands (Kashmiri Gate → Delhi).
- **Rank them.** Trust (government operator vs copycat, rating weighted by review count, live
  tracking, highway pickups), fit (a "semi-sleeper" is a reclining seat, not a bed; seater/sleeper
  buses need a berth), price, seats left, buses already gone, closeness to your preferred time.
- **Spot scams.** Look-alike domains (redbus-pay.in, makemytrip.com.offer.in), payments to personal
  UPI IDs, OTP/PIN requests, refund and remote-access tricks, with the 1930 / cybercrime.gov.in line.
- **Show seats.** A text seat map (decks, aisle, window, sold, women/men-only) and suggestions
  ("a lower single berth near the front").
- **Book in your browser.** Step-by-step script with stop points, ready-made browser actions,
  automatic reading of each page, a pre-payment check, and a final **Pay here** link.

## Tools

All 12 tools are read-only: they never book, pay or store anything, and answer in well under a second.

| Tool | Use it when | Returns |
|---|---|---|
| `plan_bus_trip` | "Bus from Delhi to Manali tomorrow, AC sleeper" | Dated booking links, one filter tip, a safety line |
| `rank_buses` | Results are visible (AI read them, or you pasted them) | Ranked table: time, bus, fare, trust 0-100, notes, one booking link |
| `check_operator` | You pick a bus or name an operator | Trust signals: RTC or copycat, rating vs reviews, tracking, pickup, bus-type traps |
| `check_booking_safety` | You share a link, number, SMS or WhatsApp message | Fake-site and scam patterns found, and what to do |
| `booking_steps` | You want to book a ranked bus | A script **for the AI**: every click in order, details to ask you for, ⏸ stop points |
| `explain_seat_map` | The AI read a bus's seat map | Text seat diagram, free berths vs seats, a suggestion |
| `booking_summary` | The passenger form is filled | Short card to check; reply **confirm** to go to payment |
| `page_action` | The AI controls a browser on redBus/MakeMyTrip | A tested script for the next step: `capture`, `open_bus`, `select_seats`, `choose_points`, `decline_addons`, `continue` |
| `read_page` | After running the capture script | The answer for that page: ranked buses, seat map, numbered stops, missing form fields or the summary, or the payment hand-over |
| `rank_pasted_results` | ChatGPT / Gemini: you copied the redBus or MakeMyTrip results page and pasted it | The same ranked table as `rank_buses` |
| `search` | ChatGPT deep research / knowledge, or any "find" request | Standard search results (JSON): routes with booking links, the safety checklist, government operators |
| `fetch` | Opening a `search` result | Standard document (JSON): route links, checklist text or operator details |

`page_action` + `read_page` (added in v0.5.0) mean the AI no longer writes its own page-reading
code: it runs route-master's capture script, passes the result back, and gets the finished
answer. A few clicks only work as real clicks (redBus bus cards, MakeMyTrip's last button): there
the script marks the element as `rm-target` and the AI clicks it.

## Resources, templates and prompts

For MCP clients that show them (Claude Desktop, Claude Code, MCP Inspector, …). Chat apps that
don't support them simply ignore them; everything is also reachable through the tools.

**Resources**

| URI | Content |
|---|---|
| `route-master://guide/how-it-works` | The booking flow and safety rules (Markdown) |
| `route-master://guide/safety-checklist` | What to check before paying; where to report fraud |
| `route-master://guide/supported-sites` | What `read_page`/`page_action` handle, and when it was checked |
| `route-master://scripts/capture.js` | The page-capture script |
| `route-master://data/government-operators` | State transport (RTC) operators with verified sites (JSON) |
| `route-master://data/booking-platforms` | Official booking sites and domains (JSON) |

**Resource templates**

| URI | Content |
|---|---|
| `route-master://places/{name}` | How a place resolves: city, state, aliases, bus stands, booking-site IDs |
| `route-master://routes/{origin}/{destination}/{date}` | Booking links for a route and date (`YYYY-MM-DD`) |
| `route-master://sites/{site}/booking-steps` | Booking script for `redbus` or `makemytrip` |

**Prompts**

| Prompt | Arguments | Starts |
|---|---|---|
| `find_bus` | origin, destination, date, preferences (optional) | A search: links, ranking, operator check, short answer |
| `book_bus` | operator, departure, origin, destination, date, site (default redBus) | A booking up to the payment page, pausing at every decision |
| `check_scam` | message | A fraud check of a link, number or message |

## A booking, step by step

What happens when you ask *"book a sleeper from Dehradun to Varanasi tonight"* in Claude Desktop
(tested live on 2 Nov 2026):

1. `plan_bus_trip` → the AI opens the dated redBus link.
2. `page_action(capture)` → `read_page` → ranked list (here: one bus, Tiwari and Brothers 17:00).
3. `check_operator` → 70/100, 4.5★ from 72 ratings, live tracking.
4. `page_action(open_bus)` → real click → `read_page` → seat map. **You pick a berth** (3U).
5. `page_action(select_seats)` → `page_action(continue)` → `read_page` → numbered stops.
   **You pick boarding and drop points.**
6. **You agree** to your details being typed; the AI types them and asks about each paid extra;
   `page_action(decline_addons)` for the ones you refused.
7. `read_page` on the form → booking summary. **You reply "confirm".**
8. `page_action(continue)` → payment page → `read_page` → one message:
   > **Total: ₹3,673.95** · seat held about 7 min
   > **Pay here (redbus.in):** https://www.redbus.in/paymentDetails
9. **You pay.** The ticket arrives by SMS/email.

## Connect to the hosted server

route-master is live at:

```
https://mcp-route-master.onrender.com/mcp
```

Streamable HTTP, no sign-in, no API key. All 12 tools are read-only. Health check:
<https://mcp-route-master.onrender.com/health>.

> **First call can be slow.** It runs on Render's free plan, which sleeps after about 15 minutes
> idle. The first request then takes up to a minute; if the app reports a timeout, ask again.

| App | Where to add it | Booking |
|---|---|---|
| [Claude (claude.ai, Desktop, mobile)](#claude-claudeai-desktop-mobile) | Customize → Connectors → Add custom connector | **Claude clicks** in its browser (Desktop) |
| [Claude Code](#claude-code) | `claude mcp add --transport http …` | Claude clicks (Claude in Chrome) or you click |
| [ChatGPT](#chatgpt) | Developer mode → add the app by URL | You click (paste the results page) |
| [Gemini app](#gemini-app-geminigooglecom) | Settings → Connected apps → Add a custom app | You click (paste the results page) |
| [Gemini CLI](#gemini-cli) | `gemini mcp add --transport http …` | You click (paste the results page) |
| [Any other MCP client](#any-other-mcp-client) | Streamable HTTP URL above | Depends on the client |

### Claude (claude.ai, Desktop, mobile)
1. Open [claude.ai](https://claude.ai) → **Customize** (or **Settings**) → **Connectors**.
2. Click **Add custom connector**.
3. Name: `route-master` · URL: `https://mcp-route-master.onrender.com/mcp` · leave the OAuth fields empty → **Add**.
4. In a chat, open the **+** / tools menu and make sure **route-master** is switched on.

A connector added on claude.ai also appears in Claude Desktop and the mobile apps. The Free plan
allows one custom connector; Pro, Max, Team and Enterprise allow more (on Team/Enterprise an Owner
adds it under Organization settings → Connectors). In **Claude Desktop** with its built-in browser
(or Claude in Chrome), Claude opens redBus/MakeMyTrip, reads each page with route-master and does
the clicking up to the payment page.

### Claude Code
```bash
claude mcp add --transport http route-master https://mcp-route-master.onrender.com/mcp
```
Check with `claude mcp list` (or `/mcp` inside Claude Code).

### ChatGPT
1. ChatGPT on the web → **Settings → Apps** (called *Apps & Connectors* or *Connectors* in some
   versions) → **Advanced settings** → turn on **Developer mode**.
2. Back in **Apps**, click **Create** (or **+ / Add**), then:
   - Name: `route-master`
   - MCP server URL: `https://mcp-route-master.onrender.com/mcp`
   - Authentication: **No authentication** (None)
   - Tick "I trust this application" → **Create**.
3. In a **new chat**, open **+ → Developer mode** (or **More → route-master**) and select
   **route-master** for that chat. Turning it on in Settings alone isn't enough.
4. Ask: *"Use route-master: sleeper bus Delhi to Dehradun tonight."*

OpenAI documents developer mode for Plus, Pro, Business, Enterprise and Education; our test also
worked on a Free account, but menus and availability change. ChatGPT can't control your browser,
so it works in **paste mode**: open the link route-master gives, select all and copy the results
page, paste it into the chat, and route-master ranks the real buses (`rank_pasted_results`).
Then it gives you step-by-step instructions to finish on the site (`booking_steps`). More in
`docs/chatgpt.md`.

### Gemini app (gemini.google.com)
1. On the **web** (gemini.google.com) → **Settings → Connected apps**.
2. Under **Custom apps**, click **Add a custom app**, paste `https://mcp-route-master.onrender.com/mcp` → **Next** → approve.
3. In a chat, mention it: *"@route-master sleeper bus Delhi to Dehradun tonight"*.

Custom apps can only be added from the web app (they then show on mobile too), and for now only
in English and for some accounts. Like ChatGPT, Gemini works in **paste mode**: copy the results
page from the link and paste it in. Gemini sometimes answers from its own knowledge instead of
calling the tools; if the answer has no route-master links or tables, say
*"use the route-master tools only"*. More in `docs/gemini.md`.

### Gemini CLI
```bash
gemini mcp add --transport http route-master https://mcp-route-master.onrender.com/mcp
```
Or add this to `~/.gemini/settings.json` (Windows: `%USERPROFILE%\.gemini\settings.json`):
```json
{
  "mcpServers": {
    "route-master": { "httpUrl": "https://mcp-route-master.onrender.com/mcp", "timeout": 60000 }
  }
}
```
Run `/mcp` inside Gemini CLI to see the tools; the prompts (`find_bus`, `book_bus`, `check_scam`)
appear as slash commands. A copy is in `examples/gemini_cli_remote.json`.

### Any other MCP client
Use transport **Streamable HTTP** and URL `https://mcp-route-master.onrender.com/mcp` (no headers). To test without an AI:
```bash
npx @modelcontextprotocol/inspector
```
then choose *Streamable HTTP*, paste the URL, **Connect** → **List Tools** (12 tools).

### If it doesn't work
| Problem | Fix |
|---|---|
| Timeout or "couldn't connect" on the first try | The free server was asleep. Wait a minute and ask again, or open the `/health` link first. |
| ChatGPT/Gemini answers without using route-master | Select the app for that chat (ChatGPT: + → Developer mode; Gemini: mention `@route-master`) and say "use route-master". |
| Fares or seat numbers that no page showed | They didn't come from route-master. Paste the results page so the real buses are ranked. |
| A link was removed as "suspicious" (Gemini) | Use the redBus link; or ask route-master for the links again. |
| Claude can't click on the site | Use Claude Desktop with its built-in browser (or Claude in Chrome); on claude.ai web/mobile you click yourself. |

## Signed-in mode: the Chrome extension

With the **route-master Chrome extension**, any AI app (Claude, ChatGPT, Gemini) can book in **your
own Chrome** while you only chat. The AI picks nothing on its own: you choose the bus, seat and
stops, and you pay yourself. Setup and details are in
[`route-master-extension/README.md`](route-master-extension/README.md).

| Address | Sign-in | What you get |
|---|---|---|
| `https://mcp-route-master.onrender.com/mcp` | none | Guided mode: links, ranking, scam checks, steps (you click) |
| `https://mcp-route-master.onrender.com/app/mcp` | Google | Everything above, plus `browser_*` tools that book through the extension |

1. Install the extension (**Load unpacked**) and **Sign in with Google**.
2. Add `/app/mcp` as a connector in your AI app and sign in with the **same Google account**.
3. Chat. Every step is one tool call that returns what the page now shows:
   - `browser_open`: ranked buses;
   - `browser_action` with `open_bus`, `select_seats`, `choose_points`, `decline_addons` or `continue`;
   - `browser_fill` and `browser_choose`: the passenger form, after you agree;
   - the final `continue`: only after you "confirm" a problem-free summary; the payment page then
     opens in front for you.

**How the server finds your browser:** the connector's sign-in and the extension's sign-in carry the
same Google account ID. The extension keeps a live WebSocket to `/ext/ws`, and the server sends each
step to that connection.

| Signed-in mode setting | Purpose |
|---|---|
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Google OAuth Web client. Setting the ID turns signed-in mode on. |
| `BASE_URL` | Public URL, e.g. `https://mcp-route-master.onrender.com` (redirect URI: `BASE_URL/auth/callback`) |
| `JWT_SIGNING_KEY` | 32+ random characters; signs sign-in and extension tokens (changing it signs everyone out) |
| `REDIS_URL` | Optional `rediss://…` (e.g. Upstash free tier): keeps sign-ins across restarts, encrypted |

## Run it yourself

To run your own copy, locally or on your own host.


Needs Python 3.10+ and [uv](https://docs.astral.sh/uv/getting-started/installation/). Until the
package is on PyPI, point `uvx` at this folder (replace the path with yours). After publishing,
use `"args": ["mcp-route-master"]`.

Without uv: `pip install -r requirements.txt && pip install -e .`, then use
`"command": "mcp-route-master"` (no args) in the configs below.

### Claude Desktop, local (free plan works)
Settings → Developer → Edit Config, then add to `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "route-master": {
      "command": "uvx",
      "args": ["--from", "C:\\Users\\LENOVO\\Desktop\\TechnocratIQ\\myProject\\mcp-route-master", "mcp-route-master"]
    }
  }
}
```
Restart Claude Desktop after every update. See `examples/claude_desktop_config.json`.
For assisted booking, use Claude's built-in browser (or Claude in Chrome).

### Gemini CLI, local (free)
Add the same block to `~/.gemini/settings.json` (Windows: `%USERPROFILE%\.gemini\settings.json`).
See `examples/gemini_settings.json`; for a remote server, `examples/gemini_cli_remote.json`.
Type `/mcp` in Gemini CLI to see the tools; the prompts appear as slash commands. More in
`docs/gemini.md`.

### Claude Code (local)
```bash
claude mcp add route-master -- uvx --from "C:\Users\LENOVO\Desktop\TechnocratIQ\myProject\mcp-route-master" mcp-route-master
```

### Your own remote server (for ChatGPT, claude.ai and the Gemini app)
These run in the cloud, so they connect only to a **public HTTPS URL** (Streamable HTTP at `/mcp`).

1. **Get a URL.**
   - Quick test: run `uvx --from . mcp-route-master --transport http --port 8000` and
     `cloudflared tunnel --url http://127.0.0.1:8000`, then set
     `MCP_ALLOWED_HOSTS=<the trycloudflare host>`.
   - Permanent: deploy the `Dockerfile`. **Render:** `render.yaml` is a ready Blueprint, see `docs/render.md`. `deploy/` has templates for Fly.io (Mumbai region)
     and Google Cloud Run. The server listens on `$PORT` and has a `/health` check.
2. **Add `https://<your-host>/mcp`:**
   - **ChatGPT:** turn on developer mode (paid plans) and create an app/connector with
     authentication **None**. See `docs/chatgpt.md`.
   - **Gemini app:** Settings → Connected apps → Custom apps → Add a custom app. Availability is
     limited (English, personal accounts). See `docs/gemini.md`.
   - **claude.ai:** Settings → Connectors → Add custom connector.
3. **Optional protection:**
   - `MCP_AUTH_TOKEN` makes clients send `Authorization: Bearer …`. Use it for Gemini CLI/API and
     scripts; ChatGPT and the Gemini app can't send a fixed token.
   - For those two, use a hard-to-guess `--path` instead.
   - `MCP_CORS_ORIGINS` allows browser-based clients.

### Host it on Prefect Horizon (FastMCP's hosting, free for personal projects)
Push this folder to GitHub, sign in at horizon.prefect.io with GitHub, pick the repo and set
**Entrypoint** `horizon_app.py:mcp` with **Authentication** off (needed for ChatGPT and the Gemini
app). You get `https://<server-name>.fastmcp.app/mcp`, and every push to `main` redeploys.
`horizon_app.py` makes the `src/` package importable on the host.

### Try it without an AI
```bash
npx @modelcontextprotocol/inspector uvx --from . mcp-route-master
```

### Compatibility notes
- Tool input schemas avoid `anyOf`, `null` and `$ref`, so Gemini accepts them; outputs are plain
  text (works in every host).
- Every tool is annotated `readOnlyHint: true`, `destructiveHint: false`, `openWorldHint: false`
  (it changes nothing and calls no outside service), which is what ChatGPT reads to decide
  whether to ask before running a tool.
- `search` / `fetch` follow the standard shapes ChatGPT uses for knowledge sources and deep research.
- Fully asynchronous: every tool, resource and prompt is `async`; large page captures are parsed in a
  worker thread so one request never blocks others on a shared remote server.
- Both the legacy and current MCP protocol versions are tested (2024-11-05, 2025-03-26, 2025-06-18).

## Configuration

| Setting | Default | Purpose |
|---|---|---|
| `--transport` / `MCP_TRANSPORT` | `stdio` | `stdio` for desktop clients, `http` for remote clients |
| `--host` / `HOST` | `127.0.0.1` | Use `0.0.0.0` when deploying |
| `--port` / `PORT` | `8000` | HTTP port |
| `--path` / `MCP_PATH` | `/mcp` | HTTP endpoint path |
| `--allowed-host` / `MCP_ALLOWED_HOSTS` | none | Public hostnames to accept (DNS-rebinding protection) |
| `MCP_ROUTE_MASTER_DATA_DIR` | per-user data folder | Local place overrides and the review queue |
| `MCP_ROUTE_MASTER_NO_LEARN` | off | `1` stops recording unknown place names |
| `MCP_AUTH_TOKEN` | none | HTTP mode: require `Authorization: Bearer <token>` (16+ characters). `/health` stays open |
| `MCP_CORS_ORIGINS` | none | HTTP mode: comma-separated origins (or `*`) allowed for browser-based clients |
| `LOG_LEVEL` | `WARNING` | Logs go to stderr (stdout carries MCP in stdio mode) |
| `GOOGLE_CLIENT_ID` and co. | none | Signed-in mode for the Chrome extension, see [Signed-in mode](#signed-in-mode-the-chrome-extension) |

`mcp-route-master --version` and `--show-data-dir` print the version and the data folder.

## Supported sites

| Site | Links | `read_page` | `page_action` | Checked live |
|---|---|---|---|---|
| redBus | dated (cities with IDs) | results, seats, stops, passenger form, payment | open bus\*, seats, stops, decline extras, continue\* | 1-2 Nov 2026, desktop and mobile layout |
| MakeMyTrip | dated (any city) | results, seats + stops, review form | open bus\*, seats, stops, decline extras, continue\* | 2 Nov 2026 |
| Goibibo | dated (cities with codes) | – | – | links only |
| AbhiBus, Cleartrip, FlixBus, IRCTC Bus | route page / homepage | – | – | links only |

\* marks the element for a real click (the site ignores scripted clicks there).

What the live tests taught (and the server now handles):
- **redBus:** guest checkout, no login. In a narrow window it shows its mobile layout (tap the bus
  card; "Fill passenger details" after choosing stops). It may pre-fill State of Residence and
  remember contact details; WhatsApp updates can start on or off. Results load as you scroll
  (only while the browser pane is visible). The list price ("₹2,199 onwards") is a seat; berths
  cost more. The payment page adds 5% GST and holds the seat for about 8 minutes.
- **MakeMyTrip:** guest checkout. The seat map has no numbers until a seat is clicked, so seats are
  labelled by position (L1, U3…) and the site's own number ("B") is used after selecting. Its
  `data-testid` can say "available" for greyed (sold) berths; the grey look wins. The last
  CONTINUE ignores scripted clicks. Email is required; Free Cancellation is priced per person.
- **Pickup point sets the departure time** (20:30 at the office, 20:50 at the mall), and many
  drops are highway stops (toll plazas, petrol pumps), which route-master flags.

If a site changes its layout, `read_page` says what it couldn't read and the AI falls back to
reading the page itself.

## Safety and privacy

- **Never automated:** logging in, creating accounts, OTPs, passwords, UPI PIN, card details,
  payment. The scripts never type personal details; the AI types them only after you agree.
- **Approval gate:** the AI goes to payment only when the latest `booking_summary` has no problems
  and you reply **confirm** to it. If the page changed, it shows a new summary and asks again.
- **Payment hand-over check:** if the payment total is more than the confirmed fare plus about 5%
  tax, the hand-over says so instead of calling it ready.
- **Official sites only:** `read_page` accepts only redbus.in and makemytrip.com pages (exact host
  match, look-alikes rejected); booking links and checkout pages are checked against official
  domains.
- **The capture script** reads only the open tab and returns it to the AI. On payment pages it
  keeps only the summary text and never reads input fields. Password fields are never read.
- **No storage:** passenger details pass through the AI and this server only for the current
  step; nothing is saved or logged. The only file written is the optional list of unknown place
  names (`pending_locations.json`), which never contains personal data.
- **No scraping, no bypassing:** route-master never fetches booking sites and never works around
  bot checks; if a site ignores a click, the user is given the link to press it.

## Project layout

```
mcp-route-master/
  src/mcp_route_master/
    server.py          FastMCP server: 12 tools, instructions, CLI (stdio + Streamable HTTP)
    extras.py          MCP resources, resource templates and prompts
    http_options.py    remote mode: optional Bearer token and CORS
    extension/         signed-in mode for the Chrome extension
      app.py             /app/mcp (Google sign-in, browser_* tools) and the combined web app
      relay.py           live WebSocket connections to users' extensions (/ext/ws)
      tokens.py          Google ID-token check and the extension's own signed token
    places/            where and when
      resolver.py        place names → hubs (aliases, bus stands, fuzzy match, ambiguity)
      links.py           per-site dated booking links
      dates.py           travel dates in India time, per-site date formats
    trust/             is it safe?
      operators.py       operator trust: RTC match, copycats, review-weighted rating, highway pickups
      safety.py          scam checks for links and messages
      bus_types.py       AC / sleeper / semi-sleeper / seater classification
    booking/           choosing and booking
      ranking.py         bus ranking: trust, fit level, price, seats, time
      seats.py           seat-map layout (clustering on-screen positions), drawing, suggestions
      guide.py           click-by-click booking scripts per site, with stop points
      checkout.py        pre-payment checks (official page, seats, passengers, contacts, add-ons)
    browser/           reading and acting on the open booking page
      scripts.py         browser scripts: page capture, select seats, choose stops, decline extras
      pages.py           parses captured redBus/MakeMyTrip pages (results, seats, stops, form, payment)
      extension_bundle.py  builds the extension's page/actions.js from scripts.py
    common/            shared helpers
      schema.py          portable optional fields for tool schemas (no anyOf/null)
      store.py           user data folder: local overrides, review queue
      textutil.py        data loading, normalising, edit distance
    data/              hubs.json (places, IDs), operators.json (RTCs, platforms)
  tests/
    unit/              one file per module
    integration/       MCP contract, stdio/HTTP end-to-end, live-test regressions
    browser/           browser scripts on mock pages in headless Chromium (+ js/ Node runner)
    fixtures/          page captures recorded live (redBus, MakeMyTrip)
  route-master-extension/  Chrome extension (Manifest V3): background.js, page/, popup/, README.md
  docs/                setup guides: ChatGPT, Gemini, remote HTTP, Render
  examples/            ready-to-copy client configs (Claude Desktop, Gemini CLI)
  deploy/              Fly.io and Cloud Run templates
  render.yaml          Render Blueprint (see docs/render.md)
  horizon_app.py       entrypoint for Prefect Horizon (FastMCP hosting)
  Dockerfile           for remote (HTTP) deployment
  pyproject.toml       package metadata; requirements.txt / requirements-dev.txt for pip
```

## Development

With uv:
```bash
uv sync                                  # creates .venv with dev tools
uv run pytest                            # 397 tests
uv run ruff check . && uv run ruff format --check .
```

With pip:
```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt -e .
pytest
```

`tests/browser/` runs the browser scripts in real Chromium. It needs Node.js and its
`playwright` package (`npm i -g playwright && npx playwright install chromium`) and is skipped
automatically without them.

Adding support for a page layout: capture the page with `route-master://scripts/capture.js`
(or `page_action(capture)`), add the JSON to `tests/fixtures/page_fixtures.py`, extend the parser
in `browser/pages.py`, and add a test in `tests/unit/test_pages.py`.

## Places, booking-site IDs and link formats

Places live in `src/mcp_route_master/data/hubs.json`: about 260 hubs with aliases, bus stands and
boarding areas (which resolve to the parent city).

- **Unknown places** are counted in `pending_locations.json` in the data folder
  (`mcp-route-master --show-data-dir`). It's a review list only and never changes results.
- **To add a place or ID locally**, create `locations.json` in the same folder, same format:
  ```json
  {"hubs": [{"name": "Kasol", "state": "Himachal Pradesh", "aliases": ["kasol village"]}],
   "routes": {"abhibus": {"Delhi|Kasol": 1234}}}
  ```
- **Check IDs on the live site first.** A wrong ID shows no error, just "0 buses" or another date.
  Tip: `https://www.redbus.in/bus-tickets/{from}-to-{to}` redirects to a URL containing both
  city IDs.

| Platform | Format | Needs |
|---|---|---|
| redBus | `/bus-tickets/{slug}-to-{slug}?fromCityId=&toCityId=&onward=DD-Mon-YYYY&doj=…` | redBus city IDs and slugs (Prayagraj = `allahabad`, 84832) |
| MakeMyTrip | `/bus/search/{Origin}/{Destination}/DD-MM-YYYY` | city names only |
| Goibibo | `/bus/search?bid=bus-{O}-{D}-{YYYYMMDD}-0-0-0-0-{GICC}-{GICC}` | Goibibo city codes |
| AbhiBus | `/buses/{routeId}/{Origin}-{Destination}` | AbhiBus route ID; date picked on the page |
| Cleartrip, FlixBus, IRCTC Bus | homepage | – |

redBus IDs filled in so far: Delhi, Manali, Bengaluru, Hyderabad, Dehradun, Prayagraj, Varanasi.
Government operator websites in `operators.json` are listed only when confirmed on a government
source; the safety checker treats those domains as official.

## Limitations and roadmap

- **No live data without a browser.** The server doesn't fetch seats or fares. For full
  automation without a browser, the path is a licensed partner API (redBus SeatSeller,
  MakeMyTrip/Goibibo affiliate, FlixBus partner) plus a hosted backend.
- **Two sites automated.** `read_page`/`page_action` cover redBus and MakeMyTrip; other sites get
  links. Site layouts change; the parsers are tested on recorded pages and report what they
  couldn't read.
- **Few routes have every ID.** Other routes get MakeMyTrip dated links plus homepages.
- **Pattern-based safety checks.** "No red flags" is not a guarantee; phone numbers can't be
  verified offline.
- **Planned:** suggesting a change at a nearby hub when a route has few or no direct buses; more
  redBus/Goibibo city IDs.

---

Version 0.11.0 · Python 3.10+ · FastMCP 4.1

## License

MIT, see `LICENSE`.
