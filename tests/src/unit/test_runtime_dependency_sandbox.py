"""Tests for the shared runtime-dependency sandbox lint."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = ROOT / "scripts" / "check_runtime_dependency_sandbox.py"


def _load_sandbox() -> ModuleType:
    spec = importlib.util.spec_from_file_location("runtime_dependency_sandbox", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repository_runtime_passes_dependency_sandbox() -> None:
    """Production component code preserves the peer-owned runtime contract."""
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


def _write_upstream_pyproject(path: Path, fastmcp_requirement: str | None) -> None:
    dependencies = (
        ["httpx==0.28.1"] if fastmcp_requirement is None else [fastmcp_requirement, "httpx==0.28.1"]
    )
    quoted = ", ".join(repr(dependency) for dependency in dependencies)
    path.write_text(f"[project]\nname = 'ha-mcp'\ndependencies = [{quoted}]\n")


@pytest.mark.parametrize("version", ["3.4.6", "3.4.7"])
def test_ha_mcp_supported_patch_pins_are_compatible(
    tmp_path: Path,
    version: str,
) -> None:
    """Previous and current HA-MCP pins fit the standalone compatibility range."""
    sandbox = _load_sandbox()
    upstream = tmp_path / "pyproject.toml"
    _write_upstream_pyproject(upstream, f"fastmcp=={version}")

    assert sandbox.validate_ha_mcp_fastmcp_compatibility(upstream) == []


@pytest.mark.parametrize("version", ["3.4.4", "4.0.0"])
def test_ha_mcp_pin_outside_supported_range_fails(
    tmp_path: Path,
    version: str,
) -> None:
    """CI blocks HA-MCP pins that ESPHome MCP has not declared compatible."""
    sandbox = _load_sandbox()
    upstream = tmp_path / "pyproject.toml"
    _write_upstream_pyproject(upstream, f"fastmcp=={version}")

    assert sandbox.validate_ha_mcp_fastmcp_compatibility(upstream) == [
        f"ha-mcp FastMCP pin {version} is outside ESPHome MCP supported range fastmcp>=3.4.5,<4"
    ]


def test_ha_mcp_missing_fastmcp_requirement_fails(tmp_path: Path) -> None:
    """CI requires an explicit upstream owner contract."""
    sandbox = _load_sandbox()
    upstream = tmp_path / "pyproject.toml"
    _write_upstream_pyproject(upstream, None)

    assert sandbox.validate_ha_mcp_fastmcp_compatibility(upstream) == [
        "ha-mcp does not declare a FastMCP runtime dependency"
    ]


def test_ha_mcp_non_exact_fastmcp_requirement_requires_checker_update(
    tmp_path: Path,
) -> None:
    """An upstream policy change cannot silently bypass compatibility validation."""
    sandbox = _load_sandbox()
    upstream = tmp_path / "pyproject.toml"
    _write_upstream_pyproject(upstream, "fastmcp>=3.4.5,<4")

    assert sandbox.validate_ha_mcp_fastmcp_compatibility(upstream) == [
        "ha-mcp FastMCP requirement must be an exact pin for compatibility "
        "validation: 'fastmcp>=3.4.5,<4'"
    ]


def _write_runtime_constants(
    path: Path,
    *,
    spec: str = "fastmcp>=3.4.5,<4",
    requirements: str = "(STANDALONE_FASTMCP_SPEC,)",
    compat_ref: str = "master",
) -> None:
    path.write_text(
        f'STANDALONE_FASTMCP_SPEC = "{spec}"\n'
        f"STANDALONE_RUNTIME_REQUIREMENTS = {requirements}\n"
        f'HA_MCP_COMPAT_REF = "{compat_ref}"\n'
    )


def test_runtime_constants_reject_exact_pin(tmp_path: Path) -> None:
    """Standalone policy must not recreate HA-MCP lockstep patch releases."""
    sandbox = _load_sandbox()
    const = tmp_path / "const.py"
    _write_runtime_constants(const, spec="fastmcp==3.4.7")

    assert sandbox.validate_runtime_constants(const) == [
        "STANDALONE_FASTMCP_SPEC must be bounded as fastmcp>=X.Y.Z,<N"
    ]


def test_runtime_constants_reject_unbounded_range(tmp_path: Path) -> None:
    """Standalone compatibility needs an explicit future-major ceiling."""
    sandbox = _load_sandbox()
    const = tmp_path / "const.py"
    _write_runtime_constants(const, spec="fastmcp>=3.4.5")

    assert sandbox.validate_runtime_constants(const) == [
        "STANDALONE_FASTMCP_SPEC must be bounded as fastmcp>=X.Y.Z,<N"
    ]


@pytest.mark.parametrize(
    "requirements",
    [
        '("uvicorn>=0.35", STANDALONE_FASTMCP_SPEC)',
        '("websockets>=15", STANDALONE_FASTMCP_SPEC)',
    ],
)
def test_runtime_constants_reject_mirrored_or_ha_owned_requirements(
    tmp_path: Path,
    requirements: str,
) -> None:
    """ESPHome MCP installs only FastMCP in standalone mode."""
    sandbox = _load_sandbox()
    const = tmp_path / "const.py"
    _write_runtime_constants(const, requirements=requirements)

    assert sandbox.validate_runtime_constants(const) == [
        "STANDALONE_RUNTIME_REQUIREMENTS must contain only STANDALONE_FASTMCP_SPEC"
    ]


def test_runtime_constants_reject_stable_ha_mcp_release_ref(tmp_path: Path) -> None:
    """Compatibility follows the HA-MCP branch that can change next."""
    sandbox = _load_sandbox()
    const = tmp_path / "const.py"
    _write_runtime_constants(const, compat_ref="v8.2.0")

    assert sandbox.validate_runtime_constants(const) == ["HA_MCP_COMPAT_REF must be 'master'"]


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


@pytest.mark.parametrize(
    "install_expression",
    [
        "partial(install_package, 'fastmcp>=3.4.5,<4', upgrade=True)",
        "await hass.async_add_executor_job(install_package, 'fastmcp>=3.4.5,<4')",
        "partial(func=install_package, requirement='fastmcp>=3.4.5,<4')",
        "installer.install_package('fastmcp>=3.4.5,<4')",
    ],
)
def test_install_contract_rejects_every_package_install_reference(
    tmp_path: Path,
    install_expression: str,
) -> None:
    """Any install_package reference bypasses HA's requirements manager."""
    sandbox = _load_sandbox()
    embedded_server = tmp_path / "embedded_server.py"
    embedded_server.write_text(
        f"from functools import partial\nasync def install(hass):\n    {install_expression}\n"
    )

    assert sandbox.validate_install_contract(embedded_server) == [
        "embedded dependency install at line 3 bypasses HA's requirements manager",
        "embedded dependency install must use HA async_process_requirements "
        "with STANDALONE_RUNTIME_REQUIREMENTS",
    ]


