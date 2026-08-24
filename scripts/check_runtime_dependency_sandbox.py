"""Protect the generated HA-MCP runtime contract from unsafe live mutation."""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "esphome_mcp"
CONTRACT_PATH = COMPONENT / "ha_mcp_runtime" / "contract.py"
EMBEDDED_SERVER_PATH = COMPONENT / "embedded_server.py"

_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_EXACT_FASTMCP_PIN = re.compile(
    r"fastmcp==\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?"
)
_REQUIREMENT_NAME = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[^]]+\])?"
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
_MUTATION_ERROR = "forbidden runtime dependency mutation"


def _constant_string(path: Path, name: str) -> str | None:
    """Read one top-level string literal."""
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
        if not any(
            isinstance(target, ast.Name) and target.id == name
            for target in targets
        ):
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    return None


def _constant_string_tuple(path: Path, name: str) -> tuple[str, ...] | None:
    """Read one top-level tuple/list containing only string literals."""
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
        if not any(
            isinstance(target, ast.Name) and target.id == name
            for target in targets
        ):
            continue
        if not isinstance(value, (ast.Tuple, ast.List)):
            return None
        if not all(
            isinstance(item, ast.Constant) and isinstance(item.value, str)
            for item in value.elts
        ):
            return None
        return tuple(item.value for item in value.elts)
    return None


def _canonical_name(requirement: str) -> str | None:
    """Return the canonical distribution name from a requirement string."""
    match = _REQUIREMENT_NAME.match(requirement.strip())
    if match is None:
        return None
    return re.sub(r"[-_.]+", "-", match.group(1)).lower()


def _import_aliases(
    tree: ast.AST,
) -> tuple[set[str], set[str], set[str], set[str]]:
    """Find direct and aliased handles to the process-global module cache."""
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


def _is_module_cache(
    node: ast.AST | None,
    sys_names: set[str],
    aliases: set[str],
) -> bool:
    """Return whether an AST node refers to sys.modules."""
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
    """Return whether an assignment target writes through sys.modules."""
    if isinstance(node, ast.Attribute):
        return _is_module_cache(node, sys_names, aliases)
    return (
        isinstance(node, ast.Subscript)
        and _is_module_cache(node.value, sys_names, aliases)
    )


def validate_runtime_source(path: Path) -> list[str]:
    """Return sandbox violations in one runtime Python source file."""
    tree = ast.parse(path.read_text(), filename=str(path))
    sys_names, module_cache_names, importlib_names, reload_names = _import_aliases(
        tree
    )
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
            or (
                isinstance(node.func, ast.Name)
                and node.func.id in reload_names
            )
        ):
            violation = "importlib.reload()"
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                _mutates_module_cache_target(
                    target,
                    sys_names,
                    module_cache_names,
                )
                for target in targets
            ):
                violation = "assignment to sys.modules"
        elif isinstance(node, ast.AugAssign) and _is_module_cache(
            node.target,
            sys_names,
            module_cache_names,
        ):
            violation = "in-place update of sys.modules"
        elif isinstance(node, ast.NamedExpr) and _mutates_module_cache_target(
            node.target,
            sys_names,
            module_cache_names,
        ):
            violation = "assignment to sys.modules"
        elif isinstance(node, ast.Delete) and any(
            _mutates_module_cache_target(
                target,
                sys_names,
                module_cache_names,
            )
            for target in node.targets
        ):
            violation = "deletion from sys.modules"

        if violation is not None:
            relative = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
            errors.append(
                f"{relative}:{node.lineno}: {_MUTATION_ERROR}: {violation}"
            )
    return errors


def validate_runtime_tree(component: Path = COMPONENT) -> list[str]:
    """Return all shared-dependency sandbox violations in the component."""
    errors: list[str] = []
    for path in sorted(component.rglob("*.py")):
        errors.extend(validate_runtime_source(path))
    return errors


