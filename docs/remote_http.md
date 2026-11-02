# Remote (HTTP) setup

Needed for ChatGPT, claude.ai (web/mobile), the Gemini app and any remote client.

1. Deploy with the Dockerfile on any host that gives you HTTPS (templates in `deploy/`).
2. Set `MCP_ALLOWED_HOSTS=your-host.example.com` (protects against DNS rebinding).
3. Optional: `MCP_AUTH_TOKEN=<32+ random characters>` to require `Authorization: Bearer …`
   (for Gemini CLI/API, Claude Code, scripts; ChatGPT and the Gemini app can't send it).
   Optional: `MCP_CORS_ORIGINS=https://app.example.com` for browser-based clients.
4. Check `https://your-host.example.com/health` returns `{"status":"ok"}`.
5. Add `https://your-host.example.com/mcp`:
   - **ChatGPT:** see `chatgpt.md`.
   - **Gemini:** see `gemini.md`.
   - **claude.ai:** Settings → Connectors → Add custom connector.

Test it first with MCP Inspector: `npx @modelcontextprotocol/inspector`, choose
"Streamable HTTP", enter the URL (and the Bearer header if you set a token).
