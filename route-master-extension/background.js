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
    // One command at a time: they all drive the same tab (a late one waits for the previous).
    queue = queue.then(async () => {
      let reply;
      try { reply = { type: "result", id: msg.id, ok: true, result: await run(msg.op, msg.args || {}) }; }
      catch (e) { reply = { type: "result", id: msg.id, ok: false, error: String(e && e.message || e) }; }
      if (sock.readyState === WebSocket.OPEN) sock.send(JSON.stringify(reply));
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

async function settle(tabId) {
  await sleep(1500);
  const tab = await chrome.tabs.get(tabId);
  if (tab.status === "loading") await waitLoad(tabId);
  await sleep(1500);
}

async function exec(tabId, func, args = []) {
  await chrome.scripting.executeScript({ target: { tabId }, files: ["page/actions.js", "page/forms.js"] });
  const [res] = await chrome.scripting.executeScript({ target: { tabId }, func, args });
  return res && res.result;
}

async function capture(tabId) {
  const tab = await chrome.tabs.get(tabId);
  if (!allowed(tab.url)) throw new Error("The route-master tab isn't on redBus or MakeMyTrip.");
  return exec(tabId, () => globalThis.__rm.capture({}));
}

// A real (trusted) mouse click on the element the page script marked: some sites ignore
// clicks made by scripts. Chrome briefly shows "route-master started debugging this browser".
async function realClick(tabId) {
  const point = await exec(tabId, () => {
    const el = document.querySelector("[data-rm-target]");
    if (!el) return null;
    el.scrollIntoView({ block: "center" });
    const r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  });
  if (!point) throw new Error("Nothing to click on this page.");
  const target = { tabId };
  try {
    await chrome.debugger.attach(target, "1.3");
  } catch {
    // DevTools open or debugging refused: fall back to a normal click.
    await exec(tabId, () => document.querySelector("[data-rm-target]")?.click());
    return;
  }
  try {
    const base = { x: point.x, y: point.y, button: "left", clickCount: 1 };
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", { ...base, type: "mouseMoved", button: "none" });
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", { ...base, type: "mousePressed" });
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", { ...base, type: "mouseReleased" });
  } finally {
    await chrome.debugger.detach(target).catch(() => {});
  }
}

async function run(op, args) {
  if (op === "open") {
    if (!allowed(args.url)) throw new Error("Only https redBus and MakeMyTrip pages can be opened.");
    const tab = await getTab(true);
    await chrome.tabs.update(tab.id, { url: args.url });
    await sleep(500);
    await waitLoad(tab.id, 30000);
    await sleep(2500); // the sites render the list after the page loads
    return { capture: await capture(tab.id) };
  }
  if (op === "peek") {
    // Read another page (e.g. tomorrow's buses) in a background tab; the booking tab stays put.
    if (!allowed(args.url)) throw new Error("Only https redBus and MakeMyTrip pages can be opened.");
    const base = await getTab(true);
    const t = await chrome.tabs.create({ windowId: base.windowId, url: args.url, active: false });
    try {
      await sleep(500);
      await waitLoad(t.id, 30000);
      await sleep(2500);
      return { capture: await capture(t.id) };
    } finally {
      chrome.tabs.remove(t.id).catch(() => {});
    }
  }
  const tab = await getTab(false);
  if (op === "capture") return { capture: await capture(tab.id) };
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
    const raw = await exec(tab.id, (n, a) => globalThis.__rm[n](a), [name, args.args || {}]);
    const result = typeof raw === "string" ? JSON.parse(raw) : raw;
    if (args.click && result && result.marked) { await realClick(tab.id); await settle(tab.id); }
    else await sleep(800);
    return { result, capture: await capture(tab.id) };
  }
  if (op === "fill") {
    const result = await exec(tab.id, f => globalThis.__rmForms.fill(f), [args.fields || []]);
    if (result && result.error) throw new Error(result.error);
    await sleep(600);
    return { ...result, capture: await capture(tab.id) };
  }
  if (op === "choose") {
    const result = await exec(tab.id, (t, n) => globalThis.__rmForms.choose(t, n),
      [String(args.text || ""), Number(args.occurrence) || 1]);
    if (!result || result.error) throw new Error((result && result.error) || "couldn't choose that option");
    await realClick(tab.id);
    await sleep(800);
    return { result, capture: await capture(tab.id) };
  }
  throw new Error("Unknown command " + op);
}
