# Host route-master on Render

route-master runs as a remote MCP server over **Streamable HTTP** on a single endpoint, `/mcp`
(the older SSE transport is not used). Render builds the `Dockerfile` and runs it; the server reads
`PORT` from Render and listens on `0.0.0.0`. `/health` is the health check.

## 1. Push the repo to GitHub
The repo root must contain `Dockerfile`, `render.yaml`, `requirements.txt`, `pyproject.toml` and `src/`.

## 2. Create the service from the Blueprint
1. Sign in at <https://dashboard.render.com> with GitHub.
2. **New → Blueprint**, pick the `mcp-route-master` repo, branch `main`.
3. Render reads `render.yaml`: one free Docker web service in Singapore, health check `/health`,
   auto-deploy on every push. Click **Apply**.

(Without the Blueprint: **New → Web Service**, pick the repo, Language **Docker**, Instance type
**Free**, Region **Singapore**, then under Advanced set Health Check Path to `/health`.)

## 3. Wait for the first deploy
The log ends with Uvicorn listening on `0.0.0.0:<PORT>` and Render marks the service **Live**.
Your URL is `https://<service-name>.onrender.com`.

## 4. Check it
```bash
curl https://<service-name>.onrender.com/health
# {"status":"ok","name":"mcp-route-master","version":"..."}

npx @modelcontextprotocol/inspector
# Transport: Streamable HTTP, URL: https://<service-name>.onrender.com/mcp → Connect → List Tools (12)
```

## 5. Connect your AI app to `https://<service-name>.onrender.com/mcp`
- **claude.ai:** Settings → Connectors → Add custom connector.
- **ChatGPT:** developer mode → create an app/connector, authentication **None** (see `chatgpt.md`).
- **Gemini CLI:** `examples/gemini_cli_remote.json` (`httpUrl`).
- **Gemini app:** Settings → Connected apps → Custom apps (see `gemini.md`).

## Settings (Render → service → Environment)
| Variable | What it does |
|---|---|
| `MCP_AUTH_TOKEN` | Optional, 16+ characters. Clients must send `Authorization: Bearer <token>`. Fine for Gemini CLI, Claude Code and scripts; ChatGPT and the Gemini app can't send it. |
| `MCP_CORS_ORIGINS` | Optional. Origins of browser-based clients, comma-separated. |
| `MCP_PATH` | Optional. Serve MCP on another path (e.g. a hard-to-guess one) instead of `/mcp`. |
| `LOG_LEVEL` | `INFO` by default in `render.yaml`. |

Don't set `MCP_ALLOWED_HOSTS` on Render: the server is public and Render forwards the
`*.onrender.com` host itself.

## Good to know
- **Free plan sleeps** after about 15 minutes without traffic; the next request wakes it, which takes
  about a minute. AI apps may time out on that first call; just try again. A paid instance doesn't sleep.
- The server is stateless (`stateless_http`) and keeps nothing between requests except the local
  "unknown place names" review list in `/data`, which a free instance loses on each deploy. That's fine.
- Every push to `main` redeploys. Roll back from Render → service → **Events**.
