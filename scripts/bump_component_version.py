"""Patch-bump ESPHome MCP release metadata for an automated update."""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = Path("pyproject.toml")
MANIFEST_PATH = Path("custom_components/esphome_mcp/manifest.json")
CONST_PATH = Path("custom_components/esphome_mcp/const.py")
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?$")


def _git(args: list[str]) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def _version_key(version: str) -> tuple[int, int, int, int, int]:
    match = VERSION_RE.fullmatch(version)
    if match is None:
        raise RuntimeError(f"unsupported release version {version!r}")
    major, minor, patch, prerelease, prerelease_num = match.groups()
    prerelease_rank = {"a": 0, "b": 1, "rc": 2, None: 3}[prerelease]
    return (
        int(major),
        int(minor),
        int(patch),
        prerelease_rank,
        int(prerelease_num or 0),
    )


def _next_patch(version: str) -> str:
    match = VERSION_RE.fullmatch(version)
    if match is None:
        raise RuntimeError(f"unsupported release version {version!r}")
    major, minor, patch, _prerelease, _prerelease_num = match.groups()
    return f"{major}.{minor}.{int(patch) + 1}"


def _manifest_version_from_ref(ref: str) -> str:
    raw = _git(["show", f"{ref}:{MANIFEST_PATH.as_posix()}"])
    return str(json.loads(raw)["version"])


def _const_version(source: str) -> str | None:
    module = ast.parse(source, filename=str(CONST_PATH))
    for statement in module.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "VERSION" for target in statement.targets
        ):
            continue
        if isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
            return statement.value.value
    return None


def _worktree_versions() -> tuple[str, str, str]:
    project = tomllib.loads((ROOT / PYPROJECT_PATH).read_text())
    manifest = json.loads((ROOT / MANIFEST_PATH).read_text())
    const_version = _const_version((ROOT / CONST_PATH).read_text())
    return (
        str(project["project"]["version"]),
        str(manifest["version"]),
        str(const_version or ""),
    )


def _replace_assignment(source: str, name: str, current: str, target: str) -> str:
    pattern = re.compile(
        rf'^(?P<prefix>{re.escape(name)}\s*=\s*)["\']{re.escape(current)}["\']',
        re.MULTILINE,
    )
    rendered, count = pattern.subn(rf'\g<prefix>"{target}"', source)
    if count != 1:
        raise RuntimeError(f"expected exactly one {name} assignment for {current!r}")
    return rendered


def _write_versions(current: str, target: str) -> None:
    pyproject_path = ROOT / PYPROJECT_PATH
    manifest_path = ROOT / MANIFEST_PATH
    const_path = ROOT / CONST_PATH

    pyproject = _replace_assignment(pyproject_path.read_text(), "version", current, target)
    manifest = json.loads(manifest_path.read_text())
    manifest["version"] = target
    rendered_manifest = json.dumps(manifest, indent=2) + "\n"
    const = _replace_assignment(const_path.read_text(), "VERSION", current, target)

    pyproject_path.write_text(pyproject)
    manifest_path.write_text(rendered_manifest)
    const_path.write_text(const)


def bump_component_version(base_ref: str = "origin/master") -> str:
    """Raise aligned release metadata one patch above ``base_ref`` exactly once."""
    base_version = _manifest_version_from_ref(base_ref)
    versions = _worktree_versions()
    if len(set(versions)) != 1:
        raise RuntimeError(
            "release version metadata is not aligned: "
            f"pyproject={versions[0]!r}, manifest={versions[1]!r}, const={versions[2]!r}"
        )

    current_version = versions[0]
    current_key = _version_key(current_version)
    base_key = _version_key(base_version)
    if current_key < base_key:
        raise RuntimeError(
            f"current release version {current_version!r} is behind "
            f"{base_ref} version {base_version!r}"
        )
    if current_key > base_key:
        print(
            f"ESPHome MCP version {current_version} already exceeds "
            f"{base_ref} version {base_version}; leaving it unchanged."
        )
        return current_version

    target_version = _next_patch(base_version)
    _write_versions(current_version, target_version)
    print(
        f"Bumped ESPHome MCP version from {current_version} to {target_version} "
        f"for the automated dependency update."
    )
    return target_version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "base_ref",
        nargs="?",
        default="origin/master",
        help="Git ref whose manifest version is the bump baseline.",
    )
    args = parser.parse_args(argv)

    try:
        bump_component_version(args.base_ref)
    except (
        KeyError,
        OSError,
        RuntimeError,
        SyntaxError,
        subprocess.CalledProcessError,
        tomllib.TOMLDecodeError,
    ) as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
