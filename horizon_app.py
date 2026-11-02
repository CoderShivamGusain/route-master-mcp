"""Entry point for hosted FastMCP platforms (Prefect Horizon / FastMCP Cloud).

They load a module and look for the server object: use the entrypoint `horizon_app.py:mcp`.
The package lives in src/, so make it importable even when the host doesn't install the project.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from mcp_route_master.server import get_resolver, mcp  # noqa: E402

get_resolver()  # load place data at startup, not inside the first request

__all__ = ["mcp"]
