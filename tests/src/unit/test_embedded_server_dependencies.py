"""Unit tests for ESPHome MCP runtime dependency handling."""

from __future__ import annotations

import asyncio
import builtins
import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[3]


def _install_homeassistant_stubs(
    monkeypatch: Any,
    *,
    async_process_requirements: Any | None = None,
    install_package: Any | None = None,
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
    req_mod.pip_kwargs = lambda config_dir: {
        "config_dir": config_dir,
        "timeout": 5,
    }

    util_mod = ModuleType("homeassistant.util")
    package_mod = ModuleType("homeassistant.util.package")
    package_mod.install_package = install_package or (lambda *_args, **_kwargs: True)

    monkeypatch.setitem(sys.modules, "homeassistant", ha_mod)
    monkeypatch.setitem(sys.modules, "homeassistant.config_entries", config_entries_mod)
    monkeypatch.setitem(sys.modules, "homeassistant.core", core_mod)
    monkeypatch.setitem(sys.modules, "homeassistant.requirements", req_mod)
    monkeypatch.setitem(sys.modules, "homeassistant.util", util_mod)
    monkeypatch.setitem(sys.modules, "homeassistant.util.package", package_mod)

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
    def __init__(self) -> None:
        self.updated: dict[str, Any] | None = None

    def async_update_entry(self, entry: Any, *, data: dict[str, Any]) -> None:
        self.updated = data
        entry.data = data


class _FakeHass:
    def __init__(self) -> None:
        self.config = _FakeConfig()
        self.config_entries = _FakeConfigEntries()

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        return func(*args)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_dependency_fast_path_uses_home_assistant_requirements(monkeypatch: Any) -> None:
    """An already-recorded importable dependency goes through HA's requirement manager."""
    process_calls: list[tuple[str, list[str], bool]] = []
    install_calls: list[tuple[Any, ...]] = []

    async def async_process_requirements(
        _hass: Any,
        label: str,
        requirements: list[str],
        *,
        is_built_in: bool,
    ) -> None:
        process_calls.append((label, requirements, is_built_in))

    def install_package(*args: Any, **_kwargs: Any) -> bool:
        install_calls.append(args)
        return True

    module = _load_embedded_server(
        monkeypatch,
        async_process_requirements=async_process_requirements,
        install_package=install_package,
    )
    monkeypatch.setattr(module, "_server_dependencies_importable", lambda: True)
    monkeypatch.setattr(module, "_installed_fastmcp_version", lambda: "3.4.4")
    monkeypatch.setattr(module, "_installed_peer_fastmcp_specs", lambda: {})
    monkeypatch.setattr(module, "_fastmcp_runtime_loaded", lambda: False)

    entry = SimpleNamespace(
        data={module.DATA_LAST_PIP_SPEC: module.DEFAULT_PIP_SPEC},
        options={"pip_spec": "fastmcp==0.0.1"},
    )
    manager = module.EmbeddedServerManager(_FakeHass(), entry)

    _run(manager._async_ensure_package())

    assert process_calls == [
        (
            "ESPHome MCP server (fastmcp==3.4.4)",
            ["fastmcp==3.4.4"],
            False,
        )
    ]
    assert install_calls == []


def test_missing_dependency_forces_install_of_pinned_requirement(monkeypatch: Any) -> None:
    """Missing dependencies force a real install of the pinned server requirement."""
    install_calls: list[tuple[str, bool, dict[str, Any]]] = []
    importable = iter([False, True])

    def install_package(spec: str, *, upgrade: bool, **kwargs: Any) -> bool:
        install_calls.append((spec, upgrade, kwargs))
        return True

    module = _load_embedded_server(monkeypatch, install_package=install_package)
    monkeypatch.setattr(module, "_server_dependencies_importable", lambda: next(importable))
    versions = iter([None, "3.4.4"])
    monkeypatch.setattr(module, "_installed_fastmcp_version", lambda: next(versions))
    monkeypatch.setattr(module, "_installed_peer_fastmcp_specs", lambda: {})
    monkeypatch.setattr(module, "_fastmcp_runtime_loaded", lambda: False)

    hass = _FakeHass()
    entry = SimpleNamespace(data={}, options={"pip_spec": "fastmcp==0.0.1"})
    manager = module.EmbeddedServerManager(hass, entry)

    _run(manager._async_ensure_package())

    assert install_calls == [
        (
            "fastmcp==3.4.4",
            True,
            {"config_dir": "/config", "timeout": 300},
        )
    ]
    assert hass.config_entries.updated == {module.DATA_LAST_PIP_SPEC: module.DEFAULT_PIP_SPEC}


def test_changed_code_pin_forces_install_when_runtime_is_not_loaded(
    monkeypatch: Any,
) -> None:
    """A code-side pin change installs safely before any shared module is loaded."""
    process_calls: list[tuple[str, list[str], bool]] = []
    install_calls: list[tuple[str, bool, dict[str, Any]]] = []

    async def async_process_requirements(
        _hass: Any,
        label: str,
        requirements: list[str],
        *,
        is_built_in: bool,
    ) -> None:
        process_calls.append((label, requirements, is_built_in))

    def install_package(spec: str, *, upgrade: bool, **kwargs: Any) -> bool:
        install_calls.append((spec, upgrade, kwargs))
        return True

    module = _load_embedded_server(
        monkeypatch,
        async_process_requirements=async_process_requirements,
        install_package=install_package,
    )
    monkeypatch.setattr(module, "_server_dependencies_importable", lambda: True)
    versions = iter(["3.4.3", "3.4.4"])
    monkeypatch.setattr(module, "_installed_fastmcp_version", lambda: next(versions))
    monkeypatch.setattr(module, "_installed_peer_fastmcp_specs", lambda: {})
    monkeypatch.setattr(module, "_fastmcp_runtime_loaded", lambda: False)

    hass = _FakeHass()
    entry = SimpleNamespace(data={module.DATA_LAST_PIP_SPEC: "fastmcp==0.0.1"}, options={})
    manager = module.EmbeddedServerManager(hass, entry)

    _run(manager._async_ensure_package())

    assert process_calls == []
    assert install_calls == [
        (
            "fastmcp==3.4.4",
            True,
            {"config_dir": "/config", "timeout": 300},
        )
    ]
    assert hass.config_entries.updated == {module.DATA_LAST_PIP_SPEC: module.DEFAULT_PIP_SPEC}


def test_matching_installed_pin_repairs_stale_marker_without_reinstall(
    monkeypatch: Any,
) -> None:
    """Matching on-disk FastMCP is reused even when the entry marker is stale."""
    process_calls: list[list[str]] = []
    install_calls: list[tuple[Any, ...]] = []

    async def async_process_requirements(
        _hass: Any,
        _label: str,
        requirements: list[str],
        *,
        is_built_in: bool,
    ) -> None:
        assert is_built_in is False
        process_calls.append(requirements)

    def install_package(*args: Any, **_kwargs: Any) -> bool:
        install_calls.append(args)
        return True

    module = _load_embedded_server(
        monkeypatch,
        async_process_requirements=async_process_requirements,
        install_package=install_package,
    )
    monkeypatch.setattr(module, "_server_dependencies_importable", lambda: True)
    monkeypatch.setattr(module, "_installed_fastmcp_version", lambda: "3.4.4")
    monkeypatch.setattr(module, "_installed_peer_fastmcp_specs", lambda: {})
    monkeypatch.setattr(module, "_fastmcp_runtime_loaded", lambda: True)

    hass = _FakeHass()
    entry = SimpleNamespace(data={module.DATA_LAST_PIP_SPEC: "fastmcp==3.4.3"}, options={})
    manager = module.EmbeddedServerManager(hass, entry)

    _run(manager._async_ensure_package())

    assert process_calls == [["fastmcp==3.4.4"]]
    assert install_calls == []
    assert hass.config_entries.updated == {module.DATA_LAST_PIP_SPEC: "fastmcp==3.4.4"}


def test_loaded_shared_fastmcp_mismatch_refuses_reinstall_and_preserves_modules(
    monkeypatch: Any,
) -> None:
    """A running FastMCP consumer is never evicted or overwritten in-process."""
    install_calls: list[tuple[Any, ...]] = []

    def install_package(*args: Any, **_kwargs: Any) -> bool:
        install_calls.append(args)
        return True

    module = _load_embedded_server(monkeypatch, install_package=install_package)
    monkeypatch.setattr(module, "_server_dependencies_importable", lambda: True)
    monkeypatch.setattr(module, "_installed_fastmcp_version", lambda: "3.4.2")
    monkeypatch.setattr(module, "_installed_peer_fastmcp_specs", lambda: {})
    fastmcp_module = ModuleType("fastmcp")
    fastmcp_server_module = ModuleType("fastmcp.server")
    monkeypatch.setitem(sys.modules, "fastmcp", fastmcp_module)
    monkeypatch.setitem(sys.modules, "fastmcp.server", fastmcp_server_module)

    hass = _FakeHass()
    entry = SimpleNamespace(data={module.DATA_LAST_PIP_SPEC: "fastmcp==3.4.2"}, options={})
    manager = module.EmbeddedServerManager(hass, entry)

    try:
        _run(manager._async_ensure_package())
    except module.EmbeddedServerError as err:
        assert err.kind == "restart"
        assert "Refusing to replace the shared runtime" in str(err)
        assert "restart Home Assistant" in str(err)
    else:
        raise AssertionError("EmbeddedServerError was not raised")

    assert install_calls == []
    assert sys.modules["fastmcp"] is fastmcp_module
    assert sys.modules["fastmcp.server"] is fastmcp_server_module
    assert hass.config_entries.updated is None


def test_mismatched_ha_mcp_requirement_refuses_cold_downgrade(monkeypatch: Any) -> None:
    """A peer package pin mismatch blocks pip even before FastMCP is imported."""
    install_calls: list[tuple[Any, ...]] = []

    def install_package(*args: Any, **_kwargs: Any) -> bool:
        install_calls.append(args)
        return True

    module = _load_embedded_server(monkeypatch, install_package=install_package)
    monkeypatch.setattr(module, "_server_dependencies_importable", lambda: True)
    monkeypatch.setattr(module, "_installed_fastmcp_version", lambda: "3.4.5")
    monkeypatch.setattr(
        module,
        "_installed_peer_fastmcp_specs",
        lambda: {"ha-mcp": "fastmcp==3.4.5"},
    )
    monkeypatch.setattr(module, "_fastmcp_runtime_loaded", lambda: False)

    hass = _FakeHass()
    entry = SimpleNamespace(data={}, options={})
    manager = module.EmbeddedServerManager(hass, entry)

    try:
        _run(manager._async_ensure_package())
    except module.EmbeddedServerError as err:
        assert err.kind == "restart"
        assert "ha-mcp requires fastmcp==3.4.5" in str(err)
        assert "Refusing to replace a peer integration's shared FastMCP dependency" in str(err)
    else:
        raise AssertionError("EmbeddedServerError was not raised")

    assert install_calls == []
    assert hass.config_entries.updated is None


def test_install_that_does_not_produce_required_version_fails_closed(
    monkeypatch: Any,
) -> None:
    """A successful pip exit cannot mask a stale or conflicting installed wheel."""
    module = _load_embedded_server(monkeypatch, install_package=lambda *_args, **_kwargs: True)
    monkeypatch.setattr(module, "_server_dependencies_importable", lambda: True)
    versions = iter([None, "3.4.2"])
    monkeypatch.setattr(module, "_installed_fastmcp_version", lambda: next(versions))
    monkeypatch.setattr(module, "_installed_peer_fastmcp_specs", lambda: {})
    monkeypatch.setattr(module, "_fastmcp_runtime_loaded", lambda: False)

    hass = _FakeHass()
    entry = SimpleNamespace(data={}, options={})
    manager = module.EmbeddedServerManager(hass, entry)

    try:
        _run(manager._async_ensure_package())
    except module.EmbeddedServerError as err:
        assert err.kind == "package"
        assert "version 3.4.2 does not match the required version 3.4.4" in str(err)
    else:
        raise AssertionError("EmbeddedServerError was not raised")

    assert hass.config_entries.updated is None


def test_runtime_rejects_non_exact_fastmcp_spec(monkeypatch: Any) -> None:
    """Defense in depth rejects an unpinned requirement even outside CI."""
    module = _load_embedded_server(monkeypatch)

    for spec in ("fastmcp", "fastmcp>=3.4.3", "fastmcp==3.4.3,<4"):
        try:
            module._pinned_fastmcp_version(spec)
        except module.EmbeddedServerError as err:
            assert err.kind == "package"
            assert "must be an exact FastMCP pin" in str(err)
        else:
            raise AssertionError(f"EmbeddedServerError was not raised for {spec!r}")


def test_dependency_probe_does_not_import_runtime_packages(monkeypatch: Any) -> None:
    """Import checks do not cache stale FastMCP modules before forced installs."""
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
    monkeypatch.setattr(module, "_server_dependencies_importable", lambda: True)
    monkeypatch.setattr(module, "_installed_fastmcp_version", lambda: "3.4.4")
    monkeypatch.setattr(module, "_installed_peer_fastmcp_specs", lambda: {})
    monkeypatch.setattr(module, "_fastmcp_runtime_loaded", lambda: False)

    entry = SimpleNamespace(
        data={module.DATA_LAST_PIP_SPEC: module.DEFAULT_PIP_SPEC},
        options={},
    )
    manager = module.EmbeddedServerManager(_FakeHass(), entry)

    try:
        _run(manager._async_ensure_package())
    except module.EmbeddedServerError as err:
        assert err.kind == "package"
        assert "fastmcp==3.4.4" in str(err)
    else:
        raise AssertionError("EmbeddedServerError was not raised")


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

    try:
        module._import_server_runtime_with_retry()
    except RuntimeError as err:
        assert err is failure
    else:
        raise AssertionError("RuntimeError was not raised")

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

    try:
        module._import_server_runtime_with_retry()
    except module.EmbeddedServerError as err:
        assert err.kind == "restart"
        assert "repeatedly collided" in str(err)
        assert isinstance(err.__cause__, RuntimeError)
    else:
        raise AssertionError("EmbeddedServerError was not raised")

    assert sleeps == [0.1, 0.2]


def test_wrapped_module_lock_deadlock_is_detected(monkeypatch: Any) -> None:
    """Import wrappers cannot hide the retryable deadlock in an exception chain."""
    module = _load_embedded_server(monkeypatch)
    deadlock = RuntimeError("deadlock detected by _ModuleLock('fastmcp.server.context')")
    wrapper = RuntimeError("FastMCP import failed")
    wrapper.__cause__ = deadlock

    assert module._is_module_lock_deadlock(wrapper) is True
