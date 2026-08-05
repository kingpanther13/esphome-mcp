"""Tests for the shared runtime-dependency sandbox lint."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = ROOT / "scripts" / "check_runtime_dependency_sandbox.py"

HA_MCP_RUNTIME_REQUIREMENTS = [
    "fastmcp==3.4.5",
    "httpx[socks]==0.28.1",
    "pydantic==2.13.4",
    "python-dotenv==1.2.2",
    "truststore==0.10.4",
    "websockets>=15.0.1,<18",
    "cryptography>=48.0.0,<51",
    "pydantic-monty==0.0.18",
    "tzdata>=2024.1",
    "packaging>=24.0",
]


def _load_sandbox() -> ModuleType:
    spec = importlib.util.spec_from_file_location("runtime_dependency_sandbox", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repository_runtime_passes_dependency_sandbox() -> None:
    """Production component code neither evicts nor reloads shared modules."""
    sandbox = _load_sandbox()

    assert sandbox.validate_runtime_tree() == []
    assert sandbox.validate_runtime_constants() == []
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
    """Runtime code may inspect module state without mutating another integration."""
    sandbox = _load_sandbox()
    runtime_file = tmp_path / "runtime.py"
    runtime_file.write_text("import sys\nloaded = 'fastmcp' in sys.modules\n")

    assert sandbox.validate_runtime_source(runtime_file) == []


def _write_upstream_pyproject(path: Path, dependencies: list[str]) -> None:
    quoted = ", ".join(repr(dependency) for dependency in dependencies)
    path.write_text(f"[project]\nname = 'ha-mcp'\ndependencies = [{quoted}]\n")


def test_ha_mcp_shared_requirement_parity(tmp_path: Path) -> None:
    """The compatibility gate accepts the complete shared dependency set."""
    sandbox = _load_sandbox()
    upstream = tmp_path / "pyproject.toml"
    _write_upstream_pyproject(upstream, HA_MCP_RUNTIME_REQUIREMENTS)

    assert sandbox.validate_ha_mcp_shared_requirements(upstream) == []


def test_ha_mcp_shared_requirement_mismatch_fails(tmp_path: Path) -> None:
    """A changed ha-mcp dependency blocks release until its spec is aligned."""
    sandbox = _load_sandbox()
    upstream = tmp_path / "pyproject.toml"
    requirements = HA_MCP_RUNTIME_REQUIREMENTS.copy()
    requirements[2] = "pydantic==9.9.9"
    _write_upstream_pyproject(upstream, requirements)

    errors = sandbox.validate_ha_mcp_shared_requirements(upstream)

    assert errors == [
        "shared runtime dependency mismatch for pydantic: "
        "ESPHome MCP uses 'pydantic==2.13.4', ha-mcp uses 'pydantic==9.9.9'"
    ]


def test_ha_mcp_websockets_spec_is_excluded_as_ha_owned(
    tmp_path: Path,
) -> None:
    """ha-mcp may constrain websockets without making ESPHome install it."""
    sandbox = _load_sandbox()
    upstream = tmp_path / "pyproject.toml"
    requirements = HA_MCP_RUNTIME_REQUIREMENTS.copy()
    requirements[5] = "websockets==17.0"
    _write_upstream_pyproject(upstream, requirements)

    assert sandbox.validate_ha_mcp_shared_requirements(upstream) == []


def test_ha_mcp_added_dependency_fails(tmp_path: Path) -> None:
    """A new ha-mcp direct dependency must be mirrored by ESPHome MCP."""
    sandbox = _load_sandbox()
    upstream = tmp_path / "pyproject.toml"
    _write_upstream_pyproject(upstream, [*HA_MCP_RUNTIME_REQUIREMENTS, "new-shared==1.0"])

    assert sandbox.validate_ha_mcp_shared_requirements(upstream) == [
        "ESPHome MCP is missing ha-mcp runtime dependency 'new-shared'"
    ]


def test_ha_mcp_removed_dependency_fails(tmp_path: Path) -> None:
    """ESPHome MCP cannot retain a direct requirement removed from ha-mcp."""
    sandbox = _load_sandbox()
    upstream = tmp_path / "pyproject.toml"
    requirements = [
        requirement
        for requirement in HA_MCP_RUNTIME_REQUIREMENTS
        if not requirement.startswith("truststore")
    ]
    _write_upstream_pyproject(upstream, requirements)

    assert sandbox.validate_ha_mcp_shared_requirements(upstream) == [
        "ha-mcp is missing shared runtime dependency 'truststore'"
    ]


def test_runtime_constants_reject_stable_ha_mcp_release_ref(tmp_path: Path) -> None:
    """Compatibility cannot silently drift back from ha-mcp master to a release tag."""
    sandbox = _load_sandbox()
    const = tmp_path / "const.py"
    const.write_text(
        'DEFAULT_PIP_SPEC = "fastmcp==3.4.5"\n'
        'HA_MCP_COMPAT_REF = "v7.12.3"\n'
        'HA_OWNED_RUNTIME_REQUIREMENTS = ("websockets",)\n'
        "SHARED_RUNTIME_REQUIREMENTS = (DEFAULT_PIP_SPEC,)\n"
    )

    assert sandbox.validate_runtime_constants(const) == ["HA_MCP_COMPAT_REF must be 'master'"]


def test_runtime_constants_reject_esphome_websockets_requirement(tmp_path: Path) -> None:
    """ESPHome cannot reintroduce a direct websocket install over HA Core's copy."""
    sandbox = _load_sandbox()
    const = tmp_path / "const.py"
    const.write_text(
        'DEFAULT_PIP_SPEC = "fastmcp==3.4.5"\n'
        'HA_MCP_COMPAT_REF = "master"\n'
        'HA_OWNED_RUNTIME_REQUIREMENTS = ("websockets",)\n'
        "SHARED_RUNTIME_REQUIREMENTS = ("
        '"websockets>=15.0.1,<18", DEFAULT_PIP_SPEC)\n'
    )

    assert sandbox.validate_runtime_constants(const) == [
        "websockets is HA-owned and must not be installed by ESPHome MCP"
    ]


