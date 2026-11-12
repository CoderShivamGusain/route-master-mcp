# Use route-master in Gemini

| Gemini surface | How to connect |
|---|---|
| **Gemini CLI** (free, any country) | Local: `examples/gemini_settings.json`. Remote: `examples/gemini_cli_remote.json` |
| **Gemini app** (gemini.google.com) | Settings → Connected apps → Custom apps → Add a custom app → paste `https://<your-host>/mcp`. Needs a public HTTPS server; availability is limited (English, personal accounts, some regions) |
| **Gemini API / managed agents** | Pass the remote URL as an MCP server tool; send `Authorization: Bearer <MCP_AUTH_TOKEN>` as a custom header if you set a token |

Tool schemas are written for Gemini's stricter checks (no `anyOf`, `null` or `$ref`).

## Gemini with the Chrome extension (it books in your browser)
With the [route-master extension](../route-master-extension/README.md) signed in, Gemini can open
the buses, the seat map and the form in your own Chrome, just as Claude and ChatGPT do. Connect
Gemini to the **signed-in** address and sign in with the same Google account as the extension:

```
https://mcp-route-master.onrender.com/app/mcp
```

- **Gemini app:** Settings → Connected apps → Custom apps → Add a custom app → paste the address
  above → sign in with Google when asked.
- **Gemini CLI:** `gemini mcp add --transport http route-master https://mcp-route-master.onrender.com/app/mcp`,
  then `/mcp auth route-master` to sign in.

Remove or turn off any older route-master app that points at `/mcp`: with both connected, Gemini
may pick the one that can't use your browser.

Then ask: *"Buses from Dehradun to Gurugram around 9 am today."* If Gemini says the extension
isn't connected, open Chrome and check the extension popup shows **Connected**.

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

## How it behaves in Gemini without the extension
On the open `/mcp` address Gemini can't click in your browser, so it uses the "you click" mode:
dated links → you paste the results page → `rank_pasted_results` → `booking_steps` with
`for_traveller` → you book and pay on the site. The `find_bus`, `book_bus` and `check_scam`
prompts appear as slash commands in Gemini CLI.
