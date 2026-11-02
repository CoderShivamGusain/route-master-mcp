# Google Cloud Run

```bash
gcloud run deploy mcp-route-master --source . --region asia-south1 \
  --allow-unauthenticated --port 8000 \
  --set-env-vars MCP_ALLOWED_HOSTS=<service-host>.run.app
```
Cloud Run sets `$PORT`; the server listens on it. Use the `https://…run.app/mcp` URL in ChatGPT,
Gemini or claude.ai.
