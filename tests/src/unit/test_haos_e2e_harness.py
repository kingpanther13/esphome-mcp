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


def test_build_image_injects_enabled_esphome_mcp_entry(tmp_path: Path) -> None:
    """The baked HAOS storage entry starts the component with webhook auth disabled."""
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
    assert entry["disabled_by"] is None
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
    }


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
    assert "ESPHOME_MCP_SERVER_WEBHOOK_ID" in EMBEDDED_E2E_PATH.read_text()
