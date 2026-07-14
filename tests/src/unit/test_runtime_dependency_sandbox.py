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
    """Production component code neither evicts nor reloads shared modules."""
    sandbox = _load_sandbox()

    assert sandbox.validate_runtime_tree() == []
    assert sandbox.validate_runtime_constants() == []
    assert sandbox.validate_worker_import_contract() == []


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


def test_ha_mcp_fastmcp_pin_parity(tmp_path: Path) -> None:
    """The compatibility gate accepts the exact shared FastMCP version."""
    sandbox = _load_sandbox()
    upstream = tmp_path / "pyproject.toml"
    upstream.write_text(
        '[project]\nname = "ha-mcp"\ndependencies = ["fastmcp==3.4.4", "httpx==0.28.1"]\n'
    )

    assert sandbox.validate_ha_mcp_pin(upstream) == []


def test_ha_mcp_fastmcp_pin_mismatch_fails(tmp_path: Path) -> None:
    """A future ha-mcp dependency change blocks release until pins are aligned."""
    sandbox = _load_sandbox()
    upstream = tmp_path / "pyproject.toml"
    upstream.write_text('[project]\nname = "ha-mcp"\ndependencies = ["fastmcp==9.9.9"]\n')

    errors = sandbox.validate_ha_mcp_pin(upstream)

    assert errors == [
        "shared FastMCP pin mismatch: ESPHome MCP uses 'fastmcp==3.4.4', "
        "ha-mcp uses 'fastmcp==9.9.9'"
    ]


def test_runtime_constants_reject_stable_ha_mcp_release_ref(tmp_path: Path) -> None:
    """Compatibility cannot silently drift back from ha-mcp master to a release tag."""
    sandbox = _load_sandbox()
    const = tmp_path / "const.py"
    const.write_text('DEFAULT_PIP_SPEC = "fastmcp==3.4.4"\nHA_MCP_COMPAT_REF = "v7.12.3"\n')

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
