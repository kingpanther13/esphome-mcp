"""Generate ESPHome MCP's dependency-only contract from one HA-MCP commit."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = (
    ROOT
    / "custom_components"
    / "esphome_mcp"
    / "ha_mcp_runtime"
    / "contract.py"
)
HA_MCP_REPOSITORY = "homeassistant-ai/ha-mcp"
PYPROJECT_PATH = "pyproject.toml"
MANIFEST_PATH = "custom_components/ha_mcp_tools/manifest.json"
CONST_PATH = "custom_components/ha_mcp_tools/const.py"
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_REQUIREMENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*")


def _github_headers(*, raw: bool = False) -> dict[str, str]:
    """Return authenticated GitHub API headers when a token is available."""
    headers = {
        "Accept": (
            "application/vnd.github.raw+json"
            if raw
            else "application/vnd.github+json"
        ),
        "User-Agent": "esphome-mcp-runtime-contract-sync",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = (
        os.environ.get("GH_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or os.environ.get("RENOVATE_TOKEN")
    )
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _request(url: str, *, raw: bool = False) -> bytes:
    """Read one GitHub API resource."""
    request = urllib.request.Request(url, headers=_github_headers(raw=raw))
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except (OSError, urllib.error.HTTPError) as err:
        raise RuntimeError(f"GitHub request failed for {url}: {err}") from err


def _resolve_commit(ref: str) -> str:
    """Resolve a branch, tag, or commit to one immutable commit SHA."""
    encoded_ref = urllib.parse.quote(ref, safe="")
    url = (
        f"https://api.github.com/repos/{HA_MCP_REPOSITORY}/commits/"
        f"{encoded_ref}"
    )
    payload = json.loads(_request(url))
    sha = payload.get("sha")
    if not isinstance(sha, str) or _COMMIT_RE.fullmatch(sha) is None:
        raise RuntimeError(f"GitHub returned an invalid commit SHA for {ref!r}")
    return sha


def _read_source(path: str, sha: str) -> str:
    """Read one upstream file from the resolved commit."""
    encoded_path = "/".join(urllib.parse.quote(part, safe="") for part in path.split("/"))
    url = (
        f"https://api.github.com/repos/{HA_MCP_REPOSITORY}/contents/"
        f"{encoded_path}?ref={sha}"
    )
    return _request(url, raw=True).decode()


def _string_constant(source: str, name: str, *, filename: str) -> str:
    """Read one top-level string constant without importing upstream code."""
    tree = ast.parse(source, filename=filename)
    for node in tree.body:
        value: ast.expr | None = None
        target_names: list[str] = []
        if isinstance(node, ast.Assign):
            value = node.value
            target_names = [
                target.id for target in node.targets if isinstance(target, ast.Name)
            ]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            value = node.value
            target_names = [node.target.id]
        if (
            name in target_names
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            return value.value
    raise RuntimeError(f"{filename} does not define string constant {name}")


def _canonical_name(requirement: str) -> str:
    """Return the canonical distribution name from a simple PEP 508 string."""
    match = _REQUIREMENT_NAME_RE.match(requirement)
    if match is None:
        raise RuntimeError(f"Could not parse requirement name from {requirement!r}")
    return re.sub(r"[-_.]+", "-", match.group(0)).lower()


def _string_list(value: Any, *, label: str) -> tuple[str, ...]:
    """Validate and freeze an upstream requirement list."""
    if not isinstance(value, list) or not all(
        isinstance(requirement, str) and requirement for requirement in value
    ):
        raise RuntimeError(f"{label} must be a list of requirement strings")
    requirements = tuple(value)
    names = [_canonical_name(requirement) for requirement in requirements]
    if len(names) != len(set(names)):
        raise RuntimeError(f"{label} declares a dependency more than once")
    return requirements


def _format_tuple(name: str, values: tuple[str, ...]) -> str:
    """Render one deterministic Python string tuple."""
    lines = [f"{name} = ("]
    lines.extend(f"    {json.dumps(value)}," for value in values)
    lines.append(")")
    return "\n".join(lines)


def _render_contract(
    *,
    sha: str,
    server_version: str,
    component_version: str,
    server_requirements: tuple[str, ...],
    component_requirements: tuple[str, ...],
) -> str:
    """Render the complete generated contract module."""
    fastmcp = next(
        (
            requirement
            for requirement in server_requirements
            if _canonical_name(requirement) == "fastmcp"
        ),
        None,
    )
    if fastmcp is None:
        raise RuntimeError("HA-MCP master does not declare a FastMCP dependency")

    return (
        '"""Generated dependency contract for one immutable HA-MCP master snapshot.\n\n'
        "Do not edit this file by hand. Run scripts/sync_ha_mcp_runtime_contract.py\n"
        "to refresh both the server and custom-component sides from the same commit.\n"
        "Only dependency metadata is mirrored; no HA-MCP server or tool code is bundled.\n"
        '"""\n\n'
        f'HA_MCP_REPOSITORY = "{HA_MCP_REPOSITORY}"\n'
        "# renovate: datasource=git-refs "
        f"packageName=https://github.com/{HA_MCP_REPOSITORY} branch=master\n"
        f'HA_MCP_MASTER_SHA = "{sha}"\n'
        f"HA_MCP_SERVER_VERSION = {json.dumps(server_version)}\n"
        f"HA_MCP_COMPONENT_VERSION = {json.dumps(component_version)}\n\n"
        f"{_format_tuple('HA_MCP_SERVER_REQUIREMENTS', server_requirements)}\n\n"
        f"{_format_tuple('HA_MCP_COMPONENT_REQUIREMENTS', component_requirements)}\n\n"
        f"HA_MCP_FASTMCP_REQUIREMENT = {json.dumps(fastmcp)}\n"
        'HA_MCP_RUNTIME_CONTRACT_ID = f"{HA_MCP_REPOSITORY}@{HA_MCP_MASTER_SHA}"\n'
    )


