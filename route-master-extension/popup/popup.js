// Popup: sign in or out, show the connection status, choose how the booking window opens.
const $ = id => document.getElementById(id);
const ask = msg => chrome.runtime.sendMessage(msg);
const LABEL = {
  "signed-out": "Not signed in.",
  connecting: "Connecting…",
  connected: "Connected",
  offline: "Offline: reconnecting (or another Chrome took over).",
};

async function refresh() {
  const s = await ask({ type: "status" });
  if (s.error) return showError(s.error);
  $("status").textContent = s.status === "connected" ? `Connected as ${s.email}` : LABEL[s.status] || s.status;
  $("signin").hidden = !!s.email;
  $("signout").hidden = !s.email;
  $("reconnect").hidden = !(s.email && s.status !== "connected");
  if (s.replaced) $("status").textContent = "Another Chrome signed in to this account is in use.";
  $("window").value = s.windowState;
}

function showError(text) { $("error").textContent = text; $("error").hidden = false; }

$("signin").onclick = async () => {
  $("error").hidden = true;
  const r = await ask({ type: "signin" });
  if (r.error) showError(r.error);
  setTimeout(refresh, 800);
};
$("reconnect").onclick = async () => { await ask({ type: "reconnect" }); setTimeout(refresh, 800); };
$("signout").onclick = async () => { await ask({ type: "signout" }); refresh(); };
$("window").onchange = e => ask({ type: "window", value: e.target.value });

refresh();
setInterval(refresh, 2000);
