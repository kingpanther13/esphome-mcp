"""Hermetic checks for the HAOS E2E harness scaffold."""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[3]
BUILD_IMAGE_PATH = ROOT / "tests" / "haos_image_build" / "build_image.py"
HAOS_RUNTIME_PATH = ROOT / "tests" / "src" / "haos_runtime.py"
STREAMABLE_HTTP_PATH = ROOT / "tests" / "src" / "e2e" / "utilities" / "streamable_http.py"
EMBEDDED_E2E_PATH = ROOT / "tests" / "src" / "e2e" / "haos_only" / "test_embedded_server_haos.py"


def _load_module(name: str, path: Path) -> ModuleType:
    """Load one harness module by file path without importing the test package."""
    existing = sys.modules.get(name)
    if existing is not None:
        return existing

    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def test_haos_builder_and_runtime_constants_stay_in_sync() -> None:
    """The image builder and runtime agree on the baked component endpoint."""
    build_image = _load_module("esphome_mcp_test_build_image", BUILD_IMAGE_PATH)
    haos_runtime = _load_module("esphome_mcp_test_haos_runtime", HAOS_RUNTIME_PATH)

    assert build_image.ESPHOME_MCP_ENTRY_ID == haos_runtime.ESPHOME_MCP_SERVER_ENTRY_ID
    assert build_image.ESPHOME_MCP_WEBHOOK_ID == haos_runtime.ESPHOME_MCP_SERVER_WEBHOOK_ID
    assert build_image.ESPHOME_MCP_SECRET_PATH == haos_runtime.ESPHOME_MCP_SERVER_SECRET_PATH
    assert build_image.ESPHOME_MCP_PORT == haos_runtime.ESPHOME_MCP_SERVER_PORT == 9590
    assert build_image.ESPHOME_FIXTURE_DEVICE_ID == haos_runtime.ESPHOME_FIXTURE_DEVICE_ID
    assert build_image.ESPHOME_FIXTURE_ENTITY_ID == haos_runtime.ESPHOME_FIXTURE_ENTITY_ID


def test_build_image_injects_disabled_esphome_mcp_entry(tmp_path: Path) -> None:
    """The baked HAOS storage entry is present but inert until the test enables it."""
    build_image = _load_module("esphome_mcp_test_build_image", BUILD_IMAGE_PATH)
    config_dir = tmp_path / "homeassistant"
    storage_dir = config_dir / ".storage"
    storage_dir.mkdir(parents=True)
    config_entries_path = storage_dir / "core.config_entries"
    config_entries_path.write_text(
        json.dumps(
            {
                "version": 1,
                "minor_version": 1,
                "key": "core.config_entries",
                "data": {"entries": []},
            }
        )
    )

    build_image._inject_esphome_mcp_entry(config_dir)
    build_image._inject_esphome_mcp_entry(config_dir)

    stored = json.loads(config_entries_path.read_text())
    entries = stored["data"]["entries"]
    matching = [
        entry for entry in entries if entry.get("entry_id") == build_image.ESPHOME_MCP_ENTRY_ID
    ]

    assert len(matching) == 1
    entry = matching[0]
    assert entry["disabled_by"] == "user"
    assert entry["domain"] == "esphome_mcp"
    assert entry["unique_id"] == "esphome_mcp-server"
    assert entry["data"] == {
        "webhook_id": build_image.ESPHOME_MCP_WEBHOOK_ID,
        "secret_path": build_image.ESPHOME_MCP_SECRET_PATH,
    }
    assert entry["options"] == {
        "server_port": 9590,
        "bind_host": "127.0.0.1",
        "webhook_auth": "none",
        "enable_webhook": True,
        "pip_spec": "fastmcp==3.4.2",
    }


def test_build_image_installs_official_esphome_device_builder_before_bake() -> None:
    """The HAOS image builder installs ESPHome Device Builder before component bake."""
    build_image = _load_module("esphome_mcp_test_build_image", BUILD_IMAGE_PATH)
    source = BUILD_IMAGE_PATH.read_text()

    assert build_image.ESPHOME_DEVICE_BUILDER_ADDON.repo == (
        "https://github.com/esphome/home-assistant-addon"
    )
    assert build_image.ESPHOME_DEVICE_BUILDER_ADDON.name == "ESPHome Device Builder"
    assert build_image.ESPHOME_DEVICE_BUILDER_ADDON.start is True
    assert source.index("install_esphome_device_builder(ws)") < source.index(
        "bake_component_into_config(qcow2)"
    )


def test_build_image_removes_runtime_artifacts_before_bake(tmp_path: Path) -> None:
    """The reusable HAOS image must not keep lock files from the first boot."""
    build_image = _load_module("esphome_mcp_test_build_image", BUILD_IMAGE_PATH)
    config_dir = tmp_path / "homeassistant"
    config_dir.mkdir()
    for name in (".ha_run.lock", ".HA_RESTORE"):
        (config_dir / name).write_text("stale")
    keep = config_dir / ".HA_VERSION"
    keep.write_text("2026.7.1")

    build_image._remove_runtime_artifacts(config_dir)

    assert not (config_dir / ".ha_run.lock").exists()
    assert not (config_dir / ".HA_RESTORE").exists()
    assert keep.read_text() == "2026.7.1"


