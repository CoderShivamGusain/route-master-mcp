// route-master extension: keeps a live connection to the route-master server and does the
// booking steps it asks for, in a route-master window in this Chrome.
//
// Commands (from the server, see src/mcp_route_master/extension/relay.py):
//   open {url}                 open a redBus/MakeMyTrip page, then capture it
//   capture                    read the current page (no inputs on payment pages)
//   peek {url}                 read another page in a background tab (e.g. tomorrow's buses)
//   action {name,args,click}   run a page step (generated page/actions.js); real click if marked
//   fill {fields}              type the user's own details (never secret fields)
//   choose {text}              pick an option such as "Male" or "Don't add ..."
//   show                       bring the route-master window to the front (to pay)
import { SERVER_URL, VERSION, ALLOWED_HOSTS } from "./config.js";

const WS_URL = SERVER_URL.replace(/^http/, "ws") + "/ext/ws";
const sleep = ms => new Promise(r => setTimeout(r, ms));
let ws = null;
let pingTimer = null;
let backoff = 1000;
let queue = Promise.resolve();
let status = "signed-out"; // signed-out | connecting | connected | offline
const STEP_LIMIT = 55000; // the server waits up to 60-75 s per step
const HARD_CAP = 90000; // after this a stuck step is abandoned and stops before its next click
let stepNo = 0; // booking-tab steps, in order
let current = 0; // the step allowed to act on the tab now
const alive = g => g == null || g === current;
const stopIfStale = g => { if (!alive(g)) throw new Error("stopped: a newer step took over"); };

// ------------------------------------------------------------------ connection
async function connect({ manual = false } = {}) {
  const { token } = await chrome.storage.local.get("token");
  if (!token) { status = "signed-out"; return; }
  // Another Chrome on the same account took over: don't take it back automatically.
  const { replaced } = await chrome.storage.session.get("replaced");
  if (replaced && !manual) { status = "offline"; return; }
  if (manual) await chrome.storage.session.remove("replaced");
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  status = "connecting";
  const sock = new WebSocket(WS_URL);
  ws = sock;
  sock.onopen = () => sock.send(JSON.stringify({ type: "hello", token, version: VERSION }));
  sock.onmessage = ev => onMessage(sock, ev.data);
  sock.onclose = ev => {
    clearInterval(pingTimer);
    if (ws === sock) ws = null;
    if (ev.code === 4401) { chrome.storage.local.remove(["token", "email"]); status = "signed-out"; return; }
    if (ev.code === 4000) { status = "offline"; chrome.storage.session.set({ replaced: true }); return; }
    status = "offline";
    setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, 60000);
  };
}

async function onMessage(sock, data) {
  let msg;
  try { msg = JSON.parse(data); } catch { return; }
  if (msg.type === "welcome") {
    status = "connected"; backoff = 1000;
    clearInterval(pingTimer);
    // A message every 20 s keeps both the connection and this service worker alive.
    pingTimer = setInterval(() => sock.readyState === WebSocket.OPEN && sock.send('{"type":"ping"}'), 20000);
  } else if (msg.type === "cmd") {
    const answer = async g => {
      const work = run(msg.op, msg.args || {}, g);
      let reply;
      try { reply = { type: "result", id: msg.id, ok: true, result: await withTimeout(work, STEP_LIMIT, "This step took too long; the page may still be loading.") }; }
      catch (e) { reply = { type: "result", id: msg.id, ok: false, error: String(e && e.message || e) }; }
      if (sock.readyState === WebSocket.OPEN) sock.send(JSON.stringify(reply));
      return work;
    };
    // Reading another page (peek) uses its own background tab, so it runs alongside. Everything else
    // drives the booking tab one at a time: the server gets an answer within STEP_LIMIT, but the next
    // step still waits for this one to really finish (up to HARD_CAP), so two never act on the tab at
    // once; a step past HARD_CAP is abandoned and stops before its next click.
    if (msg.op === "peek") answer(null).catch(() => {});
    else queue = queue.then(async () => {
      const g = ++stepNo;
      current = g;
      const work = answer(g);
      await withTimeout(work, HARD_CAP, "abandoned").catch(() => {});
    });
  }
}

chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
chrome.alarms.create("keepalive", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener(a => a.name === "keepalive" && connect());
connect();

// ------------------------------------------------------------------ sign-in
async function signIn() {
  const cfg = await (await fetch(SERVER_URL + "/ext/config")).json();
  if (!cfg.google_client_id) throw new Error("The server hasn't turned on sign-in yet.");
  const nonce = crypto.randomUUID();
  const url = "https://accounts.google.com/o/oauth2/v2/auth?" + new URLSearchParams({
    client_id: cfg.google_client_id, response_type: "id_token", redirect_uri: chrome.identity.getRedirectURL(),
    scope: "openid email profile", nonce, prompt: "select_account",
  });
  const back = await chrome.identity.launchWebAuthFlow({ url, interactive: true });
  const idToken = new URLSearchParams(new URL(back).hash.slice(1)).get("id_token");
  if (!idToken) throw new Error("Google didn't return a sign-in.");
  const claims = JSON.parse(atob(idToken.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
  if (claims.nonce !== nonce) throw new Error("Sign-in check failed; try again.");
  const r = await fetch(SERVER_URL + "/ext/token", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id_token: idToken }),
  });
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || "The server refused the sign-in.");
  await chrome.storage.local.set({ token: j.token, email: j.email });
  if (ws) ws.close();
  backoff = 1000;
  await connect({ manual: true });
  return j.email;
}

async function signOut() {
  await chrome.storage.local.remove(["token", "email"]);
  if (ws) ws.close(1000);
  status = "signed-out";
}

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  (async () => {
    if (msg.type === "signin") return { email: await signIn() };
    if (msg.type === "signout") { await signOut(); return {}; }
    if (msg.type === "reconnect") { backoff = 1000; await connect({ manual: true }); return {}; }
    if (msg.type === "window") { await chrome.storage.local.set({ windowState: msg.value }); return {}; }
    const { email, windowState } = await chrome.storage.local.get(["email", "windowState"]);
    const { replaced } = await chrome.storage.session.get("replaced");
    return { status, email: email || null, windowState: windowState || "normal", replaced: !!replaced };
  })().then(reply, e => reply({ error: String(e && e.message || e) }));
  return true; // reply comes later
});

// ------------------------------------------------------------------ the route-master tab
const allowed = url => { try { const u = new URL(url); return u.protocol === "https:" && ALLOWED_HOSTS.some(h => h.test(u.hostname)); } catch { return false; } };
const isPayment = url => { try { const u = new URL(url); return /pay|checkout/i.test(u.pathname) || /^payments?\./i.test(u.hostname); } catch { return false; } };

async function getTab(create) {
  const { rmTab } = await chrome.storage.session.get("rmTab");
  if (rmTab) { try { return await chrome.tabs.get(rmTab); } catch { /* closed */ } }
  if (!create) throw new Error("No route-master tab is open; open a booking page first.");
  const { windowState } = await chrome.storage.local.get("windowState");
  const opts = windowState === "minimized"
    ? { url: "about:blank", focused: false, state: "minimized" }
    : { url: "about:blank", focused: false, width: 1280, height: 900 };
  const win = await chrome.windows.create(opts);
  const tab = win.tabs[0];
  await chrome.storage.session.set({ rmTab: tab.id });
  return tab;
}

function waitLoad(tabId, ms = 30000) {
  return new Promise(resolve => {
    const done = () => { clearTimeout(t); chrome.tabs.onUpdated.removeListener(on); resolve(); };
    const on = (id, info) => id === tabId && info.status === "complete" && done();
    const t = setTimeout(done, ms);
    chrome.tabs.onUpdated.addListener(on);
    chrome.tabs.get(tabId).then(tab => tab.status === "complete" && done(), done);
  });
}

// After a click: give the page a moment, and a new page up to 10 s to load. Every step must end well
// inside the server's 60 s, or the next command queues behind it and times out too.
async function settle(tabId) {
  await sleep(1500);
  const tab = await chrome.tabs.get(tabId);
  if (tab.status === "loading") await waitLoad(tabId, 10000);
  await sleep(1500);
}