def test_worker_import_retry_cannot_be_bypassed(tmp_path: Path) -> None:
    """The sandbox fails if the worker starts serving without safe preloading."""
    sandbox = _load_sandbox()
    embedded_server = tmp_path / "embedded_server.py"
    embedded_server.write_text(
        "class EmbeddedServerManager:\n    def _thread_main(self):\n        self._serve()\n"
    )

    assert sandbox.validate_worker_import_contract(embedded_server) == [
        "worker thread must call _import_server_runtime_with_retry"
    ]


def test_install_contract_rejects_direct_package_install(tmp_path: Path) -> None:
    """Shared package installs cannot bypass HA's requirements manager."""
    sandbox = _load_sandbox()
    embedded_server = tmp_path / "embedded_server.py"
    embedded_server.write_text(
        "from functools import partial\n"
        "async def install(hass):\n"
        "    partial(install_package, 'fastmcp==3.4.5', upgrade=True)\n"
    )

    assert sandbox.validate_install_contract(embedded_server) == [
        "embedded dependency install at line 3 bypasses HA's requirements manager",
        "embedded dependency install must use HA async_process_requirements "
        "with SHARED_RUNTIME_REQUIREMENTS",
    ]


def test_install_contract_accepts_ha_requirements_manager(tmp_path: Path) -> None:
    """The supported install shape delegates locking and constraints to HA."""
    sandbox = _load_sandbox()
    embedded_server = tmp_path / "embedded_server.py"
    embedded_server.write_text(
        "async def install(hass):\n"
        "    await async_process_requirements(\n"
        "        hass, 'ESPHome MCP', list(SHARED_RUNTIME_REQUIREMENTS)\n"
        "    )\n"
    )

    assert sandbox.validate_install_contract(embedded_server) == []