def _contract_ref() -> str:
    """Read the SHA Renovate already updated in the generated contract."""
    return _string_constant(
        CONTRACT_PATH.read_text(),
        "HA_MCP_MASTER_SHA",
        filename=str(CONTRACT_PATH),
    )


def _generate(ref: str) -> str:
    """Resolve and render one HA-MCP source snapshot."""
    sha = _resolve_commit(ref)
    project = tomllib.loads(_read_source(PYPROJECT_PATH, sha))
    manifest = json.loads(_read_source(MANIFEST_PATH, sha))
    const_source = _read_source(CONST_PATH, sha)

    project_table = project.get("project")
    if not isinstance(project_table, dict):
        raise RuntimeError("HA-MCP pyproject.toml has no [project] table")
    server_version = project_table.get("version")
    if not isinstance(server_version, str) or not server_version:
        raise RuntimeError("HA-MCP pyproject.toml has no project version")
    server_requirements = _string_list(
        project_table.get("dependencies"),
        label="HA-MCP server dependencies",
    )

    if not isinstance(manifest, dict):
        raise RuntimeError("HA-MCP component manifest is not an object")
    component_version = manifest.get("version")
    if not isinstance(component_version, str) or not component_version:
        raise RuntimeError("HA-MCP component manifest has no version")
    component_requirements = _string_list(
        manifest.get("requirements"),
        label="HA-MCP component requirements",
    )
    const_version = _string_constant(
        const_source,
        "COMPONENT_VERSION",
        filename=CONST_PATH,
    )
    if const_version != component_version:
        raise RuntimeError(
            "HA-MCP master component version drift: manifest "
            f"{component_version!r} != const {const_version!r}"
        )

    return _render_contract(
        sha=sha,
        server_version=server_version,
        component_version=component_version,
        server_requirements=server_requirements,
        component_requirements=component_requirements,
    )


def main(argv: list[str] | None = None) -> int:
    """Synchronize or verify the generated dependency contract."""
    parser = argparse.ArgumentParser()
    ref_group = parser.add_mutually_exclusive_group()
    ref_group.add_argument("--ref", default=None, help="HA-MCP ref to mirror")
    ref_group.add_argument(
        "--contract-ref",
        action="store_true",
        help="Mirror the SHA already stored in the contract (for Renovate/CI)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail instead of writing when the generated contract differs",
    )
    args = parser.parse_args(argv)

    ref = _contract_ref() if args.contract_ref else (args.ref or "master")
    try:
        rendered = _generate(ref)
    except (OSError, RuntimeError, SyntaxError, tomllib.TOMLDecodeError) as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1

    current = CONTRACT_PATH.read_text() if CONTRACT_PATH.exists() else ""
    if current == rendered:
        print(f"HA-MCP runtime contract is current for {ref}.")
        return 0
    if args.check:
        print(
            "ERROR: HA-MCP runtime contract is out of sync; run "
            "scripts/sync_ha_mcp_runtime_contract.py --contract-ref",
            file=sys.stderr,
        )
        return 1

    CONTRACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONTRACT_PATH.write_text(rendered)
    print(f"Updated HA-MCP runtime contract from {ref}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
