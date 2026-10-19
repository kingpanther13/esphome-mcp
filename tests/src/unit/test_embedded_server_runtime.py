"""Unit tests for the embedded ESPHome MCP server runtime."""

from __future__ import annotations

import asyncio
import builtins
import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
COMPONENT = ROOT / "custom_components" / "esphome_mcp"


def _load_embedded_server(monkeypatch: Any) -> ModuleType:
    """Import embedded_server against just enough Home Assistant stubs."""
    ha_mod = ModuleType("homeassistant")
    ha_mod.__path__ = []
    config_entries_mod = ModuleType("homeassistant.config_entries")
    config_entries_mod.ConfigEntry = object
    core_mod = ModuleType("homeassistant.core")
    core_mod.HomeAssistant = object
    monkeypatch.setitem(sys.modules, "homeassistant", ha_mod)
    monkeypatch.setitem(sys.modules, "homeassistant.config_entries", config_entries_mod)
    monkeypatch.setitem(sys.modules, "homeassistant.core", core_mod)
    custom_components_mod = ModuleType("custom_components")
    custom_components_mod.__path__ = [str(ROOT / "custom_components")]
    monkeypatch.setitem(sys.modules, "custom_components", custom_components_mod)
    for module_name in (
        "custom_components.esphome_mcp",
        "custom_components.esphome_mcp.embedded_server",
    ):
        monkeypatch.delitem(sys.modules, module_name, raising=False)
    return importlib.import_module("custom_components.esphome_mcp.embedded_server")


class _FakeConfig:
    config_dir = "/config"

    def path(self, *parts: str) -> str:
        return "/".join(("/config", *parts))


class _FakeConfigEntries:
    def __init__(self) -> None:
        self.updated: dict[str, Any] | None = None

    def async_update_entry(self, entry: Any, *, data: dict[str, Any]) -> None:
        self.updated = data
        entry.data = data

    def async_entries(self, domain: str) -> list[Any]:
        raise AssertionError(f"ESPHome MCP startup must not inspect {domain} entries")


class _FakeHass:
    def __init__(self) -> None:
        self.config = _FakeConfig()
        self.config_entries = _FakeConfigEntries()
        self.data: dict[str, Any] = {}

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        return func(*args)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_runtime_has_no_package_install_or_peer_coupling() -> None:
    """The vendored runtime ships with the component; nothing is installed at start."""
    source = (COMPONENT / "embedded_server.py").read_text()

    for forbidden in (
        "homeassistant.requirements",
        "async_process_requirements",
        "importlib.metadata",
        "ha_mcp_tools",
        "ha_mcp_runtime",
        "ha_mcp._vendor",
    ):
        assert forbidden not in source


def test_start_ignores_other_integrations_and_drops_legacy_runtime_data(
    monkeypatch: Any,
) -> None:
    """Startup never reads HA-MCP state and clears 1.x runtime bookkeeping."""
    module = _load_embedded_server(monkeypatch)
    started: list[str] = []

    class FakeThread:
        def __init__(self, *, target: Any, name: str, daemon: bool) -> None:
            self.name = name

        def start(self) -> None:
            started.append(self.name)

    async def ready(_self: Any) -> None:
        return None

    monkeypatch.setattr(module.threading, "Thread", FakeThread)
    monkeypatch.setattr(module.os, "makedirs", lambda *_args: None)
    monkeypatch.setattr(module.EmbeddedServerManager, "_async_wait_until_ready", ready)
    hass = _FakeHass()
    entry = SimpleNamespace(
        data={
            module.DATA_SECRET_PATH: "/private",
            "webhook_id": "hook",
            "last_pip_spec": "ha-mcp @ https://example.invalid/ha-mcp.zip",
            "owned_runtime_url": "https://example.invalid/ha-mcp.zip",
        },
        options={},
    )

    _run(module.EmbeddedServerManager(hass, entry).async_start())

    assert started == ["esphome-mcp-server"]
    assert hass.config_entries.updated == {
        module.DATA_SECRET_PATH: "/private",
        "webhook_id": "hook",
    }


def test_start_leaves_current_entry_data_untouched(monkeypatch: Any) -> None:
    module = _load_embedded_server(monkeypatch)

    class FakeThread:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def start(self) -> None:
            return None

    async def ready(_self: Any) -> None:
        return None

    monkeypatch.setattr(module.threading, "Thread", FakeThread)
    monkeypatch.setattr(module.os, "makedirs", lambda *_args: None)
    monkeypatch.setattr(module.EmbeddedServerManager, "_async_wait_until_ready", ready)
    hass = _FakeHass()
    entry = SimpleNamespace(data={module.DATA_SECRET_PATH: "/private"}, options={})

    _run(module.EmbeddedServerManager(hass, entry).async_start())

    assert hass.config_entries.updated is None


