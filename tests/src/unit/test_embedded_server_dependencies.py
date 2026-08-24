"""Unit tests for ESPHome MCP runtime dependency handling."""

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


def _install_homeassistant_stubs(
    monkeypatch: Any,
    *,
    async_process_requirements: Any | None = None,
    requirements_not_found: type[Exception] | None = None,
) -> type[Exception]:
    """Install just enough Home Assistant modules to import embedded_server."""
    requirements_not_found = requirements_not_found or type(
        "RequirementsNotFound", (Exception,), {}
    )

    ha_mod = ModuleType("homeassistant")
    ha_mod.__path__ = []
    config_entries_mod = ModuleType("homeassistant.config_entries")
    config_entries_mod.ConfigEntry = object
    core_mod = ModuleType("homeassistant.core")
    core_mod.HomeAssistant = object
    req_mod = ModuleType("homeassistant.requirements")

    async def default_async_process_requirements(*_args: Any, **_kwargs: Any) -> None:
        return None

    req_mod.RequirementsNotFound = requirements_not_found
    req_mod.async_process_requirements = (
        async_process_requirements or default_async_process_requirements
    )

    monkeypatch.setitem(sys.modules, "homeassistant", ha_mod)
    monkeypatch.setitem(sys.modules, "homeassistant.config_entries", config_entries_mod)
    monkeypatch.setitem(sys.modules, "homeassistant.core", core_mod)
    monkeypatch.setitem(sys.modules, "homeassistant.requirements", req_mod)
    return requirements_not_found


def _load_embedded_server(monkeypatch: Any, **stubs: Any) -> ModuleType:
    _install_homeassistant_stubs(monkeypatch, **stubs)
    custom_components_mod = ModuleType("custom_components")
    custom_components_mod.__path__ = [str(ROOT / "custom_components")]
    monkeypatch.setitem(sys.modules, "custom_components", custom_components_mod)
    sys.modules.pop("custom_components.esphome_mcp", None)
    sys.modules.pop("custom_components.esphome_mcp.embedded_server", None)
    return importlib.import_module("custom_components.esphome_mcp.embedded_server")


class _FakeConfig:
    config_dir = "/config"

    def path(self, *parts: str) -> str:
        return "/".join(("/config", *parts))


class _FakeConfigEntries:
    def __init__(self, entries: list[Any] | None = None) -> None:
        self.updated: dict[str, Any] | None = None
        self.entries = entries or []

    def async_update_entry(self, entry: Any, *, data: dict[str, Any]) -> None:
        self.updated = data
        entry.data = data

    def async_entries(self, _domain: str) -> list[Any]:
        return self.entries


class _FakeHass:
    def __init__(self, entries: list[Any] | None = None) -> None:
        self.config = _FakeConfig()
        self.config_entries = _FakeConfigEntries(entries)
        self.data: dict[str, Any] = {}

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        return func(*args)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _peer_entry(*, disabled_by: str | None = None) -> Any:
    return SimpleNamespace(data={"entry_type": "server"}, disabled_by=disabled_by)


def _configure_runtime(
    monkeypatch: Any,
    module: ModuleType,
    *,
    version: str | None,
    peer_specs: dict[str, str | None],
    importable: bool = True,
    loaded: bool = False,
) -> None:
    monkeypatch.setattr(module, "_installed_fastmcp_version", lambda: version)
    monkeypatch.setattr(module, "_installed_peer_fastmcp_specs", lambda: peer_specs)
    monkeypatch.setattr(module, "_server_dependencies_importable", lambda: importable)
    monkeypatch.setattr(module, "_fastmcp_runtime_loaded", lambda: loaded)


def test_supported_range_accepts_current_and_announced_ha_mcp_pins(monkeypatch: Any) -> None:
    """HA-MCP patch updates inside FastMCP 3.x do not require lockstep releases."""
    module = _load_embedded_server(monkeypatch)

    assert module._version_satisfies_requirement("3.4.6", module.STANDALONE_FASTMCP_SPEC)
    assert module._version_satisfies_requirement("3.4.7", module.STANDALONE_FASTMCP_SPEC)
    assert not module._version_satisfies_requirement("3.4.4", module.STANDALONE_FASTMCP_SPEC)
    assert not module._version_satisfies_requirement("4.0.0", module.STANDALONE_FASTMCP_SPEC)


