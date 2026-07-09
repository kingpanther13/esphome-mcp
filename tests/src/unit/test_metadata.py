"""Metadata checks for the custom component scaffold."""

from __future__ import annotations

import json
from importlib import util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
COMPONENT = ROOT / "custom_components" / "esphome_mcp"
RELEASE_METADATA_SPEC = util.spec_from_file_location(
    "validate_release_metadata",
    ROOT / "scripts" / "validate_release_metadata.py",
)
assert RELEASE_METADATA_SPEC is not None
assert RELEASE_METADATA_SPEC.loader is not None
release_metadata = util.module_from_spec(RELEASE_METADATA_SPEC)
RELEASE_METADATA_SPEC.loader.exec_module(release_metadata)
validate_release_metadata = release_metadata.validate_release_metadata


def test_manifest_is_hacs_ready() -> None:
    """Manifest has the expected custom-component identity."""
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    pyproject = (ROOT / "pyproject.toml").read_text()
    const = (COMPONENT / "const.py").read_text()

    assert manifest["domain"] == "esphome_mcp"
    assert manifest["config_flow"] is True
    assert "hassio" in manifest["after_dependencies"]
    assert "webhook" in manifest["dependencies"]
    assert "fastmcp==3.4.2" not in manifest.get("requirements", [])
    assert manifest["version"] == "0.1.0"
    assert 'version = "0.1.0"' in pyproject
    assert 'VERSION = "0.1.0"' in const


def test_hacs_metadata_exists() -> None:
    """HACS metadata is present at the repository root only."""
    root_hacs = json.loads((ROOT / "hacs.json").read_text())

    assert root_hacs["name"] == "ESPHome MCP"
    assert root_hacs["homeassistant"] == "2025.9.1"
    assert root_hacs["hide_default_branch"] is True
    assert root_hacs["render_readme"] is True
    assert "zip_release" not in root_hacs
    assert "filename" not in root_hacs
    assert not (COMPONENT / "hacs.json").exists()


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


def test_readme_has_hacs_facing_usage_information() -> None:
    """README includes the information HACS renders for custom repositories."""
    variants = ("README.md", "readme.md", "readme.MD", "README.MD", "README", "readme")
    info_files = [filename for filename in variants if (ROOT / filename).is_file()]
    assert info_files == ["README.md"]

    readme = (ROOT / info_files[0]).read_text()

    assert "## What You Get" in readme
    assert "## Requirements" in readme
    assert "## HACS Installation" in readme
    assert "https://github.com/kingpanther13/esphome-mcp" in readme
    assert "## Connecting An MCP Client" in readme
    assert "## Tools" in readme
    assert "esp_dashboard_devices" in readme
    assert "esp_manage_addon" in readme
    assert "## Safety Notes" in readme
    assert "latest published **ESPHome MCP** release" in readme
    assert "seven-character commit version" in readme


def test_release_metadata_validation_accepts_manifest_version() -> None:
    """Release publishing must use a real version tag, not a short commit."""
    assert validate_release_metadata("v0.1.0") == []


@pytest.mark.parametrize("version", ["99cdab0", "v0.1.0rc", "v0.1.1"])
def test_release_metadata_validation_rejects_bad_versions(version: str) -> None:
    """The release guard rejects the short-commit path that broke HACS installs."""
    errors = validate_release_metadata(version)

    assert errors


def test_release_workflow_creates_a_github_release() -> None:
    """The release workflow publishes a tag-backed release for HACS to install."""
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()

    assert "workflow_dispatch:" in workflow
    assert "scripts/validate_release_metadata.py" in workflow
    assert "contents: read" in workflow
    assert "needs: validate" in workflow
    assert "contents: write" in workflow
    assert "GH_REPO: ${{ github.repository }}" in workflow
    assert "VERSION: ${{ inputs.version }}" in workflow
    assert 'python scripts/validate_release_metadata.py "$VERSION"' in workflow
    assert workflow.count("uses: actions/checkout@v7") == 1
    assert not any(
        "${{ inputs.version }}" in line
        for line in workflow.splitlines()
        if line.strip().startswith("run:")
    )
    assert "gh release create" in workflow
    assert '--target "${GITHUB_SHA}"' in workflow
    assert 'tag="v${version}"' in workflow


def test_hacs_release_archive_contains_component_payload() -> None:
    """A tag source archive contains every runtime file HACS needs to extract."""
    required_files = {
        "__init__.py",
        "addon_tools.py",
        "brand/icon.png",
        "config_flow.py",
        "const.py",
        "embedded_entry.py",
        "embedded_server.py",
        "embedded_setup.py",
        "manifest.json",
        "mcp_webhook.py",
        "server.py",
        "strings.json",
        "translations/en.json",
        "ui_panel.py",
    }

    component_files = {
        path.relative_to(COMPONENT).as_posix()
        for path in COMPONENT.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }

    assert required_files <= component_files