def test_worker_records_the_vendored_fastmcp_version(monkeypatch: Any) -> None:
    module = _load_embedded_server(monkeypatch)
    vendored = ModuleType(module._VENDORED_FASTMCP)
    vendored.__version__ = "9.9.9"

    def fake_import() -> None:
        sys.modules[module._VENDORED_FASTMCP] = vendored

    async def fake_serve(_self: Any) -> None:
        return None

    monkeypatch.setitem(sys.modules, module._VENDORED_FASTMCP, vendored)
    monkeypatch.setenv("ESPHOME_MCP_CONFIG_DIR", "/config/.esphome_mcp")
    monkeypatch.setattr(module, "_import_server_runtime_with_retry", fake_import)
    monkeypatch.setattr(module.EmbeddedServerManager, "_serve", fake_serve)
    entry = SimpleNamespace(data={module.DATA_SECRET_PATH: "/private"}, options={})
    manager = module.EmbeddedServerManager(_FakeHass(), entry)

    manager._thread_main()

    assert module._VENDORED_FASTMCP == "custom_components.esphome_mcp._vendor.fastmcp"
    assert manager.fastmcp_version == "9.9.9"
    assert manager._thread_exc is None


def test_stateless_http_server_disables_uvicorn_websockets(monkeypatch: Any) -> None:
    """The MCP listener does not import Home Assistant's shared websockets copy."""
    module = _load_embedded_server(monkeypatch)
    captured: dict[str, Any] = {}

    class FakeLifespan:
        async def __aenter__(self) -> FakeLifespan:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

    class FakeMcp:
        def http_app(self, *, path: str, stateless_http: bool) -> object:
            captured["path"] = path
            captured["stateless_http"] = stateless_http
            return object()

        def _lifespan_manager(self) -> FakeLifespan:
            return FakeLifespan()

    class FakeEspHomeMCPServer:
        def __init__(self, _hass: Any) -> None:
            self.mcp = FakeMcp()

    class FakeConfig:
        def __init__(self, _app: object, **kwargs: Any) -> None:
            captured.update(kwargs)

    class FakeUvicornServer:
        def __init__(self, _config: FakeConfig) -> None:
            self.should_exit = False

        async def serve(self) -> None:
            return None

    uvicorn_module = ModuleType("uvicorn")
    uvicorn_module.Config = FakeConfig
    uvicorn_module.Server = FakeUvicornServer
    server_module = ModuleType("custom_components.esphome_mcp.server")
    server_module.EspHomeMCPServer = FakeEspHomeMCPServer
    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn_module)
    monkeypatch.setitem(sys.modules, "custom_components.esphome_mcp.server", server_module)

    original_import = builtins.__import__

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "websockets" or name.startswith("websockets."):
            raise AssertionError(f"stateless MCP startup imported {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    entry = SimpleNamespace(data={module.DATA_SECRET_PATH: "/private"}, options={})
    manager = module.EmbeddedServerManager(_FakeHass(), entry)

    async def run_server() -> None:
        manager._stop_event = asyncio.Event()
        await manager._serve()

    _run(run_server())

    assert captured["path"] == "/private"
    assert captured["stateless_http"] is True
    assert captured["ws"] == "none"


def test_module_lock_deadlock_is_retried_then_import_succeeds(monkeypatch: Any) -> None:
    """The exact cross-thread importlib deadlock is retried."""
    module = _load_embedded_server(monkeypatch)
    calls: list[str] = []
    sleeps: list[float] = []
    server_attempts = 0

    def import_module(name: str) -> ModuleType:
        nonlocal server_attempts
        calls.append(name)
        if name.endswith(".server"):
            server_attempts += 1
            if server_attempts == 1:
                raise RuntimeError("deadlock detected by _ModuleLock('fastmcp.server.server')")
        return ModuleType(name)

    monkeypatch.setattr(module.importlib, "import_module", import_module)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    monkeypatch.setattr(module, "_IMPORT_DEADLOCK_RETRY_DELAYS_SECONDS", (0.25,))

    module._import_server_runtime_with_retry()

    assert calls == [
        "uvicorn",
        "custom_components.esphome_mcp.server",
        "uvicorn",
        "custom_components.esphome_mcp.server",
    ]
    assert sleeps == [0.25]


def test_repeated_module_lock_deadlocks_require_restart(monkeypatch: Any) -> None:
    """Retry exhaustion becomes a structured restart repair."""
    module = _load_embedded_server(monkeypatch)
    sleeps: list[float] = []

    def import_module(_name: str) -> ModuleType:
        raise RuntimeError("deadlock detected by _ModuleLock('fastmcp.server.server')")

    monkeypatch.setattr(module.importlib, "import_module", import_module)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    monkeypatch.setattr(module, "_IMPORT_DEADLOCK_RETRY_DELAYS_SECONDS", (0.1, 0.2))

    with pytest.raises(module.EmbeddedServerError) as exc:
        module._import_server_runtime_with_retry()

    assert exc.value.kind == "restart"
    assert "repeatedly collided" in str(exc.value)
    assert sleeps == [0.1, 0.2]
