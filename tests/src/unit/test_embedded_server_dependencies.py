"""Unit tests for the HA-MCP master runtime contract."""

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
    integration_version: str | None = None,
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
    loader_mod = ModuleType("homeassistant.loader")
    req_mod = ModuleType("homeassistant.requirements")

    async def default_async_process_requirements(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def async_get_integration(_hass: Any, _domain: str) -> Any:
        version = integration_version
        if version is None:
            version = sys.modules[
                "custom_components.esphome_mcp.embedded_server"
            ].HA_MCP_COMPONENT_VERSION
        return SimpleNamespace(version=version)

    loader_mod.async_get_integration = async_get_integration
    req_mod.RequirementsNotFound = requirements_not_found
    req_mod.async_process_requirements = (
        async_process_requirements or default_async_process_requirements
    )

    monkeypatch.setitem(sys.modules, "homeassistant", ha_mod)
    monkeypatch.setitem(sys.modules, "homeassistant.config_entries", config_entries_mod)
    monkeypatch.setitem(sys.modules, "homeassistant.core", core_mod)
    monkeypatch.setitem(sys.modules, "homeassistant.loader", loader_mod)
    monkeypatch.setitem(sys.modules, "homeassistant.requirements", req_mod)
    return requirements_not_found


def _load_embedded_server(monkeypatch: Any, **stubs: Any) -> ModuleType:
    _install_homeassistant_stubs(monkeypatch, **stubs)
    custom_components_mod = ModuleType("custom_components")
    custom_components_mod.__path__ = [str(ROOT / "custom_components")]
    monkeypatch.setitem(sys.modules, "custom_components", custom_components_mod)
    for module_name in (
        "custom_components.esphome_mcp",
        "custom_components.esphome_mcp.embedded_server",
        "custom_components.esphome_mcp.ha_mcp_runtime",
        "custom_components.esphome_mcp.ha_mcp_runtime.contract",
    ):
        monkeypatch.delitem(sys.modules, module_name, raising=False)
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


def _peer_entry(
    *,
    disabled_by: str | None = None,
    entry_type: str = "server",
) -> Any:
    return SimpleNamespace(
        data={"entry_type": entry_type},
        disabled_by=disabled_by,
    )


def _configure_runtime(
    monkeypatch: Any,
    module: ModuleType,
    *,
    version: str | None = "3.4.7",
    peer_requirements: dict[str, tuple[str, ...]] | None = None,
    violations: tuple[str, ...] = (),
    importable: bool = True,
    loaded: bool = False,
    loaded_version: str | None = None,
    loaded_origin: str | None = None,
    installed_origin: str | None = None,
) -> None:
    """Stub package state without importing or modifying real dependencies."""
    default_origin = "/config/deps/fastmcp/__init__.py"
    monkeypatch.setattr(module, "_installed_fastmcp_version", lambda: version)
    monkeypatch.setattr(
        module,
        "_installed_ha_mcp_requirements",
        lambda: peer_requirements or {},
    )
    monkeypatch.setattr(module, "_unsatisfied_runtime_requirements", lambda: violations)
    monkeypatch.setattr(module, "_server_dependencies_importable", lambda: importable)
    monkeypatch.setattr(module, "_fastmcp_runtime_loaded", lambda: loaded)
    monkeypatch.setattr(
        module,
        "_loaded_fastmcp_fingerprint",
        lambda: (
            (
                loaded_version if loaded_version is not None else version,
                loaded_origin if loaded_origin is not None else default_origin,
            )
            if loaded
            else None
        ),
    )
    monkeypatch.setattr(
        module,
        "_installed_fastmcp_origin",
        lambda: installed_origin if installed_origin is not None else default_origin,
    )


def test_contract_is_one_master_snapshot_for_server_and_component(monkeypatch: Any) -> None:
    """The dependency-only package records both halves of one immutable commit."""
    module = _load_embedded_server(monkeypatch)

    assert len(module.HA_MCP_MASTER_SHA) == 40
    assert module.HA_MCP_COMPONENT_VERSION == "2.0.1"
    assert module.HA_MCP_FASTMCP_REQUIREMENT == "fastmcp==3.4.7"
    assert module.HA_MCP_FASTMCP_REQUIREMENT in module.HA_MCP_SERVER_REQUIREMENTS
    assert not any(
        requirement.lower().startswith("websockets")
        for requirement in module.HA_MCP_SERVER_REQUIREMENTS
    )


def test_satisfied_contract_is_reused_without_install(monkeypatch: Any) -> None:
    """ESPHome leaves a complete matching runtime untouched."""
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
        monkeypatch,
        async_process_requirements=async_process_requirements,
    )
    _configure_runtime(monkeypatch, module)
    hass = _FakeHass()
    manager = module.EmbeddedServerManager(hass, SimpleNamespace(data={}, options={}))

    _run(manager._async_ensure_package())

    assert process_calls == []
    assert manager.fastmcp_version == "3.4.7"
    assert hass.config_entries.updated == {
        module.DATA_LAST_PIP_SPEC: module.HA_MCP_FASTMCP_REQUIREMENT
    }


