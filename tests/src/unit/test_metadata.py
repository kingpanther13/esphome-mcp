"""Metadata checks for the custom component scaffold."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
COMPONENT = ROOT / "custom_components" / "esphome_mcp"


def test_manifest_is_hacs_ready() -> None:
    """Manifest has the expected custom-component identity."""
    manifest = json.loads((COMPONENT / "manifest.json").read_text())

    assert manifest["domain"] == "esphome_mcp"
    assert manifest["config_flow"] is True
    assert "webhook" in manifest["dependencies"]
    assert "fastmcp==3.4.2" in manifest["requirements"]
    assert manifest["version"] == "0.1.0"


def test_hacs_metadata_exists() -> None:
    """HACS metadata is present at the repository root and component level."""
    root_hacs = json.loads((ROOT / "hacs.json").read_text())
    component_hacs = json.loads((COMPONENT / "hacs.json").read_text())

    assert root_hacs["name"] == "ESPHome MCP"
    assert component_hacs["name"] == "ESPHome MCP"


def test_server_defaults_are_scaffolded() -> None:
    """The scaffold uses the requested port and tool prefix."""
    const = (COMPONENT / "const.py").read_text()
    server = (COMPONENT / "server.py").read_text()

    assert "DEFAULT_SERVER_PORT = 9590" in const
    assert 'name="esp_overview"' in server
    assert 'name="esp_list_devices"' in server
    assert 'name="esp_list_entities"' in server