def test_install_contract_accepts_ha_requirements_manager(tmp_path: Path) -> None:
    """The supported install shape delegates locking and constraints to HA."""
    sandbox = _load_sandbox()
    embedded_server = tmp_path / "embedded_server.py"
    embedded_server.write_text(
        "async def install(hass):\n"
        "    await async_process_requirements(\n"
        "        hass, 'ESPHome MCP', list(STANDALONE_RUNTIME_REQUIREMENTS)\n"
        "    )\n"
    )

    assert sandbox.validate_install_contract(embedded_server) == []


def test_install_contract_checks_every_requirements_manager_call(tmp_path: Path) -> None:
    """One compliant requirements call cannot mask another unsafe requirement set."""
    sandbox = _load_sandbox()
    embedded_server = tmp_path / "embedded_server.py"
    embedded_server.write_text(
        "async def install(hass):\n"
        "    await async_process_requirements(\n"
        "        hass, 'ESPHome MCP', list(STANDALONE_RUNTIME_REQUIREMENTS)\n"
        "    )\n"
        "    await async_process_requirements(\n"
        "        hass, 'other', OTHER_REQUIREMENTS + STANDALONE_RUNTIME_REQUIREMENTS\n"
        "    )\n"
    )

    assert sandbox.validate_install_contract(embedded_server) == [
        "HA requirements-manager call at line 5 must use exactly STANDALONE_RUNTIME_REQUIREMENTS"
    ]
