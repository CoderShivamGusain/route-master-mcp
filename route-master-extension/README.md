# route-master Chrome extension

Lets your AI assistant (Claude, ChatGPT or Gemini) **book intercity buses for you on redBus and
MakeMyTrip, in your own Chrome**, while you just chat. You choose the bus, seat and stops, say
"confirm", and **pay yourself** on the official site.

```
You ── chat ──► Claude / ChatGPT / Gemini ── /app/mcp (signed in) ──► route-master server
                                                                            │  live connection
                                                                            ▼  (/ext/ws)
                              redbus.in / makemytrip.com ◄── this extension in your Chrome
```

- **No pairing codes.** You sign in with the same Google account in the extension and in your AI
  app's route-master connector. The server matches the two by account.
- **Works in every chat.** Once both are signed in, nothing to reconnect. Chrome starts the
  extension in the background with your computer.
- **Safe by design.** It only works on `redbus.in` and `makemytrip.com`. It never reads or types card,
  UPI, OTP, PIN or password fields, never presses pay or book buttons, and the final step to
  payment needs your "confirm" on a checked summary.

## Install (free, no Chrome Web Store needed)

1. Get this folder: clone the repo, or download it from GitHub (Code → Download ZIP) and unzip.
2. Open `chrome://extensions` in Chrome, and turn on **Developer mode** (top right).
3. Click **Load unpacked** and choose the `route-master-extension` folder.
4. Pin the route-master icon (puzzle icon → pin), click it, then **Sign in with Google**.
   Use a Google account that the server owner added as a test user (see below).
5. The popup should say **Connected as you@gmail.com**.

Keep the folder where it is: Chrome loads the extension from it. To update, replace the folder's
files and click the **reload** icon on the extension's card in `chrome://extensions`.

Chrome may show warnings about Developer-mode extensions, and on first install it lists the
permissions (see "Permissions" below).

## Connect your AI app (once per app)

Add a custom connector / app with this URL, then click **Sign in** and pick the **same Google
account** as in the extension:

```
https://mcp-route-master.onrender.com/app/mcp
```

| App | Where |
|---|---|
| Claude (claude.ai, Desktop, mobile) | Customize → Connectors → Add custom connector |
| ChatGPT | Settings → Apps → Advanced → Developer mode → Create app (authentication: OAuth) |
| Gemini app | Settings → Connected apps → Add a custom app |
| Gemini CLI | `gemini mcp add --transport http route-master https://mcp-route-master.onrender.com/app/mcp` |

Then just chat: *"Sleeper bus from Delhi to Dehradun tonight."* The AI opens redBus in a
route-master window in your Chrome (behind your other windows), ranks the buses, shows the seat
map, picks your seat and stops, fills the form with your OK, shows a summary, and after your
"confirm" opens the payment page in front for you to pay.

The bus list covers **redBus and MakeMyTrip together**: the AI opens one site, route-master reads the
other in a background tab for the same trip, and ranks every bus in one table (the Book column says on
which site each one is; the same bus on both shows once, at the cheaper fare).

