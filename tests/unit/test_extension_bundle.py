"""The extension's page scripts are generated from browser/scripts.py and must stay in sync."""

import json
from pathlib import Path

from mcp_route_master.browser import extension_bundle

EXT = Path(__file__).resolve().parents[2] / "route-master-extension"


def test_committed_actions_js_is_up_to_date():
    assert extension_bundle.OUT == EXT / "page" / "actions.js"
    assert extension_bundle.OUT.read_text(encoding="utf-8") == extension_bundle.build(), (
        "run: python -m mcp_route_master.browser.extension_bundle"
    )


def test_manifest_stays_minimal():
    m = json.loads((EXT / "manifest.json").read_text())
    assert m["manifest_version"] == 3
    hosts = set(m["host_permissions"])
    assert hosts == {
        "https://*.redbus.in/*",
        "https://*.makemytrip.com/*",
        "https://mcp-route-master.onrender.com/*",
    }
    assert "<all_urls>" not in json.dumps(m) and "content_scripts" not in m
    assert m["version"] == __import__("mcp_route_master").__version__
