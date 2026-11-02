# Use route-master in ChatGPT

ChatGPT connects only to a **public HTTPS** MCP server (it runs in OpenAI's cloud, so it can't start
a program on your computer). Custom MCP apps need **developer mode**, available on paid plans
(Plus, Pro, Business, Enterprise, Edu); the setting's location varies by ChatGPT version
(Settings → Apps / Connectors → Advanced, or Settings → Security).

## 1. Get an HTTPS URL

**Quick test from your computer (temporary URL):**
```bash
uvx --from . mcp-route-master --transport http --port 8000
# in a second terminal (Cloudflare's free tunnel, no account needed):
cloudflared tunnel --url http://127.0.0.1:8000
# → https://<random-name>.trycloudflare.com
```
Start the server with the tunnel's host allowed: `MCP_ALLOWED_HOSTS=<random-name>.trycloudflare.com`.

**Permanent:** deploy with the Dockerfile (Render: `render.yaml` + `docs/render.md`; `deploy/` for Fly.io and Google Cloud Run).
Check `https://<your-host>/health` returns `{"status":"ok"}`.

## 2. Add it in ChatGPT

1. Turn on developer mode.
2. Create an app/connector: name `route-master`, MCP URL `https://<your-host>/mcp`,
   authentication **None** (all tools are read-only and handle no accounts).
3. In a chat, enable route-master from the tools/apps menu and ask, e.g.
   *"Bus from Dehradun to Varanasi tomorrow night, sleeper"*.

Leave `MCP_AUTH_TOKEN` unset for ChatGPT (it can't send a fixed token). For light protection, use a
hard-to-guess path instead: `--path /mcp-7f3k9q` and add `https://<your-host>/mcp-7f3k9q`.

## How it behaves in ChatGPT

ChatGPT can't click in your own browser, so route-master switches to "you click" mode:

1. `plan_bus_trip` gives the dated redBus / MakeMyTrip links.
2. Open one, **select all and copy** the results page, paste it into the chat →
   `rank_pasted_results` ranks the buses.
3. `check_operator`, then `booking_steps` with `for_traveller` → short steps you follow on the site.
4. Paste any link, SMS or WhatsApp message → `check_booking_safety`.

`search` and `fetch` let ChatGPT's knowledge features (e.g. deep research) find routes, the
pay-safely checklist and government operators.
