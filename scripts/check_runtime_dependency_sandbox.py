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
_REQUIREMENT_PARTS = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)"
    r"(?:\[(?P<extras>[^]]+)\])?"
    r"(?P<specifier>[^;]*)"
    r"(?:;(?P<marker>.*))?$"
)
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
    return _canonical_requirement_identifier(match.group(1))


def _requirement_map(requirements: list[str] | tuple[str, ...]) -> dict[str, str]:
    """Map canonical distribution names to their complete requirement strings."""
    mapped: dict[str, str] = {}
    for requirement in requirements:
        if (name := _requirement_name(requirement)) is not None:
            mapped[name] = requirement
    return mapped


def _canonical_requirement_identifier(identifier: str) -> str:
    """Return the canonical spelling of a distribution name or extra."""
    return re.sub(r"[-_.]+", "-", identifier).lower()


def _remove_unquoted_whitespace(value: str) -> str | None:
    """Remove requirement syntax whitespace while preserving quoted marker values."""
    compact: list[str] = []
    quote: str | None = None
    escaped = False
    for char in value:
        if quote is not None:
            compact.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in {'"', "'"}:
            quote = char
            compact.append(char)
        elif not char.isspace():
            compact.append(char)
    return None if quote is not None else "".join(compact)


def _normalized_requirement(requirement: str) -> tuple[object, ...] | None:
    """Normalize a static PEP 508 requirement without third-party imports."""
    if (compact := _remove_unquoted_whitespace(requirement)) is None or (
        match := _REQUIREMENT_PARTS.fullmatch(compact)
    ) is None:
        return None
    name = _canonical_requirement_identifier(match.group("name"))
    extras = tuple(
        sorted(
            _canonical_requirement_identifier(extra)
            for extra in filter(None, (match.group("extras") or "").split(","))
        )
    )
    specifiers = tuple(sorted(filter(None, match.group("specifier").split(","))))
    return name, extras, specifiers, match.group("marker") or ""


def _requirements_match(left: str, right: str) -> bool:
    """Return whether two static requirement strings are semantically identical."""
    normalized_left = _normalized_requirement(left)
    return normalized_left is not None and normalized_left == _normalized_requirement(right)


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
    ha_owned = _constant_string_tuple(const_path, "HA_OWNED_RUNTIME_REQUIREMENTS")
    if ha_owned != ("websockets",):
        errors.append("HA_OWNED_RUNTIME_REQUIREMENTS must contain only 'websockets'")
    if len(shared_by_name) != len(shared):
        errors.append("SHARED_RUNTIME_REQUIREMENTS must contain unique valid requirements")
    if shared_by_name.get("fastmcp") != pip_spec:
        errors.append("SHARED_RUNTIME_REQUIREMENTS must contain DEFAULT_PIP_SPEC")
    if shared[-1:] != (pip_spec,):
        errors.append("DEFAULT_PIP_SPEC must be last so shared constraints install first")
    if "websockets" in shared_by_name:
        errors.append("websockets is HA-owned and must not be installed by ESPHome MCP")
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
    direct_installs: list[ast.AST] = []
    process_calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Name) and node.id == "install_package") or (
            isinstance(node, ast.Attribute) and node.attr == "install_package"
        ):
            direct_installs.append(node)
            continue
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == "async_process_requirements":
            process_calls.append(node)

    errors = [
        f"embedded dependency install at line {call.lineno} bypasses HA's requirements manager"
        for call in direct_installs
    ]
    if not process_calls:
        errors.append(
            "embedded dependency install must use HA async_process_requirements "
            "with SHARED_RUNTIME_REQUIREMENTS"
        )
        return errors

    for call in process_calls:
        requirements_arg: ast.AST | None = call.args[2] if len(call.args) > 2 else None
        if requirements_arg is None:
            requirements_arg = next(
                (keyword.value for keyword in call.keywords if keyword.arg == "requirements"),
                None,
            )
        direct_shared = (
            isinstance(requirements_arg, ast.Name)
            and requirements_arg.id == "SHARED_RUNTIME_REQUIREMENTS"
        )
        copied_shared = (
            isinstance(requirements_arg, ast.Call)
            and isinstance(requirements_arg.func, ast.Name)
            and requirements_arg.func.id == "list"
            and len(requirements_arg.args) == 1
            and not requirements_arg.keywords
            and isinstance(requirements_arg.args[0], ast.Name)
            and requirements_arg.args[0].id == "SHARED_RUNTIME_REQUIREMENTS"
        )
        if not (direct_shared or copied_shared):
            errors.append(
                f"HA requirements-manager call at line {call.lineno} must use exactly "
                "SHARED_RUNTIME_REQUIREMENTS"
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
    ha_owned = set(_constant_string_tuple(const_path, "HA_OWNED_RUNTIME_REQUIREMENTS") or ())
    upstream_shared = {
        name: spec for name, spec in upstream_by_name.items() if name not in ha_owned
    }

    errors: list[str] = []
    local_by_name = _requirement_map(local)
    for name, local_spec in local_by_name.items():
        upstream_spec = upstream_shared.get(name)
        if upstream_spec is None:
            errors.append(f"ha-mcp is missing shared runtime dependency {name!r}")
            continue
        if _requirements_match(upstream_spec, local_spec):
            continue
        errors.append(
            f"shared runtime dependency mismatch for {name}: "
            f"ESPHome MCP uses {local_spec!r}, ha-mcp uses {upstream_spec!r}"
        )
    for name in sorted(upstream_shared.keys() - local_by_name.keys()):
        errors.append(f"ESPHome MCP is missing ha-mcp runtime dependency {name!r}")
    return errors


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