In ChatGPT and Claude the seat map and the bus list also appear as a drawn picture (an MCP App):
each deck as on the site, single and double sides split by a dashed aisle, sleeper berths as long
tiles and seats as short ones, sold and women-only seats marked, the suggested seat highlighted. Tap
a free seat or a bus row to ask for it. The text says the same ("left: single sleeper berths ·
right: 2 seats side by side", `B`/`S` in each cell), so Gemini and any app that draws its own map get it too.

Use only this signed-in app for booking: turn off any older route-master app that points at `/mcp`,
or the AI may pick it and send you to the site instead of opening the seat map.

The open address `https://mcp-route-master.onrender.com/mcp` still works without sign-in: you get
links, rankings, scam checks and step-by-step guidance, and click on the site yourself.

## One-time setup for the server owner

Already done for `mcp-route-master.onrender.com` once these are in place:

1. **Google OAuth client** (Google Cloud → Google Auth Platform → Clients → your Web client) with
   both **Authorised redirect URIs**:
   ```
   https://mcp-route-master.onrender.com/auth/callback
   https://hajmndkodaikljkjjbnmpbiagfacmmei.chromiumapp.org/
   ```
   The second one is this extension's sign-in page. Its ID is fixed by the `key` in
   `manifest.json`, so it's the same on every computer.
2. **Test users** (Google Auth Platform → Audience): every Gmail that will sign in, up to 100 while
   the app is in Testing.
3. **Render environment:** `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `BASE_URL`,
   `JWT_SIGNING_KEY` (40+ random characters) and optionally `REDIS_URL` (keeps sign-ins across
   restarts). Signed-in mode turns on when `GOOGLE_CLIENT_ID` is set.

Hosting your own server: change `SERVER_URL` in `config.js` and the server host in
`manifest.json` → `host_permissions`.

## Permissions, and why

| Permission | Used for |
|---|---|
| `redbus.in`, `makemytrip.com` | Open and read booking pages, click seats and buttons there. Nothing else. |
| `mcp-route-master.onrender.com` | The live connection to the route-master server. |
| `identity` | "Sign in with Google". |
| `tabs`, `scripting` | Open the route-master window and run the page steps in it. |
| `debugger` | A real mouse click where the sites ignore script clicks (opening a bus, the continue buttons). Chrome briefly shows "route-master started debugging this browser" during that click. When the route-master window is hidden or minimized, Chrome doesn't deliver such clicks, so the click is made by script instead, and opening a bus is checked: if no seat map appears, it tries once more. |
| `storage`, `alarms` | Remember your sign-in; reconnect if the connection drops. |
| `background` | Start with Chrome, so the AI can reach your browser without you opening it. |

## How it works (for developers)

- `background.js` (service worker): signs in with Google (`chrome.identity.launchWebAuthFlow`,
  ID token with a nonce), trades the ID token at `/ext/token` for a route-master extension token
  (30 days), and keeps a WebSocket to `/ext/ws`. It pings every 20 seconds, which also keeps the
  service worker alive, and a 1-minute alarm reconnects. It runs booking-tab commands one at a time,
  each with a 55-second limit, so a slow page never holds up the next step (the server then waits and
  reads the page again instead of giving up).
- Commands: `open`, `capture`, `peek` (read another page, e.g. the other site or tomorrow's buses, in
  its own background tab; peeks run alongside each other, outside the queue), `action` (select_seats, choose_points, decline_addons, open_bus, continue), `fill`, `choose`,
  `show`. After every step it captures the page; the server turns that
  into the answer (ranked buses, seat map, stops, form check, or the payment hand-over).
- `page/actions.js` is **generated** from the server's `src/mcp_route_master/browser/scripts.py`
  (one source of truth). After changing scripts.py, run
  `python -m mcp_route_master.browser.extension_bundle`. A test fails if it's out of date.
- `page/forms.js` (hand-written): fills the user's own details and picks options such as "Male"
  or "Don't add Free Cancellation". It refuses secret fields and payment pages.
- Server side: `src/mcp_route_master/extension/` (`tokens.py`, `relay.py`, `app.py`, and
  `view.py` + `view.html`: the `ui://route-master/view-v1.html` MCP App page that draws the seat map
  and bus table from the tools' `structuredContent`).
- Tests: `tests/integration/test_extension_app.py` (server, relay and tools with a fake
  extension), `tests/browser/test_extension_scripts.py` (the page scripts in Chromium),
  `tests/unit/test_extension_tokens.py`.

## Limits

- **The route-master window may pop to the front for a few seconds** when a seat map is opened:
  Chrome doesn't draw pages in covered or minimized windows, and redBus only draws its seats while
  the page is displayed. It goes back behind (or minimized) as soon as the seats are read.

- **Desktop Chrome or Edge only.** Phones have no Chrome extensions; there the open `/mcp` gives
  guided booking.
- **Your computer must be on**, with Chrome running and the extension signed in. Otherwise the AI
  says it can't reach your browser and falls back to guided mode.
- **One Chrome per account at a time.** If two computers use the same account, the newer one takes
  over; press **Use this Chrome** in the popup to switch back.
- **Minimised window is experimental.** Some sites load results only while visible, so the default
  opens the window behind your other windows.
- **Testing mode:** only Google accounts the owner added as test users can sign in (up to 100).
- **Signing out** removes the token from this Chrome. To sign everyone out, the server owner
  changes `JWT_SIGNING_KEY`.
- Site changes can break a step. The AI then says what it couldn't do, and you can finish on the
  site yourself.
