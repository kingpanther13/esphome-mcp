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
_REQUIREMENT_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[^]]+\])?")
_SAFE_WEBSOCKETS_SPEC = "websockets>=15.0.1,<18"
# ha-mcp 8.1.0 still has this exact pin while #2146 is being fixed. It is
# compatible with the safe range and may coexist during the coordinated
# rollout; all other shared specs must match exactly.
_TRANSITIONAL_HA_MCP_WEBSOCKETS_SPEC = "websockets==17.0"
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


def _constant_string_tuple(path: Path, name: str) -> tuple[str, ...] | None:
    """Read a tuple/list of string constants, resolving earlier string names."""
    tree = ast.parse(path.read_text(), filename=str(path))
    strings: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        target_names = [target.id for target in targets if isinstance(target, ast.Name)]
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            for target_name in target_names:
                strings[target_name] = value.value
            continue
        if name not in target_names or not isinstance(value, (ast.Tuple, ast.List)):
            continue
        resolved: list[str] = []
        for item in value.elts:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                resolved.append(item.value)
            elif isinstance(item, ast.Name) and item.id in strings:
                resolved.append(strings[item.id])
            else:
                return None
        return tuple(resolved)
    return None


def _requirement_name(requirement: str) -> str | None:
    """Return a dependency's canonical distribution name."""
    if (match := _REQUIREMENT_NAME.match(requirement.strip())) is None:
        return None
    return match.group(1).lower().replace("_", "-").replace(".", "-")


def _requirement_map(requirements: list[str] | tuple[str, ...]) -> dict[str, str]:
    """Map canonical distribution names to their complete requirement strings."""
    mapped: dict[str, str] = {}
    for requirement in requirements:
        if (name := _requirement_name(requirement)) is not None:
            mapped[name] = requirement
    return mapped


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
    """Require safe, ordered shared specs validated against ha-mcp master."""
    errors: list[str] = []
    pip_spec = _constant_string(const_path, "DEFAULT_PIP_SPEC")
    if pip_spec is None or _EXACT_FASTMCP_PIN.fullmatch(pip_spec) is None:
        errors.append("DEFAULT_PIP_SPEC must be an exact fastmcp==X.Y.Z pin")
    compat_ref = _constant_string(const_path, "HA_MCP_COMPAT_REF")
    if compat_ref != "master":
        errors.append("HA_MCP_COMPAT_REF must be 'master'")
    shared = _constant_string_tuple(const_path, "SHARED_RUNTIME_REQUIREMENTS")
    if shared is None:
        errors.append("SHARED_RUNTIME_REQUIREMENTS must be a static tuple of strings")
        return errors
    shared_by_name = _requirement_map(shared)
    if len(shared_by_name) != len(shared):
        errors.append("SHARED_RUNTIME_REQUIREMENTS must contain unique valid requirements")
    if shared_by_name.get("fastmcp") != pip_spec:
        errors.append("SHARED_RUNTIME_REQUIREMENTS must contain DEFAULT_PIP_SPEC")
    if shared[-1:] != (pip_spec,):
        errors.append("DEFAULT_PIP_SPEC must be last so shared constraints install first")
    if shared_by_name.get("websockets") != _SAFE_WEBSOCKETS_SPEC:
        errors.append(
            f"websockets must use HA-compatible range {_SAFE_WEBSOCKETS_SPEC!r}"
        )
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


def validate_install_contract(path: Path = EMBEDDED_SERVER_PATH) -> list[str]:
    """Require installs to use Home Assistant's process-locked public API."""
    tree = ast.parse(path.read_text(), filename=str(path))
    direct_installs: list[ast.Call] = []
    process_calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        direct_install = isinstance(node.func, ast.Name) and node.func.id == "install_package"
        partial_install = (
            isinstance(node.func, ast.Name)
            and node.func.id == "partial"
            and bool(node.args)
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "install_package"
        )
        if direct_install or partial_install:
            direct_installs.append(node)
        if isinstance(node.func, ast.Name) and node.func.id == "async_process_requirements":
            process_calls.append(node)

    errors = [
        f"embedded dependency install at line {call.lineno} bypasses HA's requirements manager"
        for call in direct_installs
    ]
    uses_shared_requirements = any(
        any(
            isinstance(descendant, ast.Name)
            and descendant.id == "SHARED_RUNTIME_REQUIREMENTS"
            for descendant in ast.walk(call)
        )
        for call in process_calls
    )
    if not uses_shared_requirements:
        errors.append(
            "embedded dependency install must use HA async_process_requirements "
            "with SHARED_RUNTIME_REQUIREMENTS"
        )
    return errors


def validate_ha_mcp_shared_requirements(
    ha_mcp_pyproject: Path,
    const_path: Path = CONST_PATH,
) -> list[str]:
    """Require every ESPHome MCP shared spec to match ha-mcp master."""
    project = tomllib.loads(ha_mcp_pyproject.read_text())
    dependencies = [
        dependency
        for dependency in project.get("project", {}).get("dependencies", [])
        if isinstance(dependency, str)
    ]
    upstream_by_name = _requirement_map(dependencies)
    local = _constant_string_tuple(const_path, "SHARED_RUNTIME_REQUIREMENTS")
    if local is None:
        return ["SHARED_RUNTIME_REQUIREMENTS must be a static tuple of strings"]

    errors: list[str] = []
    local_by_name = _requirement_map(local)
    for name, local_spec in local_by_name.items():
        upstream_spec = upstream_by_name.get(name)
        if upstream_spec is None:
            errors.append(f"ha-mcp is missing shared runtime dependency {name!r}")
            continue
        if upstream_spec == local_spec:
            continue
        if (
            name == "websockets"
            and local_spec == _SAFE_WEBSOCKETS_SPEC
            and upstream_spec == _TRANSITIONAL_HA_MCP_WEBSOCKETS_SPEC
        ):
            continue
        errors.append(
            f"shared runtime dependency mismatch for {name}: "
            f"ESPHome MCP uses {local_spec!r}, ha-mcp uses {upstream_spec!r}"
        )
    for name in sorted(upstream_by_name.keys() - local_by_name.keys()):
        errors.append(f"ESPHome MCP is missing ha-mcp runtime dependency {name!r}")
    return errors


def validate_ha_mcp_pin(
    ha_mcp_pyproject: Path,
    const_path: Path = CONST_PATH,
) -> list[str]:
    """Backward-compatible alias for the expanded shared-requirement gate."""
    return validate_ha_mcp_shared_requirements(ha_mcp_pyproject, const_path)


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
        *validate_install_contract(),
    ]
    if args.ha_mcp_pyproject is not None:
        try:
            errors.extend(validate_ha_mcp_shared_requirements(args.ha_mcp_pyproject))
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