def test_missing_contract_installs_exact_server_requirements(monkeypatch: Any) -> None:
    """Standalone startup installs the generated server dependency tuple only."""
    process_calls: list[tuple[str, list[str]]] = []
    installed = False

    async def async_process_requirements(
        _hass: Any,
        label: str,
        requirements: list[str],
        *,
        is_built_in: bool,
    ) -> None:
        nonlocal installed
        assert is_built_in is False
        process_calls.append((label, requirements))
        installed = True

    module = _load_embedded_server(
        monkeypatch,
        async_process_requirements=async_process_requirements,
    )
    monkeypatch.setattr(module, "_installed_ha_mcp_requirements", lambda: {})
    monkeypatch.setattr(
        module,
        "_unsatisfied_runtime_requirements",
        lambda: () if installed else ("fastmcp is missing",),
    )
    monkeypatch.setattr(module, "_server_dependencies_importable", lambda: installed)
    monkeypatch.setattr(module, "_fastmcp_runtime_loaded", lambda: False)
    monkeypatch.setattr(
        module,
        "_installed_fastmcp_version",
        lambda: "3.4.7" if installed else None,
    )
    manager = module.EmbeddedServerManager(
        _FakeHass(),
        SimpleNamespace(data={}, options={}),
    )

    _run(manager._async_ensure_package())

    assert process_calls == [
        (
            f"ESPHome MCP server ({module.HA_MCP_RUNTIME_CONTRACT_ID})",
            list(module.HA_MCP_SERVER_REQUIREMENTS),
        )
    ]
    assert manager.fastmcp_version == "3.4.7"


def test_requirement_install_failure_is_a_package_error(monkeypatch: Any) -> None:
    """Home Assistant requirement-manager failures remain scoped to ESPHome MCP."""
    requirements_not_found = type("RequirementsNotFound", (Exception,), {})

    async def async_process_requirements(*_args: Any, **_kwargs: Any) -> None:
        raise requirements_not_found("no wheel")

    module = _load_embedded_server(
        monkeypatch,
        async_process_requirements=async_process_requirements,
        requirements_not_found=requirements_not_found,
    )
    _configure_runtime(
        monkeypatch,
        module,
        version=None,
        violations=("fastmcp is missing",),
        importable=False,
    )
    manager = module.EmbeddedServerManager(
        _FakeHass(),
        SimpleNamespace(data={}, options={}),
    )

    with pytest.raises(module.EmbeddedServerError) as exc:
        _run(manager._async_ensure_package())

    assert exc.value.kind == "package"
    assert module.HA_MCP_MASTER_SHA[:12] in str(exc.value)


def test_installed_ha_mcp_with_matching_contract_is_accepted(monkeypatch: Any) -> None:
    """Either HA-MCP channel may coexist when its direct dependency set matches."""
    module = _load_embedded_server(monkeypatch)
    _configure_runtime(
        monkeypatch,
        module,
        peer_requirements={"ha-mcp-dev": module.HA_MCP_SERVER_REQUIREMENTS},
    )
    manager = module.EmbeddedServerManager(
        _FakeHass(),
        SimpleNamespace(data={}, options={}),
    )

    _run(manager._async_ensure_package())

    assert manager.fastmcp_version == "3.4.7"


