"""Tests for the Renovate-driven HA-MCP master contract generator."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = ROOT / "scripts" / "sync_ha_mcp_runtime_contract.py"
SHA = "b" * 40
PYPROJECT = """\
[project]
name = "ha-mcp"
version = "9.0.0"
dependencies = [
  "fastmcp==3.5.0",
  "httpx[socks]==0.29.0",
]
"""
MANIFEST = """\
{
  "domain": "ha_mcp_tools",
  "version": "2.1.0",
  "requirements": ["ruamel.yaml>=0.19.0"]
}
"""
CONST = 'COMPONENT_VERSION = "2.1.0"\n'


def _load_sync() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ha_mcp_runtime_sync", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stub_upstream(monkeypatch: pytest.MonkeyPatch, module: ModuleType) -> None:
    """Provide one deterministic upstream source snapshot."""
    sources = {
        module.PYPROJECT_PATH: PYPROJECT,
        module.MANIFEST_PATH: MANIFEST,
        module.CONST_PATH: CONST,
    }
    monkeypatch.setattr(module, "_resolve_commit", lambda _ref: SHA)
    monkeypatch.setattr(module, "_read_source", lambda path, _sha: sources[path])


def test_generator_mirrors_server_and_component_from_one_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One generated module contains both upstream dependency surfaces."""
    module = _load_sync()
    _stub_upstream(monkeypatch, module)

    rendered = module._generate("master")

    assert f'HA_MCP_MASTER_SHA = "{SHA}"' in rendered
    assert 'HA_MCP_SERVER_VERSION = "9.0.0"' in rendered
    assert 'HA_MCP_COMPONENT_VERSION = "2.1.0"' in rendered
    assert '"fastmcp==3.5.0",' in rendered
    assert '"httpx[socks]==0.29.0",' in rendered
    assert '"ruamel.yaml>=0.19.0",' in rendered
    assert 'HA_MCP_FASTMCP_REQUIREMENT = "fastmcp==3.5.0"' in rendered


def test_generator_rejects_component_version_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manifest and component constant must move in the same upstream commit."""
    module = _load_sync()
    _stub_upstream(monkeypatch, module)
    monkeypatch.setattr(
        module,
        "_read_source",
        lambda path, _sha: (
            'COMPONENT_VERSION = "2.0.9"\n'
            if path == module.CONST_PATH
            else {
                module.PYPROJECT_PATH: PYPROJECT,
                module.MANIFEST_PATH: MANIFEST,
            }[path]
        ),
    )

    with pytest.raises(RuntimeError, match="component version drift"):
        module._generate("master")


def test_contract_ref_check_never_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """CI verifies the committed generated file without modifying it."""
    module = _load_sync()
    _stub_upstream(monkeypatch, module)
    contract = tmp_path / "contract.py"
    contract.write_text(f'HA_MCP_MASTER_SHA = "{SHA}"\n')
    monkeypatch.setattr(module, "CONTRACT_PATH", contract)

    assert module.main(["--contract-ref", "--check"]) == 1
    assert contract.read_text() == f'HA_MCP_MASTER_SHA = "{SHA}"\n'


def test_generator_requires_fastmcp_in_server_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An upstream packaging-policy change requires an explicit design update."""
    module = _load_sync()
    _stub_upstream(monkeypatch, module)
    monkeypatch.setattr(
        module,
        "_read_source",
        lambda path, _sha: (
            PYPROJECT.replace('"fastmcp==3.5.0",\n', "")
            if path == module.PYPROJECT_PATH
            else {
                module.MANIFEST_PATH: MANIFEST,
                module.CONST_PATH: CONST,
            }[path]
        ),
    )

    with pytest.raises(RuntimeError, match="does not declare a FastMCP"):
        module._generate("master")
