# Use route-master in Gemini

| Gemini surface | How to connect |
|---|---|
| **Gemini CLI** (free, any country) | Local: `examples/gemini_settings.json`. Remote: `examples/gemini_cli_remote.json` |
| **Gemini app** (gemini.google.com) | Settings → Connected apps → Custom apps → Add a custom app → paste `https://<your-host>/mcp`. Needs a public HTTPS server; availability is limited (English, personal accounts, some regions) |
| **Gemini API / managed agents** | Pass the remote URL as an MCP server tool; send `Authorization: Bearer <MCP_AUTH_TOKEN>` as a custom header if you set a token |

Tool schemas are written for Gemini's stricter checks (no `anyOf`, `null` or `$ref`).

## Gemini CLI, local (recommended)
Copy `examples/gemini_settings.json` into `~/.gemini/settings.json`
(Windows: `%USERPROFILE%\.gemini\settings.json`), fix the folder path, then run `gemini` and
type `/mcp` to see route-master's tools.

## Gemini CLI, remote
```json
{
  "mcpServers": {
    "route-master": {
      "httpUrl": "https://<your-host>/mcp",
      "headers": { "Authorization": "Bearer ${ROUTE_MASTER_TOKEN}" },
      "timeout": 30000
    }
  }
}
```
Drop `headers` if the server has no `MCP_AUTH_TOKEN`.

## How it behaves in Gemini
Gemini can't click in your browser either, so it uses the same "you click" mode as ChatGPT:
dated links → you paste the results page → `rank_pasted_results` → `booking_steps` with
`for_traveller` → you book and pay on the site. The `find_bus`, `book_bus` and `check_scam`
prompts appear as slash commands in Gemini CLI.