def test_enabled_peer_task_is_awaited_and_adopted_without_install(monkeypatch: Any) -> None:
    """ESPHome waits for HA-MCP ownership instead of racing its package install."""
    process_calls: list[list[str]] = []

    async def async_process_requirements(
        _hass: Any,
        _label: str,
        requirements: list[str],
        *,
        is_built_in: bool,
    ) -> None:
        assert is_built_in is False
        process_calls.append(requirements)

    module = _load_embedded_server(
        monkeypatch, async_process_requirements=async_process_requirements
    )
    _configure_runtime(
        monkeypatch,
        module,
        version="3.4.7",
        peer_specs={"ha-mcp": "fastmcp==3.4.7"},
        loaded=True,
    )
    hass = _FakeHass([_peer_entry()])
    entry = SimpleNamespace(data={}, options={})
    manager = module.EmbeddedServerManager(hass, entry)

    async def scenario() -> None:
        release = asyncio.Event()
        peer_manager = SimpleNamespace(is_running=False)

        async def bring_up_peer() -> None:
            await release.wait()
            peer_manager.is_running = True

        ensure_task = asyncio.create_task(manager._async_ensure_package())
        await asyncio.sleep(0)
        assert not ensure_task.done()
        peer_task = asyncio.create_task(bring_up_peer())
        hass.data[module.HA_MCP_DOMAIN] = {
            module.HA_MCP_BRINGUP_TASK_KEY: peer_task,
            module.HA_MCP_MANAGER_KEY: peer_manager,
        }
        release.set()
        await ensure_task
        assert not peer_task.cancelled()

    _run(scenario())

    assert process_calls == []
    assert hass.config_entries.updated == {module.DATA_LAST_PIP_SPEC: "fastmcp==3.4.7"}


def test_cancelling_esphome_wait_does_not_cancel_peer_bringup(monkeypatch: Any) -> None:
    """ESPHome unload protects HA-MCP's independently owned background task."""
    module = _load_embedded_server(monkeypatch)
    hass = _FakeHass([_peer_entry()])
    manager = module.EmbeddedServerManager(hass, SimpleNamespace(data={}, options={}))

    async def scenario() -> None:
        release = asyncio.Event()
        peer_task = asyncio.create_task(release.wait())
        hass.data[module.HA_MCP_DOMAIN] = {
            module.HA_MCP_BRINGUP_TASK_KEY: peer_task,
            module.HA_MCP_MANAGER_KEY: SimpleNamespace(is_running=False),
        }
        wait_task = asyncio.create_task(manager._async_wait_for_ha_mcp_owner())
        await asyncio.sleep(0)
        wait_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await wait_task
        assert not peer_task.cancelled()
        peer_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await peer_task

    _run(scenario())


def test_failed_enabled_peer_never_falls_back_to_install(monkeypatch: Any) -> None:
    """A failed HA-MCP owner is surfaced without a competing ESPHome install."""
    process_calls: list[list[str]] = []

    async def async_process_requirements(
        _hass: Any,
        _label: str,
        requirements: list[str],
        *,
        is_built_in: bool,
    ) -> None:
        assert is_built_in is False
        process_calls.append(requirements)

    module = _load_embedded_server(
        monkeypatch, async_process_requirements=async_process_requirements
    )
    _configure_runtime(monkeypatch, module, version=None, peer_specs={}, importable=False)
    hass = _FakeHass([_peer_entry()])
    manager = module.EmbeddedServerManager(hass, SimpleNamespace(data={}, options={}))

    async def scenario() -> None:
        peer_task = asyncio.create_task(asyncio.sleep(0))
        hass.data[module.HA_MCP_DOMAIN] = {
            module.HA_MCP_BRINGUP_TASK_KEY: peer_task,
            module.HA_MCP_MANAGER_KEY: SimpleNamespace(is_running=False),
        }
        with pytest.raises(module.EmbeddedServerError) as exc:
            await manager._async_ensure_package()
        assert exc.value.kind == "package"
        assert "did not start its runtime" in str(exc.value)

    _run(scenario())
    assert process_calls == []


