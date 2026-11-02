// Runs route-master's browser scripts against small mock pages in headless Chromium.
// Usage: node run_page_scripts.js <scripts.json>   (written by tests/browser/test_page_scripts.py)
// Prints one JSON object: { caseName: result, ... }. Needs Node's `playwright` package.
const fs = require("fs");
const { chromium } = require("playwright");

const seat = (n, deck, kind, price) =>
  `<button class="seat" aria-label="Seat number ${n}, ${deck} deck, seat type ${kind}, berth unreserved, ` +
  `price ${price} rupees, availability available, currently unselected. Press space or enter to select this seat."` +
  ` onclick="this.setAttribute('aria-label', this.getAttribute('aria-label').replace('currently unselected', 'currently selected'))">${n}</button>`;

const PAGES = {
  redbus_seats: `<div>${seat("3U", "upper", "sleeper", 3499)}${seat("4L", "lower", "sleeper", 3999)}${seat("1", "lower", "seater", 2199)}</div>`,
  redbus_points: `<div>
      <label><input type="radio" name="bp_0"><span>17:00</span><span>Garhwal Parking</span></label>
      <label><input type="radio" name="bp_1"><span>17:25</span><span>Mall Of Dehradun</span></label>
      <label><input type="radio" name="dp_0"><span>11:30</span><span>Mudaila (Sardar Petrol Pump)</span></label></div>`,
  mmt_points: `<div><p>PICK UP POINT</p><ul>
      <li onclick="this.classList.add('selected')">20:30, 2 NOV Brahman Wala Chowk</li>
      <li onclick="this.classList.add('selected')">20:50, 2 NOV Isbt in front of mall of dehradun gate</li></ul></div>
    <div><p>DROP POINT</p><ul><li onclick="this.classList.add('selected')">08:00, 3 NOV Agra Radhika Travels</li></ul></div>`,
  addons: `<div>
      <label><input type="radio" name="tg" checked><span>Add Trip Guarantee</span></label>
      <label><input type="radio" name="tg"><span>Don’t add Trip Guarantee</span></label>
      <label><input type="radio" name="ins"><span>Add Travel Insurance</span></label>
      <label><input type="radio" name="ins"><span>Don’t add Travel Insurance</span></label>
      <div class="travelInsuranceWrapper"><label><input type="checkbox" checked></label></div></div>`,
  continue_rb: `<div><p>Amount ₹3,499</p><button type="button" aria-label="Continue booking, Amount ₹3,499">Continue booking</button></div>`,
  results_rb: `<ul>
      <li class="tupleWrapper___a1"><div>20:00</div><div>08:00</div><div>36 Seats</div><div>₹1,600</div><div>Onwards</div><div>HPTDC</div></li>
      <li class="tupleWrapper___a1"><div>22:25</div><div>09:55</div><div>41 Seats</div><div>₹797</div><div>Onwards</div><div>ReoBus Pro</div><div>Volvo</div></li>
    </ul>`,
};

(async () => {
  const scripts = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
  const browser = await chromium.launch();
  const out = {};
  for (const [name, { page: pageName, html, url, js }] of Object.entries(scripts)) {
    const page = await browser.newPage();
    const body = html !== undefined ? html : PAGES[pageName];  // a case can bring its own HTML
    await page.route("**/*", route =>
      route.fulfill({ contentType: "text/html", body: `<!doctype html><meta charset="utf-8"><body>${body}</body>` }));
    await page.goto(url);
    try {
      const result = await page.evaluate(js);
      const marked = await page.evaluate(() => {
        const m = document.querySelector('[aria-label="rm-target"]');
        return m ? m.textContent.trim() : null;
      });
      const markedHtml = await page.evaluate(() => {
        const m = document.querySelector('[aria-label="rm-target"]');
        const card = m && m.closest("li, .card");
        return card ? card.getAttribute("data-name") : null;
      });
      out[name] = { result: typeof result === "string" ? JSON.parse(result) : result, marked, card: markedHtml };
    } catch (e) {
      out[name] = { error: String(e) };
    }
    await page.close();
  }
  await browser.close();
  console.log(JSON.stringify(out));
})();
