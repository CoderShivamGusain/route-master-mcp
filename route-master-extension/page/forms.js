// Form helpers for the route-master extension (hand-written; actions.js is generated).
// Runs in the extension's isolated world on redbus.in / makemytrip.com pages only.
//   __rmForms.fill([{field, value}])  types the user's own details into matching fields
//   __rmForms.choose(text, n)         marks the n-th option (e.g. "Male", "Don't add ...") for a real click
// Safety: never touches card, UPI, OTP, PIN, password or expiry fields, never acts on payment pages,
// and choose() can't press continue, book, pay or sign-in buttons.
(() => {
  const SECRET = /otp|one.?time|cvv|cvc|card|cc-|\bpin\b|upi|vpa|password|passcode|expiry|exp-/i;
  const BANNED = /\bpay\b|payment|book|continue|proceed|confirm|submit|log ?in|sign ?(in|up)|otp|google|upi|card/i;
  const sq = s => (s || "").replace(/\s+/g, " ").trim();
  const vis = e => !!(e.offsetParent || e.getClientRects().length);
  const onPayment = () => /pay|checkout/i.test(location.pathname) || /^payments?\./i.test(location.hostname);
  const about = e => [e.id, e.name, e.getAttribute("autocomplete"), e.placeholder, e.getAttribute("aria-label")].join(" ");
  const ownLabel = e => [...(e.labels || [])].map(l => l.innerText)
    .concat(e.previousElementSibling ? [e.previousElementSibling.innerText] : []).join(" ");
  const names = e => [e.id, e.name, e.placeholder, e.getAttribute("aria-label"), ownLabel(e)]
    .map(x => sq(x).toLowerCase().replace(/\*/g, "").trim()).filter(Boolean);

  const setValue = (el, value) => {
    el.focus();
    if (el.tagName === "SELECT") {
      const opt = [...el.options].find(o => sq(o.text).toLowerCase() === sq(value).toLowerCase() || o.value === value);
      if (!opt) return false;
      el.value = opt.value;
    } else {
      // The native setter, so React/Vue see the change.
      const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
    }
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.blur();
    return true;
  };

  const fill = fields => {
    if (onPayment()) return { error: "this is the payment page: the user fills it in" };
    const inputs = [...document.querySelectorAll("input,textarea,select")].filter(e => vis(e) &&
      !["hidden", "password", "submit", "button", "radio", "checkbox", "file"].includes(e.type));
    const out = { filled: [], missing: [], refused: [] };
    const used = new Set();
    for (const { field, value } of fields || []) {
      const key = sq(field).toLowerCase();
      if (!key || SECRET.test(key)) { out.refused.push(field); continue; }
      const free = inputs.filter(e => !used.has(e));
      const hit = free.find(e => names(e).includes(key)) || free.find(e => names(e).some(n => n.includes(key)));
      if (!hit) { out.missing.push(field); continue; }
      if (SECRET.test(about(hit)) || SECRET.test(ownLabel(hit))) { out.refused.push(field); continue; }
      if (setValue(hit, String(value))) { used.add(hit); out.filled.push(field); } else out.missing.push(field);
    }
    return out;
  };

  const choose = (text, occurrence = 1) => {
    if (onPayment()) return { error: "this is the payment page: the user decides there" };
    const want = sq(text).toLowerCase();
    if (!want || BANNED.test(want) && !/^don.?t add|^no,?\s*i\s*(don.?t|do not)\s+(need|want)\b/i.test(want))
      return { error: "that option can't be chosen by route-master" };
    const own = n => sq([...n.childNodes].filter(c => c.nodeType === 3).map(c => c.textContent).join(" ")) ||
      (n.children.length === 0 ? sq(n.textContent) : "");
    const hits = [...document.querySelectorAll("label,button,[role=radio],[role=button],[role=option],li,span,div,p")]
      .filter(n => vis(n) && own(n).toLowerCase() === want);
    const found = hits.filter(t => !hits.some(o => o !== t && t.contains(o)));
    if (found.length === 0) return { error: "option '" + text + "' not found on this page" };
    const n = Number(occurrence) || 1;
    if (n > found.length)
      return { error: "option '" + text + "' appears " + found.length + " time(s); there's no number " + n };
    const pick = found[n - 1];
    const target = pick.closest("label,button,[role=radio],[role=button],[role=option],li") || pick;
    document.querySelectorAll("[data-rm-target]").forEach(e => e.removeAttribute("data-rm-target"));
    target.setAttribute("data-rm-target", "1");
    target.scrollIntoView({ block: "center" });
    return { marked: true, option: text };
  };

  globalThis.__rmForms = { fill, choose };
})();