def test_inactive_installed_peer_is_adopted_without_install(monkeypatch: Any) -> None:
    """Installed HA-MCP metadata remains authoritative while its entry is inactive."""
    process_calls: list[list[str]] = []

    async def async_process_requirements(
        _hass: Any,
        _label: str,
        requirements: list[str],
        *,
        is_built_in: bool,
    ) -> None:
        assert is_built_in is False
        process_calls.append(requirements)

    module = _load_embedded_server(
        monkeypatch, async_process_requirements=async_process_requirements
    )
    _configure_runtime(
        monkeypatch,
        module,
        version="3.4.6",
        peer_specs={"ha-mcp": "fastmcp==3.4.6"},
    )
    hass = _FakeHass([_peer_entry(disabled_by="user")])
    manager = module.EmbeddedServerManager(hass, SimpleNamespace(data={}, options={}))

    _run(manager._async_ensure_package())

    assert process_calls == []
    assert hass.config_entries.updated == {module.DATA_LAST_PIP_SPEC: "fastmcp==3.4.6"}


def test_ambiguous_peer_distributions_fail_without_install(monkeypatch: Any) -> None:
    """Stable and dev HA-MCP distributions cannot both claim the shared runtime."""
    module = _load_embedded_server(monkeypatch)
    _configure_runtime(
        monkeypatch,
        module,
        version="3.4.7",
        peer_specs={"ha-mcp": "fastmcp==3.4.7", "ha-mcp-dev": "fastmcp==3.4.7"},
    )
    manager = module.EmbeddedServerManager(_FakeHass(), SimpleNamespace(data={}, options={}))

    with pytest.raises(module.EmbeddedServerError) as exc:
        _run(manager._async_ensure_package())

    assert exc.value.kind == "package"
    assert "Both ha-mcp and ha-mcp-dev are installed" in str(exc.value)


def test_peer_without_fastmcp_declaration_fails_without_install(monkeypatch: Any) -> None:
    """A peer-owned graph with no declared FastMCP contract is never guessed."""
    module = _load_embedded_server(monkeypatch)
    _configure_runtime(
        monkeypatch,
        module,
        version="3.4.7",
        peer_specs={"ha-mcp": None},
    )
    manager = module.EmbeddedServerManager(_FakeHass(), SimpleNamespace(data={}, options={}))

    with pytest.raises(module.EmbeddedServerError) as exc:
        _run(manager._async_ensure_package())

    assert exc.value.kind == "package"
    assert "does not declare a FastMCP requirement" in str(exc.value)


def test_peer_pin_outside_supported_range_requires_compatible_updates(
    monkeypatch: Any,
) -> None:
    """FastMCP major-version adoption remains an explicit compatibility decision."""
    module = _load_embedded_server(monkeypatch)
    _configure_runtime(
        monkeypatch,
        module,
        version="4.0.0",
        peer_specs={"ha-mcp": "fastmcp==4.0.0"},
        loaded=True,
    )
    manager = module.EmbeddedServerManager(_FakeHass(), SimpleNamespace(data={}, options={}))

    with pytest.raises(module.EmbeddedServerError) as exc:
        _run(manager._async_ensure_package())

    assert exc.value.kind == "restart"
    assert "outside ESPHome MCP's supported range" in str(exc.value)


def test_installed_version_must_satisfy_peer_requirement(monkeypatch: Any) -> None:
    """Peer metadata cannot mask a torn or incomplete dependency update."""
    module = _load_embedded_server(monkeypatch)
    _configure_runtime(
        monkeypatch,
        module,
        version="3.4.6",
        peer_specs={"ha-mcp": "fastmcp==3.4.7"},
        loaded=True,
    )
    manager = module.EmbeddedServerManager(_FakeHass(), SimpleNamespace(data={}, options={}))

    with pytest.raises(module.EmbeddedServerError) as exc:
        _run(manager._async_ensure_package())

    assert exc.value.kind == "restart"
    assert "does not satisfy ha-mcp requirement fastmcp==3.4.7" in str(exc.value)


