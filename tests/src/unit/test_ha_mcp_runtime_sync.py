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


def test_generator_accepts_python_specific_requirements() -> None:
    """The same package can have different constraints on different Python versions."""
    module = _load_sync()
    requirements = [
        "anyio>=4.10; python_version >= '3.14'",
        "anyio>=4.9; python_version < '3.14'",
    ]
    assert module._string_list(requirements, label="server") == tuple(requirements)


def test_generator_reads_vendored_runtime_from_same_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vendored runtime must produce an installable immutable package reference."""
    module = _load_sync()
    sources = {
        module.PYPROJECT_PATH: PYPROJECT.replace('"fastmcp==3.5.0",', '"uvicorn>=0.35",'),
        module.MANIFEST_PATH: MANIFEST,
        module.CONST_PATH: CONST,
        "src/ha_mcp/_vendor/fastmcp/__init__.py": '__version__ = "4.0.3"\n',
        **{
            f"src/ha_mcp/_vendor/{name}/MANIFEST.sha256": "abc  __init__.py\n"
            for name in ("fastmcp", "mcp", "mcp_types", "websockets")
        },
    }
    requested = []

    def read_source(path: str, sha: str) -> str:
        requested.append((path, sha))
        return sources[path]

    monkeypatch.setattr(module, "_resolve_commit", lambda _ref: SHA)
    monkeypatch.setattr(module, "_read_source", read_source)
    rendered = module._generate("master")
    assert 'HA_MCP_FASTMCP_VERSION = "4.0.3"' in rendered
    assert f'ha-mcp @ https://github.com/homeassistant-ai/ha-mcp/archive/{SHA}.zip' in rendered
    assert 'HA_MCP_FASTMCP_MODULE = "ha_mcp._vendor.fastmcp"' in rendered
    assert all(sha == SHA for _, sha in requested)
    assert {path for path, _ in requested} == set(sources)


@pytest.mark.parametrize(
    "requirements",
    [
        ["anyio>=4.9", "anyio>=4.10"],
        ["anyio>=4.9; python_version >= '3.13'", "anyio>=4.10; python_version >= '3.14'"],
        ["anyio>=4.9; python_version < '3.14'", 'AnyIO>=4.10; python_version < "3.14"'],
    ],
)
def test_generator_rejects_overlapping_requirements(requirements: list[str]) -> None:
    """Allowing conditional dependencies must not admit competing declarations."""
    with pytest.raises(RuntimeError, match="more than once"):
        _load_sync()._string_list(requirements, label="server")