def test_build_image_injects_esphome_registry_fixtures(tmp_path: Path) -> None:
    """The baked HAOS image has searchable ESPHome registry data."""
    build_image = _load_module("esphome_mcp_test_build_image", BUILD_IMAGE_PATH)
    config_dir = tmp_path / "homeassistant"
    storage_dir = config_dir / ".storage"
    storage_dir.mkdir(parents=True)
    device_path = storage_dir / "core.device_registry"
    entity_path = storage_dir / "core.entity_registry"
    device_path.write_text(
        json.dumps(
            {
                "version": 1,
                "minor_version": 12,
                "key": "core.device_registry",
                "data": {"devices": []},
            }
        )
    )
    entity_path.write_text(
        json.dumps(
            {
                "version": 1,
                "minor_version": 19,
                "key": "core.entity_registry",
                "data": {"entities": []},
            }
        )
    )

    build_image._inject_esphome_registry_fixtures(config_dir)
    build_image._inject_esphome_registry_fixtures(config_dir)

    devices = json.loads(device_path.read_text())["data"]["devices"]
    entities = json.loads(entity_path.read_text())["data"]["entities"]
    matching_devices = [
        device for device in devices if device.get("id") == build_image.ESPHOME_FIXTURE_DEVICE_ID
    ]
    matching_entities = [
        entity
        for entity in entities
        if entity.get("entity_id") == build_image.ESPHOME_FIXTURE_ENTITY_ID
    ]

    assert len(matching_devices) == 1
    device = matching_devices[0]
    assert device["area_id"] == "kitchen"
    assert device["identifiers"] == [["esphome", build_image.ESPHOME_FIXTURE_NODE_ID]]
    assert device["name_by_user"] == "Kitchen ESPHome"

    assert len(matching_entities) == 1
    entity = matching_entities[0]
    assert entity["device_id"] == build_image.ESPHOME_FIXTURE_DEVICE_ID
    assert entity["platform"] == "esphome"
    assert entity["original_device_class"] == "temperature"


def test_build_image_defers_server_requirement_install_to_entry_enable() -> None:
    """The bake must not copy runner-built wheels into HAOS config deps."""
    source = BUILD_IMAGE_PATH.read_text()
    manifest = json.loads(
        (ROOT / "custom_components" / "esphome_mcp" / "manifest.json").read_text()
    )

    assert "fastmcp==3.4.2" not in manifest.get("requirements", [])
    assert "_preinstall_component_requirements" not in source
    assert "/site-packages" not in source
    assert "--target" not in source


def test_build_image_detects_store_only_addon_metadata_as_not_installed() -> None:
    """Supervisor can return add-on info with state=unknown before install."""
    build_image = _load_module("esphome_mcp_test_build_image", BUILD_IMAGE_PATH)

    assert build_image._addon_is_installed(None) is False
    assert build_image._addon_is_installed({"state": "unknown", "options": {}}) is False
    assert build_image._addon_is_installed({"state": "started", "installed": False}) is False
    assert build_image._addon_is_installed({"state": "started", "options": {}}) is True


def test_streamable_http_parser_handles_json_and_multiline_sse() -> None:
    """Streamable HTTP parsing accepts JSON bodies and skips non-result SSE events."""
    streamable_http = _load_module("esphome_mcp_test_streamable_http", STREAMABLE_HTTP_PATH)

    assert streamable_http.parse_mcp_response(
        "application/json",
        b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}',
    ) == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    assert streamable_http.sse_event_payloads("data: first\r\ndata: second\r\n\r\n") == [
        "first\nsecond"
    ]

    sse_body = (
        "event: message\n"
        'data: {"jsonrpc":"2.0","id":1,"method":"notifications/progress"}\n'
        "\n"
        "event: message\r\n"
        'data: {"jsonrpc":"2.0",\r\n'
        'data: "id":2,\r\n'
        'data: "result":{"tools":[]}}\r\n'
        "\r\n"
    )

    assert streamable_http.parse_mcp_response("text/event-stream", sse_body) == {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"tools": []},
    }


def test_embedded_e2e_module_tracks_expected_webhook_and_tool_names() -> None:
    """Static inspection keeps the HAOS test coverage target visible without QEMU."""
    tree = ast.parse(EMBEDDED_E2E_PATH.read_text())
    string_constants = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    expected_tools = {
        "esp_overview",
        "esp_list_devices",
        "esp_list_entities",
        "esp_manage_addon",
        "esp_dashboard_devices",
        "esp_search_yaml",
        "esp_compile_firmware",
    }

    assert expected_tools <= string_constants
    assert "Kitchen ESPHome" in string_constants
    assert "Kitchen ESPHome Temperature" in string_constants
    assert "ESPHOME_MCP_SERVER_WEBHOOK_ID" in EMBEDDED_E2E_PATH.read_text()