async function exec(tabId, func, args = [], g = null) {
  stopIfStale(g); // an abandoned step never touches the page again
  await chrome.scripting.executeScript({ target: { tabId }, files: ["page/actions.js", "page/forms.js"] });
  stopIfStale(g);
  const [res] = await chrome.scripting.executeScript({ target: { tabId }, func, args });
  return res && res.result;
}

async function capture(tabId, g = null) {
  const tab = await chrome.tabs.get(tabId);
  if (!allowed(tab.url)) throw new Error("The route-master tab isn't on redBus or MakeMyTrip.");
  return exec(tabId, () => globalThis.__rm.capture({}), [], g);
}

// Click the element the page script marked. A real (trusted) mouse click when the tab is visible,
// because some sites ignore clicks made by scripts (Chrome briefly shows "route-master started
// debugging this browser"). Chrome doesn't deliver real clicks to a hidden, minimized or covered
// window, so there the click is made by script. Returns how it clicked.
const withTimeout = (p, ms, what) => Promise.race([p, sleep(ms).then(() => { throw new Error(what); })]);

function scriptClick() {
  const el = document.querySelector("[data-rm-target]");
  if (!el) return false;
  const r = el.getBoundingClientRect();
  const at = { bubbles: true, cancelable: true, view: window, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 };
  for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup"]) {
    el.dispatchEvent(new (type.startsWith("pointer") ? PointerEvent : MouseEvent)(type, at));
  }
  el.click();
  return true;
}

async function realClick(tabId, { trusted = true, g = null } = {}) {
  stopIfStale(g);
  const point = await exec(tabId, () => {
    const el = document.querySelector("[data-rm-target]");
    if (!el) return null;
    el.scrollIntoView({ block: "center" });
    const r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2, hidden: document.visibilityState !== "visible" };
  }, [], g);
  if (!point) throw new Error("Nothing to click on this page.");
  const byScript = async () => {
    if (!(await exec(tabId, scriptClick, [], g))) throw new Error("Nothing to click on this page.");
    return "script";
  };
  if (!trusted || point.hidden) return byScript();
  const target = { tabId };
  const detach = () => withTimeout(chrome.debugger.detach(target), 3000, "detach timed out").catch(() => {});
  const attaching = chrome.debugger.attach(target, "1.3");
  try {
    await withTimeout(attaching, 5000, "attach timed out");
  } catch {
    attaching.then(detach, () => {}); // if it attaches late, let go of it
    return byScript(); // DevTools open, debugging refused, or too slow
  }
  try {
    const base = { x: point.x, y: point.y, button: "left", clickCount: 1 };
    const send = (type, extra = {}) => withTimeout(
      chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", { ...base, type, ...extra }), 5000,
      "Chrome didn't deliver the click; keep the route-master window open (not minimized) and try again.");
    try {
      await send("mouseMoved", { button: "none" });
    } catch {
      await detach();
      return byScript(); // nothing was pressed yet, so a script click can't double up
    }
    stopIfStale(g);
    await send("mousePressed");
    await send("mouseReleased");
    return "real";
  } finally {
    await detach();
  }
}

// open_bus: wait for the seat map. If a real click didn't open it, try once by script, but only
// when the button is still there unchanged and nothing is loading (else the map may be on its way,
// and a second click would close it).
const seatMapOpen = tabId => exec(tabId, () =>
  !!document.querySelector('[data-testid^="seat_"], [aria-label^="Seat number"]') || /[?&]step=/.test(location.search));
const safeToRetry = tabId => exec(tabId, () => {
  const el = document.querySelector("[data-rm-target]");
  if (!el || !el.isConnected) return false;
  const card = el.closest("li") || el.parentElement;
  const loading = card && card.querySelector('[class*="loader" i], [class*="spinner" i], [class*="shimmer" i]');
  return !loading && (location.host.includes("makemytrip") ? /^select seats$/i.test(el.textContent.trim()) : true);
});

async function waitFor(check, ms) {
  const end = Date.now() + ms;
  while (Date.now() < end) { if (await check().catch(() => false)) return true; await sleep(500); }
  return false;
}

