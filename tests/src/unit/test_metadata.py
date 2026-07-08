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
    assert "hassio" in manifest["after_dependencies"]
    assert "webhook" in manifest["dependencies"]
    assert "fastmcp==3.4.2" not in manifest.get("requirements", [])
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
    assert 'DEFAULT_PIP_SPEC = "fastmcp==3.4.2"' in const
    assert 'name="esp_overview"' in server
    assert 'name="esp_list_devices"' in server
    assert 'name="esp_list_entities"' in server
    assert "query: str | None = None" in server
    assert 'name="esp_manage_addon"' in server
    assert 'name="esp_dashboard_devices"' in server
    assert 'name="esp_search_yaml"' in server
    assert 'name="esp_get_yaml"' in server
    assert 'name="esp_update_yaml"' in server
    assert 'name="esp_validate_yaml"' in server
    assert 'name="esp_device_logs"' in server
    assert 'name="esp_compile_firmware"' in server
    assert 'name="esp_install_firmware"' in server
    assert 'name="esp_firmware_jobs"' in server
    assert 'name="esp_get_firmware_job"' in server
    assert 'name="esp_follow_firmware_job"' in server


def test_esphome_addon_tool_contract_is_scaffolded() -> None:
    """The ESPHome add-on tool keeps the intended ha-mcp custom-component ingress shape."""
    addon_tools = (COMPONENT / "addon_tools.py").read_text()

    assert "manage_esphome_addon" in addon_tools
    assert "supervisor.send_command" in addon_tools
    assert "_create_ingress_session" in addon_tools
    assert 'headers["Cookie"] = f"ingress_session=' in addon_tools
    assert "/api/hassio_ingress" in addon_tools
    assert 'path or "/devices"' in addon_tools
    assert 'await _route_for_addon(hass, addon, "ws"' in addon_tools


def test_device_builder_specific_tools_use_current_ws_commands() -> None:
    """Named ESPHome tools target the Device Builder multiplexed API."""
    addon_tools = (COMPONENT / "addon_tools.py").read_text()

    assert "devices/list" in addon_tools
    assert "yaml/search" in addon_tools
    assert "devices/get_config" in addon_tools
    assert "devices/update_config" in addon_tools
    assert "devices/validate" in (COMPONENT / "server.py").read_text()
    assert "devices/logs" in (COMPONENT / "server.py").read_text()
    assert "devices/stop_stream" in addon_tools
    assert "firmware/compile" in (COMPONENT / "server.py").read_text()
    assert "firmware/install" in (COMPONENT / "server.py").read_text()
    assert "firmware/get_jobs" in addon_tools
    assert "firmware/get_job" in addon_tools
    assert "firmware/follow_job" in addon_tools


def test_readme_credits_prior_art() -> None:
    """README credits comparison projects used for protocol scaffolding."""
    readme = (ROOT / "README.md").read_text()

    assert "Prior Art" in readme
    assert "ha-mcp" in readme
    assert "loryanstrant" in readme
    assert "jeeftor" in readme