def validate_runtime_contract(path: Path = CONTRACT_PATH) -> list[str]:
    """Require one immutable snapshot with both server and component metadata."""
    errors: list[str] = []
    repository = _constant_string(path, "HA_MCP_REPOSITORY")
    sha = _constant_string(path, "HA_MCP_MASTER_SHA")
    server_version = _constant_string(path, "HA_MCP_SERVER_VERSION")
    component_version = _constant_string(path, "HA_MCP_COMPONENT_VERSION")
    fastmcp = _constant_string(path, "HA_MCP_FASTMCP_REQUIREMENT")
    server_requirements = _constant_string_tuple(
        path,
        "HA_MCP_SERVER_REQUIREMENTS",
    )
    component_requirements = _constant_string_tuple(
        path,
        "HA_MCP_COMPONENT_REQUIREMENTS",
    )

    if repository != "homeassistant-ai/ha-mcp":
        errors.append("HA_MCP_REPOSITORY must be homeassistant-ai/ha-mcp")
    if sha is None or _COMMIT_RE.fullmatch(sha) is None:
        errors.append("HA_MCP_MASTER_SHA must be one immutable 40-character SHA")
    if not server_version:
        errors.append("HA_MCP_SERVER_VERSION is missing")
    if not component_version:
        errors.append("HA_MCP_COMPONENT_VERSION is missing")
    if not server_requirements:
        errors.append("HA_MCP_SERVER_REQUIREMENTS must not be empty")
    if component_requirements is None:
        errors.append("HA_MCP_COMPONENT_REQUIREMENTS must be a string tuple")
    if fastmcp is None or _EXACT_FASTMCP_PIN.fullmatch(fastmcp) is None:
        errors.append("HA_MCP_FASTMCP_REQUIREMENT must be an exact FastMCP pin")
    if server_requirements is not None and fastmcp not in server_requirements:
        errors.append(
            "HA_MCP_FASTMCP_REQUIREMENT must be present in "
            "HA_MCP_SERVER_REQUIREMENTS"
        )
    if server_requirements is not None:
        names = [_canonical_name(requirement) for requirement in server_requirements]
        if None in names:
            errors.append("HA_MCP_SERVER_REQUIREMENTS contains an invalid requirement")
        elif len(names) != len(set(names)):
            errors.append(
                "HA_MCP_SERVER_REQUIREMENTS contains duplicate distributions"
            )
    return errors


def validate_worker_import_contract(path: Path = EMBEDDED_SERVER_PATH) -> list[str]:
    """Require deadlock-safe preloading before the worker serves requests."""
    tree = ast.parse(path.read_text(), filename=str(path))
    thread_main = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_thread_main"
        ),
        None,
    )
    if thread_main is None:
        return ["EmbeddedServerManager._thread_main is missing"]

    retry_lines: list[int] = []
    serve_lines: list[int] = []
    for node in ast.walk(thread_main):
        if not isinstance(node, ast.Call):
            continue
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "_import_server_runtime_with_retry"
        ):
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
    """Require installs to use HA's lock with the generated server tuple."""
    tree = ast.parse(path.read_text(), filename=str(path))
    direct_installs: list[ast.AST] = []
    process_calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Name) and node.id == "install_package") or (
            isinstance(node, ast.Attribute) and node.attr == "install_package"
        ):
            direct_installs.append(node)
            continue
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "async_process_requirements"
        ):
            process_calls.append(node)

    errors = [
        f"embedded dependency install at line {call.lineno} bypasses "
        "HA's requirements manager"
        for call in direct_installs
    ]
    if not process_calls:
        errors.append(
            "embedded dependency install must use HA async_process_requirements "
            "with HA_MCP_SERVER_REQUIREMENTS"
        )
        return errors

    for call in process_calls:
        requirements_arg = call.args[2] if len(call.args) > 2 else next(
            (
                keyword.value
                for keyword in call.keywords
                if keyword.arg == "requirements"
            ),
            None,
        )
        direct_contract = (
            isinstance(requirements_arg, ast.Name)
            and requirements_arg.id == "HA_MCP_SERVER_REQUIREMENTS"
        )
        copied_contract = (
            isinstance(requirements_arg, ast.Call)
            and isinstance(requirements_arg.func, ast.Name)
            and requirements_arg.func.id == "list"
            and len(requirements_arg.args) == 1
            and not requirements_arg.keywords
            and isinstance(requirements_arg.args[0], ast.Name)
            and requirements_arg.args[0].id == "HA_MCP_SERVER_REQUIREMENTS"
        )
        if not (direct_contract or copied_contract):
            errors.append(
                f"HA requirements-manager call at line {call.lineno} must use "
                "exactly HA_MCP_SERVER_REQUIREMENTS"
            )
    return errors


def main(argv: list[str] | None = None) -> int:
    """Run the static runtime sandbox."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--print-ha-mcp-ref",
        action="store_true",
        help="Print the immutable HA_MCP_MASTER_SHA.",
    )
    args = parser.parse_args(argv)

    if args.print_ha_mcp_ref:
        ref = _constant_string(CONTRACT_PATH, "HA_MCP_MASTER_SHA")
        if ref is None:
            print("ERROR: HA_MCP_MASTER_SHA is missing", file=sys.stderr)
            return 1
        print(ref)
        return 0

    errors = [
        *validate_runtime_tree(),
        *validate_runtime_contract(),
        *validate_worker_import_contract(),
        *validate_install_contract(),
    ]
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Runtime dependency sandbox passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