def test_compatible_standalone_runtime_is_reused_without_install(monkeypatch: Any) -> None:
    """A compatible preinstalled FastMCP remains untouched when HA-MCP is absent."""
    process_calls: list[list[str]] = []

    async def async_process_requirements(
        _hass: Any,
        _label: str,
        requirements: list[str],
        *,
        is_built_in: bool,
    ) -> None:
        assert is_built_in is False
        process_calls.append(requirements)

    module = _load_embedded_server(
        monkeypatch, async_process_requirements=async_process_requirements
    )
    _configure_runtime(monkeypatch, module, version="3.4.7", peer_specs={}, loaded=True)
    hass = _FakeHass()
    entry = SimpleNamespace(data={module.DATA_LAST_PIP_SPEC: "fastmcp==3.4.5"}, options={})
    manager = module.EmbeddedServerManager(hass, entry)

    _run(manager._async_ensure_package())

    assert process_calls == []
    assert hass.config_entries.updated == {
        module.DATA_LAST_PIP_SPEC: module.STANDALONE_FASTMCP_SPEC
    }


def test_disabled_peer_entry_without_distribution_uses_standalone(
    monkeypatch: Any,
) -> None:
    """A disabled HA-MCP entry does not block a peer-free standalone runtime."""
    module = _load_embedded_server(monkeypatch)
    _configure_runtime(monkeypatch, module, version="3.4.6", peer_specs={})
    hass = _FakeHass([_peer_entry(disabled_by="user")])
    manager = module.EmbeddedServerManager(hass, SimpleNamespace(data={}, options={}))

    _run(manager._async_ensure_package())

    assert hass.config_entries.updated == {
        module.DATA_LAST_PIP_SPEC: module.STANDALONE_FASTMCP_SPEC
    }


def test_cold_standalone_runtime_uses_bounded_requirement(monkeypatch: Any) -> None:
    """Only a peer-free cold runtime invokes HA's requirement manager."""
    process_calls: list[tuple[str, list[str], bool]] = []
    importable = iter([False, True])
    versions = iter([None, "3.4.7"])

    async def async_process_requirements(
        _hass: Any,
        label: str,
        requirements: list[str],
        *,
        is_built_in: bool,
    ) -> None:
        process_calls.append((label, requirements, is_built_in))

    module = _load_embedded_server(
        monkeypatch, async_process_requirements=async_process_requirements
    )
    monkeypatch.setattr(module, "_server_dependencies_importable", lambda: next(importable))
    monkeypatch.setattr(module, "_installed_fastmcp_version", lambda: next(versions))
    monkeypatch.setattr(module, "_installed_peer_fastmcp_specs", lambda: {})
    monkeypatch.setattr(module, "_fastmcp_runtime_loaded", lambda: False)
    hass = _FakeHass()
    manager = module.EmbeddedServerManager(hass, SimpleNamespace(data={}, options={}))

    _run(manager._async_ensure_package())

    assert process_calls == [
        (
            "ESPHome MCP server (fastmcp>=3.4.5,<4)",
            ["fastmcp>=3.4.5,<4"],
            False,
        )
    ]
    assert hass.config_entries.updated == {
        module.DATA_LAST_PIP_SPEC: module.STANDALONE_FASTMCP_SPEC
    }


def test_loaded_incompatible_standalone_runtime_is_preserved(monkeypatch: Any) -> None:
    """A running FastMCP consumer is never evicted or overwritten in-process."""
    process_calls: list[list[str]] = []

    async def async_process_requirements(
        _hass: Any,
        _label: str,
        requirements: list[str],
        *,
        is_built_in: bool,
    ) -> None:
        assert is_built_in is False
        process_calls.append(requirements)

    module = _load_embedded_server(
        monkeypatch, async_process_requirements=async_process_requirements
    )
    _configure_runtime(
        monkeypatch,
        module,
        version="3.4.4",
        peer_specs={},
        loaded=True,
    )
    fastmcp_module = ModuleType("fastmcp")
    monkeypatch.setitem(sys.modules, "fastmcp", fastmcp_module)
    manager = module.EmbeddedServerManager(_FakeHass(), SimpleNamespace(data={}, options={}))

    with pytest.raises(module.EmbeddedServerError) as exc:
        _run(manager._async_ensure_package())

    assert exc.value.kind == "restart"
    assert "Refusing to replace the shared runtime" in str(exc.value)
    assert process_calls == []
    assert sys.modules["fastmcp"] is fastmcp_module


