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

    entry = SimpleNamespace(
        data={module.DATA_LAST_PIP_SPEC: module.DEFAULT_PIP_SPEC},
        options={"pip_spec": "fastmcp==0.0.1"},
    )
    manager = module.EmbeddedServerManager(_FakeHass(), entry)

    _run(manager._async_ensure_package())

    assert process_calls == [
        (
            "ESPHome MCP server (fastmcp==3.4.2)",
            ["fastmcp==3.4.2"],
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

    hass = _FakeHass()
    entry = SimpleNamespace(data={}, options={"pip_spec": "fastmcp==0.0.1"})
    manager = module.EmbeddedServerManager(hass, entry)

    _run(manager._async_ensure_package())

    assert install_calls == [
        (
            "fastmcp==3.4.2",
            True,
            {"config_dir": "/config", "timeout": 300},
        )
    ]
    assert hass.config_entries.updated == {module.DATA_LAST_PIP_SPEC: module.DEFAULT_PIP_SPEC}


def test_changed_code_pin_forces_install_even_when_importable(monkeypatch: Any) -> None:
    """A code-side FastMCP pin change bypasses HA's already-importable shortcut."""
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
    monkeypatch.setitem(sys.modules, "fastmcp", ModuleType("fastmcp"))
    monkeypatch.setitem(sys.modules, "fastmcp.server", ModuleType("fastmcp.server"))
    monkeypatch.setitem(
        sys.modules,
        "custom_components.esphome_mcp.server",
        ModuleType("custom_components.esphome_mcp.server"),
    )

    hass = _FakeHass()
    entry = SimpleNamespace(data={module.DATA_LAST_PIP_SPEC: "fastmcp==0.0.1"}, options={})
    manager = module.EmbeddedServerManager(hass, entry)

    _run(manager._async_ensure_package())

    assert process_calls == []
    assert install_calls == [
        (
            "fastmcp==3.4.2",
            True,
            {"config_dir": "/config", "timeout": 300},
        )
    ]
    assert hass.config_entries.updated == {module.DATA_LAST_PIP_SPEC: module.DEFAULT_PIP_SPEC}
    assert "fastmcp" not in sys.modules
    assert "fastmcp.server" not in sys.modules
    assert "custom_components.esphome_mcp.server" not in sys.modules


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

    entry = SimpleNamespace(
        data={module.DATA_LAST_PIP_SPEC: module.DEFAULT_PIP_SPEC},
        options={},
    )
    manager = module.EmbeddedServerManager(_FakeHass(), entry)

    try:
        _run(manager._async_ensure_package())
    except module.EmbeddedServerError as err:
        assert err.kind == "package"
        assert "fastmcp==3.4.2" in str(err)
    else:
        raise AssertionError("EmbeddedServerError was not raised")
