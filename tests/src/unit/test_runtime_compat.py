"""Compatibility dependency setup without changing core-supplied libraries."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
COMPAT = ROOT / "custom_components/esphome_mcp/runtime_compat.py"


def _module() -> ModuleType:
    assert COMPAT.exists(), "HA httpx2 ownership compatibility is missing"
    spec = importlib.util.spec_from_file_location("esphome_runtime_compat", COMPAT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "installed, install", [("2.14.0", False), ("2.10.0", False), (None, True), ("2.5.0", True)]
)
def test_httpx2_is_installed_only_when_the_runtime_needs_it(
    monkeypatch: pytest.MonkeyPatch, installed: str | None, install: bool
) -> None:
    module = _module()

    def version(name: str) -> str:
        assert name == "httpx2"
        if installed is None:
            raise module.PackageNotFoundError(name)
        return installed

    monkeypatch.setattr(module, "version", version)
    requirements = ModuleType("homeassistant.requirements")
    installer = AsyncMock()
    requirements.async_process_requirements = installer
    monkeypatch.setitem(sys.modules, "homeassistant.requirements", requirements)

    async def executor(function: object) -> object:
        return function()

    hass = SimpleNamespace(async_add_executor_job=executor)
    asyncio.run(module.async_ensure_runtime_compat(hass))
    if install:
        installer.assert_awaited_once_with(hass, "esphome_mcp", ["httpx2>=2.10.0"])
    else:
        installer.assert_not_awaited()


def test_manifest_leaves_core_httpx2_ownership_to_ha() -> None:
    requirements = json.loads((ROOT / "custom_components/esphome_mcp/manifest.json").read_text())[
        "requirements"
    ]
    assert not any(requirement.startswith("httpx2") for requirement in requirements)


def test_runtime_compat_matches_vendored_httpx2_minimum() -> None:
    """A vendored runtime upgrade cannot silently outgrow the old-Core installer."""
    from .test_vendored_fastmcp import vendor_fastmcp

    clauses = set()
    for requirements in (ROOT / "custom_components/esphome_mcp/_vendor").glob("*/REQUIRES.txt"):
        for requirement in requirements.read_text().splitlines():
            if requirement.startswith("httpx2"):
                clauses.update(requirement.removeprefix("httpx2").split(","))
    assert _module().HTTPX2_REQUIREMENT == "httpx2" + ",".join(
        vendor_fastmcp._merge_clauses(clauses)
    )


def test_component_setup_checks_compatibility_before_starting_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing compatibility dependencies cannot fail later in the worker thread."""
    events = []

    async def ensure(_hass: object) -> None:
        events.append("compat")

    async def start(_hass: object, _entry: object) -> bool:
        events.append("server")
        return True

    compat = ModuleType("runtime_setup_fixture.runtime_compat")
    compat.async_ensure_runtime_compat = ensure
    embedded = ModuleType("runtime_setup_fixture.embedded_entry")
    embedded.async_setup_server_entry = start
    monkeypatch.setitem(sys.modules, "runtime_setup_fixture.runtime_compat", compat)
    monkeypatch.setitem(sys.modules, "runtime_setup_fixture.embedded_entry", embedded)
    spec = importlib.util.spec_from_file_location(
        "runtime_setup_fixture",
        COMPAT.parent / "__init__.py",
        submodule_search_locations=[str(COMPAT.parent)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "runtime_setup_fixture", module)
    spec.loader.exec_module(module)
    assert asyncio.run(module.async_setup_entry(object(), object())) is True
    assert events == ["compat", "server"]
