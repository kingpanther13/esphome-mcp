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


@pytest.mark.parametrize("installed, install", [("2.14.0", False), ("2.10.0", False),
                                                (None, True), ("2.5.0", True)])
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