async function openBus(tabId, g) {
  const start = Date.now();
  const how = await realClick(tabId, { g });
  if (await waitFor(() => seatMapOpen(tabId), 6000)) return { clicked: how, seat_map: true };
  // Retry only with time to spare: the whole step must end well inside the server's 60 s.
  if (how === "real" && Date.now() - start < 25000 && await safeToRetry(tabId).catch(() => false)) {
    stopIfStale(g);
    try { await realClick(tabId, { trusted: false, g }); } catch { /* the button went away: check once more */ }
    if (await waitFor(() => seatMapOpen(tabId), 6000)) return { clicked: "real+script", seat_map: true };
    return { clicked: "real+script", seat_map: false };
  }
  return { clicked: how, seat_map: false };
}

async function run(op, args, g = null) {
  if (op === "open") {
    if (!allowed(args.url)) throw new Error("Only https redBus and MakeMyTrip pages can be opened.");
    const tab = await getTab(true);
    await chrome.tabs.update(tab.id, { url: args.url });
    await sleep(500);
    await waitLoad(tab.id, 30000);
    await sleep(2500); // the sites render the list after the page loads
    return { capture: await capture(tab.id, g) };
  }
  if (op === "peek") {
    // Read another page (e.g. tomorrow's buses) in a background tab; the booking tab stays put.
    if (!allowed(args.url)) throw new Error("Only https redBus and MakeMyTrip pages can be opened.");
    // Use the booking window when there is one; never create it here (a peek runs alongside other
    // steps), so a peek without one gets its own throwaway window.
    let base = null;
    try { base = await getTab(false); } catch { /* no booking window */ }
    const own = base ? null : await chrome.windows.create({ url: "about:blank", focused: false, state: "minimized" });
    if (own) base = own.tabs[0];
    const t = await chrome.tabs.create({ windowId: base.windowId, url: args.url, active: false });
    try {
      await sleep(500);
      await waitLoad(t.id, 20000); // inside the server's 30 s for a peek
      await sleep(2500);
      return { capture: await capture(t.id) };
    } finally {
      chrome.tabs.remove(t.id).catch(() => {});
      if (own) chrome.windows.remove(own.id).catch(() => {});
    }
  }
  const tab = await getTab(false);
  if (op === "capture") return { capture: await capture(tab.id, g) };
  if (op === "show") {
    await chrome.windows.update(tab.windowId, { focused: true, state: "normal" });
    await chrome.tabs.update(tab.id, { active: true });
    return {};
  }
  if (!allowed(tab.url)) throw new Error("The route-master tab isn't on redBus or MakeMyTrip.");
  if (isPayment(tab.url)) throw new Error("This is the payment page: the user takes over here.");
  if (op === "action") {
    const name = String(args.name);
    if (!["select_seats", "choose_points", "decline_addons", "open_bus", "continue"].includes(name))
      throw new Error("Unknown step " + name);
    const raw = await exec(tab.id, (n, a) => globalThis.__rm[n](a), [name, args.args || {}], g);
    const result = typeof raw === "string" ? JSON.parse(raw) : raw;
    let click = null;
    if (args.click && result && result.marked) {
      click = name === "open_bus" ? await openBus(tab.id, g) : { clicked: await realClick(tab.id, { g }) };
      await settle(tab.id);
    } else await sleep(800);
    return { result, click, capture: await capture(tab.id, g) };
  }
  if (op === "fill") {
    const result = await exec(tab.id, f => globalThis.__rmForms.fill(f), [args.fields || []], g);
    if (result && result.error) throw new Error(result.error);
    await sleep(600);
    return { ...result, capture: await capture(tab.id, g) };
  }
  if (op === "choose") {
    const result = await exec(tab.id, (t, n) => globalThis.__rmForms.choose(t, n),
      [String(args.text || ""), Number(args.occurrence) || 1], g);
    if (!result || result.error) throw new Error((result && result.error) || "couldn't choose that option");
    await realClick(tab.id, { g });
    await sleep(800);
    return { result, capture: await capture(tab.id, g) };
  }
  throw new Error("Unknown command " + op);
}
