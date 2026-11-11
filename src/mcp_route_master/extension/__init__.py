"""The route-master Chrome extension's server side (signed-in mode, served at /app/mcp).

The open server (/mcp) never touches a browser: the AI host runs the page scripts. In signed-in
mode the user's own route-master extension does that work instead, so any AI app can book:

- tokens.py: checks the Google sign-in sent by the extension and gives it its own long-lived token.
- relay.py:  keeps one live WebSocket per signed-in user and sends commands to their extension.
- app.py:    the signed-in MCP server (Google sign-in, browser_* tools) and the combined web app.

The user is matched by Google account: the AI app's connector sign-in and the extension sign-in
carry the same Google account ID ("sub"), so no pairing codes are needed.
"""
