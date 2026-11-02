import asyncio
import json

import pytest

from mcp_route_master.common.store import MAX_PENDING, PendingLocations, load_overrides
from mcp_route_master.places.resolver import Resolver

pytestmark = pytest.mark.anyio


async def test_concurrent_records_keep_valid_json(isolated_data_dir):
    pending = PendingLocations()
    await asyncio.gather(*(pending.record(f"Place {i % 5}") for i in range(50)))
    data = json.loads((isolated_data_dir / "pending_locations.json").read_text())
    assert sum(data.values()) == 50 and len(data) == 5


async def test_names_are_sanitised_and_capped(isolated_data_dir):
    pending = PendingLocations()
    await pending.record("<script>alert(1)</script> Kasol" + "x" * 200)
    key = next(iter(json.loads((isolated_data_dir / "pending_locations.json").read_text())))
    assert "<" not in key and len(key) <= 60


async def test_queue_is_bounded(isolated_data_dir):
    path = isolated_data_dir / "pending_locations.json"
    isolated_data_dir.mkdir(parents=True)
    path.write_text(json.dumps({f"p{i}": 1 for i in range(MAX_PENDING)}))
    await PendingLocations().record("brand new place")
    assert "brand new place" not in json.loads(path.read_text())


async def test_learning_can_be_disabled(isolated_data_dir, monkeypatch):
    monkeypatch.setenv("MCP_ROUTE_MASTER_NO_LEARN", "1")
    await PendingLocations().record("Kasol")
    assert not (isolated_data_dir / "pending_locations.json").exists()


async def test_unwritable_dir_does_not_raise(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("MCP_ROUTE_MASTER_DATA_DIR", str(blocker / "sub"))
    await PendingLocations().record("Kasol")  # must not raise


def test_overrides_file_is_loaded(isolated_data_dir):
    isolated_data_dir.mkdir(parents=True)
    (isolated_data_dir / "locations.json").write_text(
        json.dumps({"hubs": [{"name": "Kasol", "state": "Himachal Pradesh"}]})
    )
    assert Resolver().resolve("Kasol").hub.name == "Kasol"


def test_broken_overrides_file_is_ignored(isolated_data_dir):
    isolated_data_dir.mkdir(parents=True)
    (isolated_data_dir / "locations.json").write_text("{not json")
    assert load_overrides() == {}
    assert Resolver().resolve("Delhi").ok


async def test_corrupt_queue_values_do_not_break_requests(isolated_data_dir):
    isolated_data_dir.mkdir(parents=True)
    path = isolated_data_dir / "pending_locations.json"
    path.write_text(json.dumps({"kasol": "abc"}))
    await PendingLocations().record("Kasol")
    assert json.loads(path.read_text())["kasol"] == 1