def test_installed_ha_mcp_with_other_contract_fails_without_install(monkeypatch: Any) -> None:
    """Stable metadata cannot silently override the mirrored master snapshot."""
    process_calls: list[list[str]] = []

    async def async_process_requirements(
        _hass: Any,
        _label: str,
        requirements: list[str],
        **_kwargs: Any,
    ) -> None:
        process_calls.append(requirements)

    module = _load_embedded_server(
        monkeypatch,
        async_process_requirements=async_process_requirements,
    )
    old_requirements = tuple(
        "fastmcp==3.4.6" if requirement.startswith("fastmcp==") else requirement
        for requirement in module.HA_MCP_SERVER_REQUIREMENTS
    )
    _configure_runtime(
        monkeypatch,
        module,
        peer_requirements={"ha-mcp": old_requirements},
    )
    manager = module.EmbeddedServerManager(
        _FakeHass(),
        SimpleNamespace(data={}, options={}),
    )

    with pytest.raises(module.EmbeddedServerError) as exc:
        _run(manager._async_ensure_package())

    assert exc.value.kind == "package"
    assert "does not match HA-MCP master" in str(exc.value)
    assert "fastmcp==3.4.7" in str(exc.value)
    assert "fastmcp==3.4.6" in str(exc.value)
    assert process_calls == []


def test_both_ha_mcp_distributions_fail_closed(monkeypatch: Any) -> None:
    """Stable and dev distributions cannot share the same import package."""
    module = _load_embedded_server(monkeypatch)

    with pytest.raises(module.EmbeddedServerError) as exc:
        module._validate_installed_ha_mcp_contract(
            {
                "ha-mcp": module.HA_MCP_SERVER_REQUIREMENTS,
                "ha-mcp-dev": module.HA_MCP_SERVER_REQUIREMENTS,
            }
        )

    assert "Both ha-mcp and ha-mcp-dev are installed" in str(exc.value)


def test_configured_ha_mcp_component_must_match_snapshot(monkeypatch: Any) -> None:
    """Server dependency lockstep also checks the installed component version."""
    module = _load_embedded_server(monkeypatch, integration_version="2.0.0")
    _configure_runtime(monkeypatch, module)
    manager = module.EmbeddedServerManager(
        _FakeHass([_peer_entry()]),
        SimpleNamespace(data={}, options={}),
    )

    with pytest.raises(module.EmbeddedServerError) as exc:
        _run(manager._async_ensure_package())

    assert exc.value.kind == "package"
    assert "custom component is version 2.0.0" in str(exc.value)
    assert module.HA_MCP_COMPONENT_VERSION in str(exc.value)


def test_disabled_ha_mcp_component_does_not_gate_standalone_start(monkeypatch: Any) -> None:
    """An inactive peer component does not prevent standalone contract use."""
    module = _load_embedded_server(monkeypatch, integration_version="0.0.0")
    _configure_runtime(monkeypatch, module)
    manager = module.EmbeddedServerManager(
        _FakeHass([_peer_entry(disabled_by="user")]),
        SimpleNamespace(data={}, options={}),
    )

    _run(manager._async_ensure_package())

    assert manager.fastmcp_version == "3.4.7"


def test_enabled_ha_mcp_server_without_bringup_task_fails_without_pip(
    monkeypatch: Any,
) -> None:
    """ESPHome never races an enabled HA-MCP server's installer."""
    process_calls: list[list[str]] = []

    async def async_process_requirements(
        _hass: Any,
        _label: str,
        requirements: list[str],
        **_kwargs: Any,
    ) -> None:
        process_calls.append(requirements)

    module = _load_embedded_server(
        monkeypatch,
        async_process_requirements=async_process_requirements,
    )
    _configure_runtime(monkeypatch, module)
    manager = module.EmbeddedServerManager(
        _FakeHass([_peer_entry()]),
        SimpleNamespace(data={}, options={}),
    )

    with pytest.raises(module.EmbeddedServerError) as exc:
        _run(manager._async_ensure_package())

    assert "has not published its package bring-up task" in str(exc.value)
    assert process_calls == []


def test_enabled_ha_mcp_server_task_is_shielded_and_then_reused(
    monkeypatch: Any,
) -> None:
    """ESPHome observes HA-MCP package completion without owning its task."""
    module = _load_embedded_server(monkeypatch)
    _configure_runtime(
        monkeypatch,
        module,
        peer_requirements={"ha-mcp-dev": module.HA_MCP_SERVER_REQUIREMENTS},
    )
    hass = _FakeHass([_peer_entry()])
    manager = module.EmbeddedServerManager(
        hass,
        SimpleNamespace(data={}, options={}),
    )

    async def scenario() -> None:
        bringup_task = asyncio.create_task(asyncio.sleep(0))
        hass.data[module.HA_MCP_DOMAIN] = {
            module.HA_MCP_BRINGUP_TASK_KEY: bringup_task
        }
        await manager._async_ensure_package()
        assert not bringup_task.cancelled()

    _run(scenario())
    assert manager.fastmcp_version == "3.4.7"


