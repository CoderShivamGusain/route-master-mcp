"""User-writable data: local location overrides and a review queue of unknown places.

Bundled data is read-only (uvx runs from a disposable cache), so anything we
write goes to the per-user data directory. Unknown place names are only
*queued* for review; they never change how places resolve until someone adds
them, with checked provider IDs, to `locations.json`.

Why not learn automatically: a resolved place feeds straight into booking-link
IDs (`places/links.py`), and an unchecked ID silently opens the wrong search. So the
server only counts which names it failed to resolve; a person decides what to add.

Environment variables:
  * MCP_ROUTE_MASTER_DATA_DIR - use this folder instead of the platform's
    per-user data directory (handy for tests).
  * MCP_ROUTE_MASTER_NO_LEARN=1 - don't record unknown place names at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from platformdirs import user_data_dir

log = logging.getLogger(__name__)

APP_NAME = "mcp-route-master"
OVERRIDES_FILE = "locations.json"
PENDING_FILE = "pending_locations.json"
MAX_PENDING = 500  # cap on distinct queued names, so the file can't grow without bound
# Anything except letters/digits (any script) and simple punctuation is stripped before a
# name is stored, so control characters or markup never reach the review file.
_SAFE_NAME = re.compile(r"[^\w .,'()-]+", re.UNICODE)


def data_dir() -> Path:
    """Folder for user-writable files (overrides and the review queue)."""
    custom = os.environ.get("MCP_ROUTE_MASTER_DATA_DIR")
    return Path(custom) if custom else Path(user_data_dir(APP_NAME, appauthor=False))


def learning_enabled() -> bool:
    """False when MCP_ROUTE_MASTER_NO_LEARN is set to 1/true/yes."""
    return os.environ.get("MCP_ROUTE_MASTER_NO_LEARN", "").strip().lower() not in {"1", "true", "yes"}


def load_overrides() -> dict[str, Any]:
    """Read the optional local overrides file. Bad files are ignored with a warning."""
    path = data_dir() / OVERRIDES_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("top level must be an object")
        return data
    except (OSError, ValueError) as exc:
        log.warning("Ignoring %s: %s", path, exc)
        return {}


def _write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON so readers only ever see the old file or the complete new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True)
    # Unique temp file in the same folder, then an atomic rename: a crash or a
    # second server process can never leave a half-written JSON file behind.
    fd, tmp = tempfile.mkstemp(prefix=path.stem, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class PendingLocations:
    """Counts unresolved place names so a maintainer can review and add them.

    The asyncio lock serialises writes within one server process; the atomic
    rename in `_write_json_atomic` protects the file across processes (a
    concurrent update may be lost, which is acceptable for review counts).
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def record(self, raw_name: str) -> None:
        """Add one to the count for `raw_name`. A failed write is logged, not raised."""
        if not learning_enabled():
            return
        name = _SAFE_NAME.sub("", raw_name).strip()[:60]
        if len(name) < 2:
            return
        async with self._lock:
            # File I/O runs in a worker thread so it doesn't block the event loop.
            try:
                await asyncio.to_thread(self._record_sync, name)
            except (OSError, ValueError, TypeError) as exc:  # never fail a request over the review queue
                log.warning("Could not record unknown location %r: %s", name, exc)

    @staticmethod
    def _record_sync(name: str) -> None:
        """Read-modify-write of the counts file; a corrupt file is started afresh."""
        path = data_dir() / PENDING_FILE
        try:
            counts = json.loads(path.read_text("utf-8")) if path.is_file() else {}
            if not isinstance(counts, dict):
                counts = {}
        except ValueError:
            counts = {}
        key = name.lower()
        if key not in counts and len(counts) >= MAX_PENDING:
            return
        previous = counts.get(key, 0)
        counts[key] = (previous if isinstance(previous, int) else 0) + 1
        _write_json_atomic(path, counts)
