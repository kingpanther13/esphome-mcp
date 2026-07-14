"""Protect shared in-process dependencies from unsafe runtime mutation."""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "esphome_mcp"
CONST_PATH = COMPONENT / "const.py"
EMBEDDED_SERVER_PATH = COMPONENT / "embedded_server.py"

_EXACT_FASTMCP_PIN = re.compile(r"fastmcp==(\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?)")
_MODULE_CACHE_MUTATORS = {
    "__delitem__",
    "__ior__",
    "__setitem__",
    "clear",
    "pop",
    "popitem",
    "setdefault",
    "update",
}


def _constant_string(path: Path, name: str) -> str | None:
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        if not any(isinstance(target, ast.Name) and target.id == name for target in targets):
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    return None


def _import_aliases(tree: ast.AST) -> tuple[set[str], set[str], set[str], set[str]]:
    sys_names: set[str] = set()
    module_cache_names: set[str] = set()
    importlib_names: set[str] = set()
    reload_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sys":
                    sys_names.add(alias.asname or alias.name)
                elif alias.name == "importlib":
                    importlib_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "sys":
                for alias in node.names:
                    if alias.name == "modules":
                        module_cache_names.add(alias.asname or alias.name)
            elif node.module == "importlib":
                for alias in node.names:
                    if alias.name == "reload":
                        reload_names.add(alias.asname or alias.name)

    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not _is_module_cache(value, sys_names, module_cache_names):
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in module_cache_names:
                    module_cache_names.add(target.id)
                    changed = True
    return sys_names, module_cache_names, importlib_names, reload_names


def _is_module_cache(node: ast.AST | None, sys_names: set[str], aliases: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in aliases
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "modules"
        and isinstance(node.value, ast.Name)
        and node.value.id in sys_names
    )


def _mutates_module_cache_target(
    node: ast.AST,
    sys_names: set[str],
    aliases: set[str],
) -> bool:
    if isinstance(node, ast.Attribute):
        return _is_module_cache(node, sys_names, aliases)
    return isinstance(node, ast.Subscript) and _is_module_cache(node.value, sys_names, aliases)


def validate_runtime_source(path: Path) -> list[str]:
    """Return sandbox violations in one runtime Python source file."""
    tree = ast.parse(path.read_text(), filename=str(path))
    sys_names, module_cache_names, importlib_names, reload_names = _import_aliases(tree)
    errors: list[str] = []

    for node in ast.walk(tree):
        violation: str | None = None
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _MODULE_CACHE_MUTATORS
            and _is_module_cache(node.func.value, sys_names, module_cache_names)
        ):
            violation = f"sys.modules.{node.func.attr}()"
        elif isinstance(node, ast.Call) and (
            (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "reload"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in importlib_names
            )
            or (isinstance(node.func, ast.Name) and node.func.id in reload_names)
        ):
            violation = "importlib.reload()"
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                _mutates_module_cache_target(target, sys_names, module_cache_names)
                for target in targets
            ):
                violation = "assignment to sys.modules"
        elif isinstance(node, ast.AugAssign) and _is_module_cache(
            node.target, sys_names, module_cache_names
        ):
            violation = "in-place update of sys.modules"
        elif isinstance(node, ast.NamedExpr) and _mutates_module_cache_target(
            node.target, sys_names, module_cache_names
        ):
            violation = "assignment to sys.modules"
        elif isinstance(node, ast.Delete) and any(
            _mutates_module_cache_target(target, sys_names, module_cache_names)
            for target in node.targets
        ):
            violation = "deletion from sys.modules"

        if violation is not None:
            relative = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
            errors.append(
                f"{relative}:{node.lineno}: forbidden runtime dependency mutation: {violation}"
            )
    return errors


def validate_runtime_tree(component: Path = COMPONENT) -> list[str]:
    """Return all shared-dependency sandbox violations in the component."""
    errors: list[str] = []
    for path in sorted(component.rglob("*.py")):
        errors.extend(validate_runtime_source(path))
    return errors


