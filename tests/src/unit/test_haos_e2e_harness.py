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
INITIAL_TEST_STATE = ROOT / "tests" / "initial_test_state"
E2E_COMPONENT_WORKFLOW = ROOT / ".github" / "workflows" / "e2e-component.yml"


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
        "enable_persistent_notification": True,
    }
    assert "pip_spec" not in entry["options"]


def test_build_image_installs_esphome_and_hacs_before_bake() -> None:
    """The HAOS image has official Device Builder and complete HACS installs."""
    build_image = _load_module("esphome_mcp_test_build_image", BUILD_IMAGE_PATH)
    source = BUILD_IMAGE_PATH.read_text()
    workflow = E2E_COMPONENT_WORKFLOW.read_text()

    assert build_image.HAOS_VERSION == "18.1"
    assert build_image.ESPHOME_DEVICE_BUILDER_ADDON.repo == (
        "https://github.com/esphome/home-assistant-addon"
    )
    assert build_image.ESPHOME_DEVICE_BUILDER_ADDON.name == "ESPHome Device Builder"
    assert build_image.ESPHOME_DEVICE_BUILDER_ADDON.start is True
    assert build_image.GET_HACS_ADDON.repo == "https://github.com/hacs/addons"
    assert build_image.GET_HACS_ADDON.name == "Get HACS"
    assert source.index("install_esphome_device_builder(ws)") < source.index(
        "bake_component_into_config(qcow2)"
    )
    assert source.index("install_hacs(ws, base_url)") < source.index(
        "bake_component_into_config(qcow2)"
    )
    assert 'seed_hacs = cc_dir / "hacs"' in source
    assert "repos/esphome/home-assistant-addon/contents/esphome/config.yaml" in workflow
    assert "repos/hacs/addons/contents/get/config.yaml" in workflow
    assert "repos/hacs/integration/releases/latest" in workflow
    assert "GH_TOKEN: ${{ github.token }}" in workflow
    assert "esphome-addon-hash" in workflow
    assert "hacs-addon-hash" in workflow
    assert "hacs-version" in workflow


def test_install_hacs_uses_supported_addon_and_restarts_core(monkeypatch) -> None:
    """The image bake installs HACS, restarts Core, and reconnects Supervisor."""
    build_image = _load_module("esphome_mcp_test_build_image", BUILD_IMAGE_PATH)
    events: list[tuple[object, ...]] = []

    class FakeWebSocket:
        def supervisor_api(
            self,
            path: str,
            *,
            method: str,
            timeout: float,
        ) -> dict[str, object]:
            events.append(("api", path, method, timeout))
            return {}

        def reconnect(self) -> None:
            events.append(("reconnect",))

    monkeypatch.setattr(
        build_image,
        "_wait_supervisor_ready",
        lambda _ws: events.append(("supervisor-ready",)),
    )
    monkeypatch.setattr(
        build_image,
        "_add_repository",
        lambda _ws, repo: events.append(("add-repository", repo)),
    )
    monkeypatch.setattr(
        build_image,
        "_reload_store",
        lambda _ws: events.append(("reload-store",)),
    )
    monkeypatch.setattr(build_image, "_discover_slug", lambda _ws, _addon: "get_hacs")
    monkeypatch.setattr(build_image, "_addon_info_or_none", lambda _ws, _slug: None)
    monkeypatch.setattr(
        build_image,
        "_install_addon_with_retry",
        lambda _ws, slug, *, timeout: events.append(("install", slug, timeout)),
    )
    monkeypatch.setattr(
        build_image,
        "_wait_http_ok",
        lambda url, *, timeout: events.append(("wait-http", url, timeout)),
    )

    build_image.install_hacs(FakeWebSocket(), "http://127.0.0.1:18123")

    assert events == [
        ("supervisor-ready",),
        ("add-repository", "https://github.com/hacs/addons"),
        ("reload-store",),
        ("install", "get_hacs", 900.0),
        ("api", "/addons/get_hacs/start", "post", 180.0),
        ("api", "/core/restart", "post", 300.0),
        ("wait-http", "http://127.0.0.1:18123/manifest.json", 300.0),
        ("reconnect",),
    ]


def test_build_image_bakes_from_seed_state_instead_of_live_config() -> None:
    """The reusable HAOS config comes from the repo seed, like ha-mcp."""
    source = BUILD_IMAGE_PATH.read_text()
    workflow = E2E_COMPONENT_WORKFLOW.read_text()

    assert INITIAL_TEST_STATE.is_dir()
    assert (INITIAL_TEST_STATE / ".storage" / "auth").is_file()
    assert (INITIAL_TEST_STATE / ".storage" / "auth_provider.homeassistant").is_file()
    assert (INITIAL_TEST_STATE / ".storage" / "onboarding").is_file()
    assert "tests/initial_test_state" in workflow
    assert 'initial_state = repo_root / "tests" / "initial_test_state"' in source
    assert "shutil.copytree(initial_state, config_dir)" in source
    assert '"/supervisor/homeassistant",' in source
    assert '"copy-out"' not in source[source.index("def bake_component_into_config") :]
    assert '"rm-rf"' not in source[source.index("def bake_component_into_config") :]
    assert "_wait_core_running" not in source
    assert "_wait_addon_state" not in source


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

    assert not any(
        str(requirement).lower().startswith("fastmcp")
        for requirement in manifest.get("requirements", [])
    )
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
        "esp_get_yaml",
        "esp_update_yaml",
        "esp_validate_yaml",
        "esp_device_logs",
        "esp_compile_firmware",
        "esp_install_firmware",
        "esp_firmware_jobs",
        "esp_get_firmware_job",
        "esp_follow_firmware_job",
    }

    assert expected_tools <= string_constants
    assert "Kitchen ESPHome" in string_constants
    assert "Kitchen ESPHome Temperature" in string_constants
    assert "ESP MCP E2E" in string_constants
    assert "devices/create" in string_constants
    assert "firmware/cancel" in string_constants
    assert "ESPHOME_MCP_SERVER_WEBHOOK_ID" in EMBEDDED_E2E_PATH.read_text()
    assert "brands/access_token" in string_constants
    assert "/api/brands/integration/esphome_mcp/icon.png" in string_constants
    assert "config_entries/get" in string_constants
    assert "/api/config/config_entries/options/flow" in string_constants
    assert "description_placeholders" in string_constants
    assert "connect_url" in string_constants
    source = EMBEDDED_E2E_PATH.read_text()
    assert "MCPServerUnavailableError" in source
    assert 'assert "<your-home-assistant-url>" not in connect_url' in source
    assert 'assert "Home Assistant URL unavailable" not in connect_url' in source
