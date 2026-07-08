"""Unit tests for ESPHome MCP server tool registration and HA search tools."""

from __future__ import annotations

import asyncio
import importlib
import sys
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
COMPONENT = ROOT / "custom_components" / "esphome_mcp"

EXPECTED_ESP_TOOLS = {
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


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _install_dependency_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the runtime modules needed to import server.py in unit tests."""
    custom_components_mod = ModuleType("custom_components")
    custom_components_mod.__path__ = [str(ROOT / "custom_components")]
    package_mod = ModuleType("custom_components.esphome_mcp")
    package_mod.__path__ = [str(COMPONENT)]

    fastmcp_mod = ModuleType("fastmcp")

    class _FakeFastMCP:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.tools: dict[str, Any] = {}
            self.routes: dict[str, Any] = {}

        def tool(self, *, name: str, annotations: dict[str, Any] | None = None) -> Any:
            def decorator(func: Any) -> Any:
                self.tools[name] = SimpleNamespace(
                    name=name,
                    annotations=annotations or {},
                    func=func,
                )
                return func

            return decorator

        def custom_route(self, path: str, *, methods: list[str]) -> Any:
            def decorator(func: Any) -> Any:
                self.routes[path] = SimpleNamespace(methods=methods, func=func)
                return func

            return decorator

    fastmcp_mod.FastMCP = _FakeFastMCP

    pydantic_mod = ModuleType("pydantic")

    def field(**kwargs: Any) -> Any:
        return kwargs.get("default")

    pydantic_mod.Field = field

    ha_mod = ModuleType("homeassistant")
    ha_mod.__path__ = []
    core_mod = ModuleType("homeassistant.core")
    core_mod.HomeAssistant = object

    aiohttp_mod = ModuleType("aiohttp")

    class _WSMsgType(Enum):
        TEXT = "TEXT"
        BINARY = "BINARY"
        CLOSE = "CLOSE"
        CLOSED = "CLOSED"
        ERROR = "ERROR"

    aiohttp_mod.WSMsgType = _WSMsgType
    aiohttp_mod.ClientError = type("ClientError", (Exception,), {})

    class _ClientTimeout:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    aiohttp_mod.ClientTimeout = _ClientTimeout
    aiohttp_mod.ClientSession = object

    for module_name in (
        "custom_components.esphome_mcp.server",
        "custom_components.esphome_mcp.addon_tools",
        "custom_components.esphome_mcp.const",
    ):
        monkeypatch.delitem(sys.modules, module_name, raising=False)

    monkeypatch.setitem(sys.modules, "custom_components", custom_components_mod)
    monkeypatch.setitem(sys.modules, "custom_components.esphome_mcp", package_mod)
    monkeypatch.setitem(sys.modules, "fastmcp", fastmcp_mod)
    monkeypatch.setitem(sys.modules, "pydantic", pydantic_mod)
    monkeypatch.setitem(sys.modules, "homeassistant", ha_mod)
    monkeypatch.setitem(sys.modules, "homeassistant.core", core_mod)
    monkeypatch.setitem(sys.modules, "aiohttp", aiohttp_mod)


def _load_server_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    _install_dependency_stubs(monkeypatch)
    return importlib.import_module("custom_components.esphome_mcp.server")


async def _run_inline(_hass: Any, coro: Any) -> Any:
    return await coro


def test_mcp_server_registers_every_current_esp_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public MCP registration includes every current ESPHome tool."""
    module = _load_server_module(monkeypatch)

    server = module.EspHomeMCPServer(SimpleNamespace(loop=None))

    assert set(server.mcp.tools) == EXPECTED_ESP_TOOLS
    assert server.mcp.kwargs["name"] == "esphome-mcp"
    assert server.mcp.kwargs["version"] == module.VERSION


def test_ha_search_tools_filter_snapshot_and_report_overview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overview/device/entity tools filter the HA ESPHome snapshot predictably."""
    module = _load_server_module(monkeypatch)
    monkeypatch.setattr(module, "_run_on_hass", _run_inline)

    snapshot = {
        "config_entries": [{"entry_id": "entry-1"}],
        "devices": [
            {
                "id": "dev-kitchen",
                "name": "Kitchen ESPHome",
                "area": "Kitchen",
                "config_entry_states": ["ConfigEntryState.LOADED"],
                "identifiers": [["esphome", "kitchen-node"]],
            },
            {
                "id": "dev-garage",
                "name": "Garage ESPHome",
                "area": "Garage",
                "config_entry_states": ["ConfigEntryState.NOT_LOADED"],
                "identifiers": [["esphome", "garage-node"]],
            },
        ],
        "entities": [
            {
                "entity_id": "sensor.kitchen_temperature",
                "domain": "sensor",
                "name": "Kitchen Temperature",
                "device_id": "dev-kitchen",
                "state": "72.1",
                "disabled_by": None,
            },
            {
                "entity_id": "switch.kitchen_relay",
                "domain": "switch",
                "name": "Kitchen Relay",
                "device_id": "dev-kitchen",
                "state": "off",
                "disabled_by": "user",
            },
            {
                "entity_id": "sensor.garage_temperature",
                "domain": "sensor",
                "name": "Garage Temperature",
                "device_id": "dev-garage",
                "state": "63",
                "disabled_by": None,
            },
        ],
    }

    async def async_snapshot(_hass: Any) -> dict[str, Any]:
        return snapshot

    monkeypatch.setattr(module, "_async_snapshot", async_snapshot)

    server = module.EspHomeMCPServer(SimpleNamespace(loop=None))
    tools = server.mcp.tools

    overview = _run(tools["esp_overview"].func())
    devices = _run(
        tools["esp_list_devices"].func(
            query="kitchen-node",
            area="kit",
            config_entry_state="loaded",
            limit=5,
        )
    )
    enabled_entities = _run(
        tools["esp_list_entities"].func(
            query="temperature",
            domain="sensor",
            device_id="dev-kitchen",
            state="72",
            disabled=False,
            limit=5,
        )
    )
    disabled_entities = _run(
        tools["esp_list_entities"].func(
            query="relay",
            device_id="dev-kitchen",
            disabled=True,
            limit=5,
        )
    )

    assert overview == {
        "success": True,
        "integration_domain": "esphome",
        "mcp_domain": module.DOMAIN,
        "server_version": module.VERSION,
        "config_entry_count": 1,
        "device_count": 2,
        "entity_count": 3,
    }
    assert devices == {
        "success": True,
        "count": 1,
        "devices": [snapshot["devices"][0]],
    }
    assert enabled_entities == {
        "success": True,
        "count": 1,
        "entities": [snapshot["entities"][0]],
    }
    assert disabled_entities == {
        "success": True,
        "count": 1,
        "entities": [snapshot["entities"][1]],
    }


def test_device_builder_tool_wrappers_forward_expected_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each ESPHome Device Builder MCP tool reaches the expected helper."""
    module = _load_server_module(monkeypatch)
    monkeypatch.setattr(module, "_run_on_hass", _run_inline)
    hass = SimpleNamespace(loop=None)
    calls: list[tuple[str, Any, dict[str, Any]]] = []

    def record_helper(name: str) -> Any:
        async def helper(helper_hass: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append((name, helper_hass, kwargs))
            return {"success": True, "helper": name}

        return helper

    for helper_name in (
        "manage_esphome_addon",
        "list_device_builder_devices",
        "search_device_builder_yaml",
        "read_device_builder_config",
        "write_device_builder_config",
        "run_device_builder_stream",
        "queue_device_builder_firmware_job",
        "list_device_builder_firmware_jobs",
        "get_device_builder_firmware_job",
        "follow_device_builder_firmware_job",
    ):
        monkeypatch.setattr(module, helper_name, record_helper(helper_name))

    server = module.EspHomeMCPServer(hass)
    tools = server.mcp.tools

    _run(
        tools["esp_manage_addon"].func(
            slug="5c53de3b_esphome",
            action="restart",
            path="/devices",
            method="POST",
            body={"ping": True},
            websocket=True,
            wait_for_close=False,
            message_limit=3,
            message_offset=1,
            options={"log_level": "debug"},
            network={"6052/tcp": 6052},
            boot="auto",
            auto_update=True,
            watchdog=False,
            port=6052,
            timeout=90,
            debug=True,
            request_headers={"X-Test": "yes"},
        )
    )
    _run(
        tools["esp_dashboard_devices"].func(
            slug="5c53de3b_esphome",
            query="kitchen",
            state="online",
            include_importable=False,
            limit=7,
            timeout=91,
            debug=True,
        )
    )
    _run(
        tools["esp_search_yaml"].func(
            "wifi",
            slug="5c53de3b_esphome",
            max_results=8,
            case_sensitive=True,
            context_lines=2,
            timeout=92,
            debug=True,
        )
    )
    _run(
        tools["esp_get_yaml"].func(
            "kitchen.yaml",
            slug="5c53de3b_esphome",
            timeout=93,
            debug=True,
        )
    )
    _run(
        tools["esp_update_yaml"].func(
            "kitchen.yaml",
            "esphome:\n  name: kitchen\n",
            slug="5c53de3b_esphome",
            allow_wipe=True,
            timeout=94,
            debug=True,
        )
    )
    _run(
        tools["esp_validate_yaml"].func(
            "kitchen.yaml",
            slug="5c53de3b_esphome",
            show_secrets=True,
            message_limit=9,
            timeout=95,
            debug=True,
        )
    )
    _run(
        tools["esp_device_logs"].func(
            "kitchen.yaml",
            slug="5c53de3b_esphome",
            port="USB",
            no_states=True,
            message_limit=10,
            timeout=96,
            debug=True,
        )
    )
    _run(
        tools["esp_compile_firmware"].func(
            "kitchen.yaml",
            slug="5c53de3b_esphome",
            force_local=True,
            timeout=97,
            debug=True,
        )
    )
    _run(
        tools["esp_install_firmware"].func(
            "kitchen.yaml",
            slug="5c53de3b_esphome",
            port="OTA",
            force_local=True,
            bootloader=True,
            timeout=98,
            debug=True,
        )
    )
    _run(
        tools["esp_firmware_jobs"].func(
            slug="5c53de3b_esphome",
            status="running",
            configuration="kitchen.yaml",
            limit=11,
            timeout=99,
            debug=True,
        )
    )
    _run(
        tools["esp_get_firmware_job"].func(
            "job-1",
            slug="5c53de3b_esphome",
            timeout=100,
            debug=True,
        )
    )
    _run(
        tools["esp_follow_firmware_job"].func(
            "job-1",
            slug="5c53de3b_esphome",
            message_limit=12,
            timeout=101,
            debug=True,
        )
    )

    assert [call[0] for call in calls] == [
        "manage_esphome_addon",
        "list_device_builder_devices",
        "search_device_builder_yaml",
        "read_device_builder_config",
        "write_device_builder_config",
        "run_device_builder_stream",
        "run_device_builder_stream",
        "queue_device_builder_firmware_job",
        "queue_device_builder_firmware_job",
        "list_device_builder_firmware_jobs",
        "get_device_builder_firmware_job",
        "follow_device_builder_firmware_job",
    ]
    assert all(call[1] is hass for call in calls)
    assert calls[0][2] == {
        "slug": "5c53de3b_esphome",
        "action": "restart",
        "path": "/devices",
        "method": "POST",
        "body": {"ping": True},
        "websocket": True,
        "wait_for_close": False,
        "message_limit": 3,
        "message_offset": 1,
        "options": {"log_level": "debug"},
        "network": {"6052/tcp": 6052},
        "boot": "auto",
        "auto_update": True,
        "watchdog": False,
        "port": 6052,
        "timeout": 90,
        "debug": True,
        "request_headers": {"X-Test": "yes"},
    }
    assert calls[1][2] == {
        "slug": "5c53de3b_esphome",
        "query": "kitchen",
        "state": "online",
        "include_importable": False,
        "limit": 7,
        "timeout": 91,
        "debug": True,
    }
    assert calls[2][2] == {
        "slug": "5c53de3b_esphome",
        "query": "wifi",
        "max_results": 8,
        "case_sensitive": True,
        "context_lines": 2,
        "timeout": 92,
        "debug": True,
    }
    assert calls[3][2] == {
        "slug": "5c53de3b_esphome",
        "configuration": "kitchen.yaml",
        "timeout": 93,
        "debug": True,
    }
    assert calls[4][2] == {
        "slug": "5c53de3b_esphome",
        "configuration": "kitchen.yaml",
        "content": "esphome:\n  name: kitchen\n",
        "allow_wipe": True,
        "timeout": 94,
        "debug": True,
    }
    assert calls[5][2] == {
        "slug": "5c53de3b_esphome",
        "command": "devices/validate",
        "args": {"configuration": "kitchen.yaml", "show_secrets": True},
        "timeout": 95,
        "debug": True,
        "message_limit": 9,
    }
    assert calls[6][2] == {
        "slug": "5c53de3b_esphome",
        "command": "devices/logs",
        "args": {"configuration": "kitchen.yaml", "port": "USB", "no_states": True},
        "timeout": 96,
        "debug": True,
        "message_limit": 10,
    }
    assert calls[7][2] == {
        "slug": "5c53de3b_esphome",
        "command": "firmware/compile",
        "args": {"configuration": "kitchen.yaml", "force_local": True},
        "timeout": 97,
        "debug": True,
    }
    assert calls[8][2] == {
        "slug": "5c53de3b_esphome",
        "command": "firmware/install",
        "args": {
            "configuration": "kitchen.yaml",
            "port": "OTA",
            "force_local": True,
            "bootloader": True,
        },
        "timeout": 98,
        "debug": True,
    }
    assert calls[9][2] == {
        "slug": "5c53de3b_esphome",
        "status": "running",
        "configuration": "kitchen.yaml",
        "limit": 11,
        "timeout": 99,
        "debug": True,
    }
    assert calls[10][2] == {
        "slug": "5c53de3b_esphome",
        "job_id": "job-1",
        "timeout": 100,
        "debug": True,
    }
    assert calls[11][2] == {
        "slug": "5c53de3b_esphome",
        "job_id": "job-1",
        "message_limit": 12,
        "timeout": 101,
        "debug": True,
    }


def test_snapshot_collects_only_esphome_registry_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registry snapshots include ESPHome records and ignore unrelated HA records."""
    module = _load_server_module(monkeypatch)

    helpers_mod = ModuleType("homeassistant.helpers")
    helpers_mod.__path__ = []
    device_registry_mod = ModuleType("homeassistant.helpers.device_registry")
    entity_registry_mod = ModuleType("homeassistant.helpers.entity_registry")

    def async_get_device_registry(hass: Any) -> Any:
        return hass.device_registry

    def async_get_entity_registry(hass: Any) -> Any:
        return hass.entity_registry

    device_registry_mod.async_get = async_get_device_registry
    entity_registry_mod.async_get = async_get_entity_registry

    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers_mod)
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.helpers.device_registry",
        device_registry_mod,
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.helpers.entity_registry",
        entity_registry_mod,
    )

    entries = [
        SimpleNamespace(
            entry_id="entry-loaded",
            title="Kitchen ESPHome",
            state="loaded",
            disabled_by=None,
        ),
        SimpleNamespace(
            entry_id="entry-disabled",
            title="Disabled ESPHome",
            state="not_loaded",
            disabled_by="user",
        ),
    ]
    devices = {
        "dev-kitchen": SimpleNamespace(
            id="dev-kitchen",
            name_by_user="Kitchen Node",
            name="Kitchen ESPHome",
            manufacturer="ESPHome",
            model="ESP32",
            sw_version="2026.7.0",
            area_id="kitchen",
            config_entries={"entry-loaded"},
            identifiers={("esphome", "kitchen-node")},
        ),
        "dev-identifier": SimpleNamespace(
            id="dev-identifier",
            name_by_user=None,
            name="Identifier Only",
            manufacturer="ESPHome",
            model="ESP8266",
            sw_version=None,
            area_id=None,
            config_entries={"other-entry"},
            identifiers={("esphome", "adopted-node")},
        ),
        "dev-other": SimpleNamespace(
            id="dev-other",
            name_by_user="Other Device",
            name="Other Device",
            manufacturer="Other",
            model="Other",
            sw_version=None,
            area_id="garage",
            config_entries={"other-entry"},
            identifiers={("mqtt", "other-node")},
        ),
    }
    entities = {
        "sensor.kitchen_temperature": SimpleNamespace(
            entity_id="sensor.kitchen_temperature",
            name="Kitchen Temperature",
            original_name="Temperature",
            device_id="dev-kitchen",
            platform="esphome",
            disabled_by=None,
            config_entry_id="entry-loaded",
        ),
        "switch.disabled_relay": SimpleNamespace(
            entity_id="switch.disabled_relay",
            name="Disabled Relay",
            original_name="Relay",
            device_id="dev-kitchen",
            platform="template",
            disabled_by="user",
            config_entry_id="entry-disabled",
        ),
        "light.other": SimpleNamespace(
            entity_id="light.other",
            name="Other",
            original_name="Other",
            device_id="dev-other",
            platform="mqtt",
            disabled_by=None,
            config_entry_id="other-entry",
        ),
    }

    class _FakeConfigEntries:
        def async_entries(self, domain: str) -> list[Any]:
            assert domain == "esphome"
            return entries

    class _FakeStates:
        def get(self, entity_id: str) -> Any:
            states = {
                "sensor.kitchen_temperature": "71.2",
                "switch.disabled_relay": "unavailable",
            }
            if entity_id not in states:
                return None
            return SimpleNamespace(state=states[entity_id])

    hass = SimpleNamespace(
        config_entries=_FakeConfigEntries(),
        device_registry=SimpleNamespace(devices=devices),
        entity_registry=SimpleNamespace(entities=entities),
        states=_FakeStates(),
    )

    snapshot = _run(module._async_snapshot(hass))

    assert snapshot["config_entries"] == [
        {
            "entry_id": "entry-loaded",
            "title": "Kitchen ESPHome",
            "state": "loaded",
            "disabled_by": None,
        },
        {
            "entry_id": "entry-disabled",
            "title": "Disabled ESPHome",
            "state": "not_loaded",
            "disabled_by": "user",
        },
    ]
    assert [device["id"] for device in snapshot["devices"]] == [
        "dev-identifier",
        "dev-kitchen",
    ]
    assert snapshot["devices"][0]["identifiers"] == [["esphome", "adopted-node"]]
    assert snapshot["devices"][0]["config_entry_states"] == []
    assert snapshot["devices"][1]["config_entry_states"] == ["loaded"]
    assert [entity["entity_id"] for entity in snapshot["entities"]] == [
        "sensor.kitchen_temperature",
        "switch.disabled_relay",
    ]
    assert snapshot["entities"][0]["state"] == "71.2"
    assert snapshot["entities"][1]["disabled_by"] == "user"
    assert snapshot["entities"][1]["platform"] == "template"
