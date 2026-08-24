"""Tests for the HA-MCP master runtime sandbox lint."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = ROOT / "scripts" / "check_runtime_dependency_sandbox.py"


def _load_sandbox() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "runtime_dependency_sandbox",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repository_runtime_passes_dependency_sandbox() -> None:
    """Production source preserves the generated lockstep contract."""
    sandbox = _load_sandbox()

    assert sandbox.validate_runtime_tree() == []
    assert sandbox.validate_runtime_contract() == []
    assert sandbox.validate_worker_import_contract() == []
    assert sandbox.validate_install_contract() == []


@pytest.mark.parametrize(
    "source",
    [
        "import sys\nsys.modules.pop('fastmcp', None)\n",
        "from sys import modules as cache\ndel cache['fastmcp']\n",
        "import sys as system\ncache = system.modules\ncache.clear()\n",
        "from sys import modules as cache\ncache |= {'fastmcp': object()}\n",
        "import sys\nsys.modules['fastmcp'] = object()\n",
        "import importlib as loader\nloader.reload(object())\n",
        "from importlib import reload as refresh\nrefresh(object())\n",
    ],
)
def test_sandbox_rejects_shared_module_cache_mutation(
    tmp_path: Path,
    source: str,
) -> None:
    """Direct and aliased process-global mutation cannot bypass the lint."""
    sandbox = _load_sandbox()
    runtime_file = tmp_path / "runtime.py"
    runtime_file.write_text(source)

    errors = sandbox.validate_runtime_source(runtime_file)

    assert len(errors) == 1
    assert "forbidden runtime dependency mutation" in errors[0]


def test_sandbox_allows_read_only_shared_module_detection(tmp_path: Path) -> None:
    """Runtime code may inspect shared module state without mutating it."""
    sandbox = _load_sandbox()
    runtime_file = tmp_path / "runtime.py"
    runtime_file.write_text("import sys\nloaded = 'fastmcp' in sys.modules\n")

    assert sandbox.validate_runtime_source(runtime_file) == []


def _write_contract(
    path: Path,
    *,
    repository: str = "homeassistant-ai/ha-mcp",
    sha: str = "a" * 40,
    fastmcp: str = "fastmcp==3.4.7",
    server_requirements: str = '("fastmcp==3.4.7", "httpx==0.28.1")',
    component_requirements: str = '("ruamel.yaml>=0.18.0",)',
) -> None:
    path.write_text(
        f'HA_MCP_REPOSITORY = "{repository}"\n'
        f'HA_MCP_MASTER_SHA = "{sha}"\n'
        'HA_MCP_SERVER_VERSION = "8.3.0"\n'
        'HA_MCP_COMPONENT_VERSION = "2.0.1"\n'
        f"HA_MCP_SERVER_REQUIREMENTS = {server_requirements}\n"
        f"HA_MCP_COMPONENT_REQUIREMENTS = {component_requirements}\n"
        f'HA_MCP_FASTMCP_REQUIREMENT = "{fastmcp}"\n'
    )


def test_contract_requires_an_immutable_commit(tmp_path: Path) -> None:
    """A moving branch name cannot masquerade as the mirrored snapshot."""
    sandbox = _load_sandbox()
    contract = tmp_path / "contract.py"
    _write_contract(contract, sha="master")

    assert sandbox.validate_runtime_contract(contract) == [
        "HA_MCP_MASTER_SHA must be one immutable 40-character SHA"
    ]


def test_contract_requires_both_component_and_server_metadata(tmp_path: Path) -> None:
    """Component requirements cannot be omitted from a server-only update."""
    sandbox = _load_sandbox()
    contract = tmp_path / "contract.py"
    _write_contract(contract, component_requirements="None")

    assert sandbox.validate_runtime_contract(contract) == [
        "HA_MCP_COMPONENT_REQUIREMENTS must be a string tuple"
    ]


def test_contract_requires_exact_fastmcp_pin_inside_server_tuple(
    tmp_path: Path,
) -> None:
    """The reported FastMCP generation must be the mirrored direct dependency."""
    sandbox = _load_sandbox()
    contract = tmp_path / "contract.py"
    _write_contract(contract, fastmcp="fastmcp>=3.4.7,<4")

    assert sandbox.validate_runtime_contract(contract) == [
        "HA_MCP_FASTMCP_REQUIREMENT must be an exact FastMCP pin",
        "HA_MCP_FASTMCP_REQUIREMENT must be present in HA_MCP_SERVER_REQUIREMENTS",
    ]


def test_contract_rejects_duplicate_distributions(tmp_path: Path) -> None:
    """One generated contract cannot declare competing pins for a package."""
    sandbox = _load_sandbox()
    contract = tmp_path / "contract.py"
    _write_contract(
        contract,
        server_requirements='("fastmcp==3.4.7", "FastMCP>=3.4.0")',
    )

    assert sandbox.validate_runtime_contract(contract) == [
        "HA_MCP_SERVER_REQUIREMENTS contains duplicate distributions"
    ]


def test_worker_import_retry_cannot_be_bypassed(tmp_path: Path) -> None:
    """The sandbox fails if the worker serves without safe preloading."""
    sandbox = _load_sandbox()
    embedded_server = tmp_path / "embedded_server.py"
    embedded_server.write_text(
        "class EmbeddedServerManager:\n    def _thread_main(self):\n        self._serve()\n"
    )

    assert sandbox.validate_worker_import_contract(embedded_server) == [
        "worker thread must call _import_server_runtime_with_retry"
    ]


@pytest.mark.parametrize(
    "install_expression",
    [
        "partial(install_package, 'fastmcp==3.4.7', upgrade=True)",
        "await hass.async_add_executor_job(install_package, 'fastmcp==3.4.7')",
        "partial(func=install_package, requirement='fastmcp==3.4.7')",
        "installer.install_package('fastmcp==3.4.7')",
    ],
)
def test_install_contract_rejects_direct_package_install(
    tmp_path: Path,
    install_expression: str,
) -> None:
    """Any install_package reference bypasses Home Assistant's lock."""
    sandbox = _load_sandbox()
    embedded_server = tmp_path / "embedded_server.py"
    embedded_server.write_text(
        f"from functools import partial\nasync def install(hass):\n    {install_expression}\n"
    )

    assert sandbox.validate_install_contract(embedded_server) == [
        "embedded dependency install at line 3 bypasses HA's requirements manager",
        "embedded dependency install must use HA async_process_requirements "
        "with HA_MCP_SERVER_REQUIREMENTS",
    ]