def test_install_result_outside_supported_range_fails_closed(monkeypatch: Any) -> None:
    """A successful installer exit cannot mask an incompatible resolver result."""
    module = _load_embedded_server(monkeypatch)
    importable = iter([False, True])
    versions = iter([None, "4.0.0"])
    monkeypatch.setattr(module, "_server_dependencies_importable", lambda: next(importable))
    monkeypatch.setattr(module, "_installed_fastmcp_version", lambda: next(versions))
    monkeypatch.setattr(module, "_installed_peer_fastmcp_specs", lambda: {})
    monkeypatch.setattr(module, "_fastmcp_runtime_loaded", lambda: False)
    manager = module.EmbeddedServerManager(_FakeHass(), SimpleNamespace(data={}, options={}))

    with pytest.raises(module.EmbeddedServerError) as exc:
        _run(manager._async_ensure_package())

    assert exc.value.kind == "package"
    assert "does not satisfy fastmcp>=3.4.5,<4" in str(exc.value)


def test_stateless_http_server_does_not_enable_uvicorn_websockets(monkeypatch: Any) -> None:
    """The MCP listener starts without importing Home Assistant's websockets copy."""
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


def test_dependency_probe_does_not_import_runtime_packages(monkeypatch: Any) -> None:
    """Import checks do not cache stale FastMCP modules before installs."""
    module = _load_embedded_server(monkeypatch)
    original_import = builtins.__import__

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "fastmcp" or name.startswith("fastmcp.") or name == "uvicorn":
            raise AssertionError(f"{name} was imported during dependency probing")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(
        module.importlib.util,
        "find_spec",
        lambda name: object() if name in {"fastmcp", "uvicorn"} else None,
    )

    assert module._server_dependencies_importable() is True
    assert "fastmcp" not in sys.modules
    assert "uvicorn" not in sys.modules


def test_requirement_install_failure_raises_package_error(monkeypatch: Any) -> None:
    """HA requirement-manager failures are surfaced as package bring-up errors."""
    requirements_not_found = type("RequirementsNotFound", (Exception,), {})

    async def async_process_requirements(*_args: Any, **_kwargs: Any) -> None:
        raise requirements_not_found("no wheel")

    module = _load_embedded_server(
        monkeypatch,
        async_process_requirements=async_process_requirements,
        requirements_not_found=requirements_not_found,
    )
    _configure_runtime(monkeypatch, module, version=None, peer_specs={}, importable=False)
    manager = module.EmbeddedServerManager(_FakeHass(), SimpleNamespace(data={}, options={}))

    with pytest.raises(module.EmbeddedServerError) as exc:
        _run(manager._async_ensure_package())

    assert exc.value.kind == "package"
    assert "fastmcp>=3.4.5,<4" in str(exc.value)


def test_module_lock_deadlock_is_retried_then_import_succeeds(monkeypatch: Any) -> None:
    """The exact cross-thread importlib deadlock is retried after a short delay."""
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


def test_non_deadlock_runtime_error_is_never_retried(monkeypatch: Any) -> None:
    """Unrelated runtime failures retain their original traceback and semantics."""
    module = _load_embedded_server(monkeypatch)
    failure = RuntimeError("application startup failed")
    calls: list[str] = []

    def import_module(name: str) -> ModuleType:
        calls.append(name)
        raise failure

    monkeypatch.setattr(module.importlib, "import_module", import_module)
    monkeypatch.setattr(
        module.time,
        "sleep",
        lambda _delay: (_ for _ in ()).throw(AssertionError("must not sleep")),
    )

    with pytest.raises(RuntimeError) as exc:
        module._import_server_runtime_with_retry()

    assert exc.value is failure
    assert calls == ["uvicorn"]


def test_repeated_module_lock_deadlocks_require_restart(monkeypatch: Any) -> None:
    """Retry exhaustion becomes a structured restart repair, not a raw crash."""
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
    assert isinstance(exc.value.__cause__, RuntimeError)
    assert sleeps == [0.1, 0.2]


def test_wrapped_module_lock_deadlock_is_detected(monkeypatch: Any) -> None:
    """Import wrappers cannot hide the retryable deadlock in an exception chain."""
    module = _load_embedded_server(monkeypatch)
    deadlock = RuntimeError("deadlock detected by _ModuleLock('fastmcp.server.context')")
    wrapper = RuntimeError("FastMCP import failed")
    wrapper.__cause__ = deadlock

    assert module._is_module_lock_deadlock(wrapper) is True