def validate_runtime_constants(const_path: Path = CONST_PATH) -> list[str]:
    """Require an exact FastMCP pin validated against ha-mcp master."""
    errors: list[str] = []
    pip_spec = _constant_string(const_path, "DEFAULT_PIP_SPEC")
    if pip_spec is None or _EXACT_FASTMCP_PIN.fullmatch(pip_spec) is None:
        errors.append("DEFAULT_PIP_SPEC must be an exact fastmcp==X.Y.Z pin")
    compat_ref = _constant_string(const_path, "HA_MCP_COMPAT_REF")
    if compat_ref != "master":
        errors.append("HA_MCP_COMPAT_REF must be 'master'")
    return errors


def validate_worker_import_contract(path: Path = EMBEDDED_SERVER_PATH) -> list[str]:
    """Require deadlock-safe preloading before the worker enters its server coroutine."""
    tree = ast.parse(path.read_text(), filename=str(path))
    thread_main: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_thread_main":
            thread_main = node
            break
    if thread_main is None:
        return ["EmbeddedServerManager._thread_main is missing"]

    retry_lines: list[int] = []
    serve_lines: list[int] = []
    for node in ast.walk(thread_main):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == "_import_server_runtime_with_retry":
            retry_lines.append(node.lineno)
        elif isinstance(node.func, ast.Attribute) and node.func.attr == "_serve":
            serve_lines.append(node.lineno)

    if not retry_lines:
        return ["worker thread must call _import_server_runtime_with_retry"]
    if not serve_lines:
        return ["worker thread no longer calls _serve; update the sandbox contract"]
    if min(retry_lines) >= min(serve_lines):
        return ["worker thread must preload retry-safe imports before calling _serve"]
    return []


def validate_ha_mcp_pin(
    ha_mcp_pyproject: Path,
    const_path: Path = CONST_PATH,
) -> list[str]:
    """Require FastMCP parity with the current ha-mcp master branch."""
    project = tomllib.loads(ha_mcp_pyproject.read_text())
    dependencies = project.get("project", {}).get("dependencies", [])
    upstream_specs = [
        dependency
        for dependency in dependencies
        if isinstance(dependency, str) and dependency.lower().startswith("fastmcp")
    ]
    if len(upstream_specs) != 1:
        return [
            "ha-mcp pyproject must contain exactly one FastMCP dependency; "
            f"found {upstream_specs!r}"
        ]

    local_spec = _constant_string(const_path, "DEFAULT_PIP_SPEC")
    if upstream_specs[0] != local_spec:
        return [
            "shared FastMCP pin mismatch: "
            f"ESPHome MCP uses {local_spec!r}, ha-mcp uses {upstream_specs[0]!r}"
        ]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ha-mcp-pyproject",
        type=Path,
        help="Downloaded pyproject.toml for HA_MCP_COMPAT_REF.",
    )
    parser.add_argument(
        "--print-ha-mcp-ref",
        action="store_true",
        help="Print HA_MCP_COMPAT_REF for CI download steps.",
    )
    args = parser.parse_args(argv)

    if args.print_ha_mcp_ref:
        compat_ref = _constant_string(CONST_PATH, "HA_MCP_COMPAT_REF")
        if compat_ref is None:
            print("ERROR: HA_MCP_COMPAT_REF is missing", file=sys.stderr)
            return 1
        print(compat_ref)
        return 0

    errors = [
        *validate_runtime_tree(),
        *validate_runtime_constants(),
        *validate_worker_import_contract(),
    ]
    if args.ha_mcp_pyproject is not None:
        try:
            errors.extend(validate_ha_mcp_pin(args.ha_mcp_pyproject))
        except (OSError, tomllib.TOMLDecodeError) as err:
            errors.append(f"could not read ha-mcp pyproject: {err}")
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Runtime dependency sandbox passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