def test_install_contract_accepts_generated_server_tuple(tmp_path: Path) -> None:
    """The supported install shape delegates locking to Home Assistant."""
    sandbox = _load_sandbox()
    embedded_server = tmp_path / "embedded_server.py"
    embedded_server.write_text(
        "async def install(hass):\n"
        "    await async_process_requirements(\n"
        "        hass, 'ESPHome MCP', list(HA_MCP_SERVER_REQUIREMENTS)\n"
        "    )\n"
    )

    assert sandbox.validate_install_contract(embedded_server) == []


def test_install_contract_checks_every_requirements_manager_call(
    tmp_path: Path,
) -> None:
    """One compliant call cannot mask another untracked dependency set."""
    sandbox = _load_sandbox()
    embedded_server = tmp_path / "embedded_server.py"
    embedded_server.write_text(
        "async def install(hass):\n"
        "    await async_process_requirements(\n"
        "        hass, 'ESPHome MCP', list(HA_MCP_SERVER_REQUIREMENTS)\n"
        "    )\n"
        "    await async_process_requirements(hass, 'other', OTHER_REQUIREMENTS)\n"
    )

    assert sandbox.validate_install_contract(embedded_server) == [
        "HA requirements-manager call at line 5 must use exactly HA_MCP_SERVER_REQUIREMENTS"
    ]
