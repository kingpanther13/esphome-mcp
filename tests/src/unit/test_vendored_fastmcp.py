"""Guards for the vendored FastMCP / MCP SDK copies.

ESPHome MCP imports only ``custom_components.esphome_mcp._vendor.{fastmcp,mcp,
mcp_types}``, so neither Home Assistant Core's ``mcp`` pin nor another
integration's MCP runtime can conflict with it.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_COMPONENT = _REPO_ROOT / "custom_components" / "esphome_mcp"
_VENDOR = _COMPONENT / "_vendor"
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import vendor_fastmcp  # noqa: E402


def _pins() -> dict[str, str]:
    return vendor_fastmcp.read_pins(_VENDOR / "requirements.txt")


def _imported_modules(source: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            found.add(node.module)
    return found


@pytest.mark.parametrize("spec", vendor_fastmcp.PACKAGES, ids=lambda s: s.package)
class TestVendoredTreeMatchesPin:
    def test_marker_matches_the_pin(self, spec) -> None:
        marker = (_VENDOR / spec.package / "VENDORED").read_text(encoding="utf-8")
        assert marker.startswith(f"{spec.distribution}=={_pins()[spec.distribution]}\n")

    def test_license_ships_with_the_tree(self, spec) -> None:
        assert (_VENDOR / spec.package / "LICENSE").stat().st_size > 0

    def test_tree_matches_the_recorded_manifest(self, spec) -> None:
        tree = _VENDOR / spec.package
        recorded = (tree / vendor_fastmcp.MANIFEST_NAME).read_text(encoding="utf-8")
        assert vendor_fastmcp.manifest_lines(tree) == recorded.splitlines(), (
            f"vendored {spec.package} was edited by hand or partially synced; "
            "re-run scripts/vendor_fastmcp.py and commit the result"
        )


def test_vendored_fastmcp_reports_the_pinned_version() -> None:
    source = (_VENDOR / "fastmcp" / "__init__.py").read_text(encoding="utf-8")
    assert f'__version__ = "{_pins()["fastmcp-slim"]}"' in source


def test_vendored_trees_import_only_their_vendored_copies() -> None:
    offenders = [
        f"{path.relative_to(_REPO_ROOT)}: {names}"
        for path in _VENDOR.rglob("*.py")
        if (names := vendor_fastmcp.remaining_vendored_imports(path.read_text("utf-8")))
    ]
    assert not offenders


def test_vendored_trees_never_import_websockets() -> None:
    """The manifest leaves websockets undeclared because nothing vendored imports it."""
    offenders = [
        str(path.relative_to(_REPO_ROOT))
        for path in _VENDOR.rglob("*.py")
        if any(
            name.split(".")[0] == "websockets"
            for name in _imported_modules(path.read_text("utf-8"))
        )
    ]
    assert not offenders


def test_component_never_imports_shared_or_peer_mcp_runtimes() -> None:
    """First-party code reaches FastMCP only through this component's vendored copy."""
    offenders = []
    for path in _COMPONENT.rglob("*.py"):
        if _VENDOR in path.parents:
            continue
        for name in _imported_modules(path.read_text("utf-8")):
            top = name.split(".")[0]
            if top in {"fastmcp", "mcp", "mcp_types", "ha_mcp"}:
                offenders.append(f"{path.relative_to(_REPO_ROOT)}: {name}")
    assert not offenders


def test_server_imports_the_vendored_fastmcp() -> None:
    server = (_COMPONENT / "server.py").read_text(encoding="utf-8")
    assert re.search(r"^from \._vendor\.fastmcp import FastMCP$", server, re.MULTILINE)


def test_manifest_requirement_merge_keeps_the_highest_floor() -> None:
    assert vendor_fastmcp._merge_clauses({">=2.5.0", ">=2.10.0"}) == [">=2.10.0"]
    assert vendor_fastmcp._merge_clauses({"<0.5.0", ">=0.4.4"}) == ["<0.5.0", ">=0.4.4"]


@pytest.mark.parametrize(
    "marker",
    [
        "platform_machine == 'arm64'",
        "python_version >= '3.14' or sys_platform == 'win32'",
        "(python_version >= '3.14'",
        "python_version >= '3.14')",
        "python_version ~= '3.14'",
        "python_version == '3.14.*'",
    ],
)
def test_manifest_requirements_reject_unknown_markers(tmp_path: Path, marker: str) -> None:
    for spec in vendor_fastmcp.PACKAGES:
        tree = tmp_path / spec.package
        tree.mkdir()
        (tree / vendor_fastmcp.REQUIRES_NAME).write_text(
            f"anyio>=4.9; {marker}\n", encoding="utf-8"
        )
    with pytest.raises(SystemExit, match="unhandled marker"):
        vendor_fastmcp.manifest_requirements(tmp_path)


@pytest.mark.parametrize(
    ("marker", "included"),
    [
        ("(python_version >= '3.15')", False),
        ("python_version < '3.15'", True),
        ('(python_version >= "3.14")', True),
        ("python_version >= '3.9'", True),
        ("python_version > '3.14'", False),
        ("python_version <= '3.14'", True),
        ("python_version == '3.14'", True),
        ("python_version != '3.14'", False),
        ("python_version >= '3.14'", True),
        ("python_version < '3.14'", False),
        ("sys_platform == 'win32'", False),
        ("sys_platform != 'emscripten'", True),
    ],
)
def test_manifest_python_markers_use_the_ha_target(
    tmp_path: Path, marker: str, included: bool
) -> None:
    """The runner's Python version must not determine HA's dependencies."""
    for spec in vendor_fastmcp.PACKAGES:
        tree = tmp_path / spec.package
        tree.mkdir()
        (tree / vendor_fastmcp.REQUIRES_NAME).write_text(
            f"beartype>=0.23.0rc2; {marker}\n", encoding="utf-8"
        )
    requirements = vendor_fastmcp.manifest_requirements(tmp_path)
    assert requirements == (["beartype>=0.23.0rc2"] if included else [])


def test_future_ha_python_target_includes_its_conditional_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vendor_fastmcp, "_TARGET_PYTHON_VERSION", (3, 15))
    for spec in vendor_fastmcp.PACKAGES:
        tree = tmp_path / spec.package
        tree.mkdir()
        (tree / vendor_fastmcp.REQUIRES_NAME).write_text(
            "beartype>=0.23.0rc2; (python_version >= '3.15')\n", encoding="utf-8"
        )
    assert vendor_fastmcp.manifest_requirements(tmp_path) == ["beartype>=0.23.0rc2"]