def test_loaded_runtime_must_match_installed_version(monkeypatch: Any) -> None:
    """Cached old FastMCP code is never mixed with newer installed files."""
    module = _load_embedded_server(monkeypatch)
    _configure_runtime(
        monkeypatch,
        module,
        loaded=True,
        loaded_version="3.4.6",
    )
    manager = module.EmbeddedServerManager(
        _FakeHass(),
        SimpleNamespace(data={}, options={}),
    )

    with pytest.raises(module.EmbeddedServerError) as exc:
        _run(manager._async_ensure_package())

    assert exc.value.kind == "restart"
    assert "loaded FastMCP 3.4.6" in str(exc.value)
    assert "installed FastMCP 3.4.7" in str(exc.value)


def test_loaded_runtime_is_never_replaced_for_contract_violation(monkeypatch: Any) -> None:
    """A live shared runtime mismatch requests restart without calling pip."""
    process_calls: list[list[str]] = []

    async def async_process_requirements(
        _hass: Any,
        _label: str,
        requirements: list[str],
        **_kwargs: Any,
    ) -> None:
        process_calls.append(requirements)

    module = _load_embedded_server(
        monkeypatch,
        async_process_requirements=async_process_requirements,
    )
    _configure_runtime(
        monkeypatch,
        module,
        loaded=True,
        violations=("mcp 1.14.1 does not satisfy mcp>=1.24",),
    )
    manager = module.EmbeddedServerManager(
        _FakeHass(),
        SimpleNamespace(data={}, options={}),
    )

    with pytest.raises(module.EmbeddedServerError) as exc:
        _run(manager._async_ensure_package())

    assert exc.value.kind == "restart"
    assert "mcp 1.14.1" in str(exc.value)
    assert process_calls == []


def test_runtime_graph_audit_follows_requested_extras(monkeypatch: Any) -> None:
    """The httpx[socks] contract also validates its extra-only dependency."""
    module = _load_embedded_server(monkeypatch)
    monkeypatch.setattr(
        module,
        "HA_MCP_SERVER_REQUIREMENTS",
        ("httpx[socks]==0.28.1",),
    )
    distributions = {
        "httpx": SimpleNamespace(
            version="0.28.1",
            requires=('socksio==1.0.0; extra == "socks"',),
        )
    }

    def distribution(name: str) -> Any:
        try:
            return distributions[name]
        except KeyError as err:
            raise module.metadata.PackageNotFoundError(name) from err

    monkeypatch.setattr(module.metadata, "distribution", distribution)

    assert module._unsatisfied_runtime_requirements() == (
        "socksio is missing (required by httpx 0.28.1)",
    )


def test_loaded_fastmcp_fingerprint_reads_cached_module(monkeypatch: Any) -> None:
    """The loaded generation is observed without importing or reloading it."""
    module = _load_embedded_server(monkeypatch)
    fastmcp_module = ModuleType("fastmcp")
    fastmcp_module.__version__ = "3.4.7"
    fastmcp_module.__file__ = "/config/deps/fastmcp/__init__.py"
    monkeypatch.setitem(sys.modules, "fastmcp", fastmcp_module)

    assert module._loaded_fastmcp_fingerprint() == (
        "3.4.7",
        "/config/deps/fastmcp/__init__.py",
    )


def test_installed_fastmcp_origin_comes_from_owning_distribution(monkeypatch: Any) -> None:
    """Package provenance follows the distribution that owns the import tree."""
    module = _load_embedded_server(monkeypatch)
    distribution = SimpleNamespace(
        files=(Path("fastmcp/__init__.py"),),
        locate_file=lambda installed_file: Path("/config/deps") / installed_file,
    )

    def get_distribution(name: str) -> Any:
        if name == "fastmcp-slim":
            return distribution
        raise module.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(module.metadata, "distribution", get_distribution)

    assert module._installed_fastmcp_origin() == "/config/deps/fastmcp/__init__.py"


def test_dependency_probe_does_not_import_runtime_packages(monkeypatch: Any) -> None:
    """Import checks do not cache FastMCP modules before installation."""
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
                raise RuntimeError(
                    "deadlock detected by _ModuleLock('fastmcp.server.server')"
                )
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
