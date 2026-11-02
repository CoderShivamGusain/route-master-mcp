# Remote MCP server (Streamable HTTP at /mcp, health check at /health).
# Render, Fly.io and Cloud Run set PORT; the server listens on 0.0.0.0:$PORT.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MCP_TRANSPORT=http HOST=0.0.0.0 PORT=8000 \
    MCP_ROUTE_MASTER_DATA_DIR=/data

WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps . \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /data && chown app /data

USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health', timeout=2)"
CMD ["mcp-route-master"]
