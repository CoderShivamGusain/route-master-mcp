"""Shared test setup: isolate the data folder and make tests/fixtures importable."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Never touch the real user data folder during tests."""
    monkeypatch.setenv("MCP_ROUTE_MASTER_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


@pytest.fixture
def anyio_backend():
    return "asyncio"
