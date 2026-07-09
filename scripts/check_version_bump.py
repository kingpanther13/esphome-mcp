"""Ensure release-facing component changes bump the manifest version."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = "custom_components/esphome_mcp/manifest.json"
RELEASE_FACING_PREFIXES = ("custom_components/esphome_mcp/",)
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?$")


def _git(args: list[str]) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def _changed_release_files(base_ref: str) -> list[str]:
    try:
        changed = _git(["diff", "--name-only", f"{base_ref}...HEAD"])
    except subprocess.CalledProcessError:
        changed = _git(["diff", "--name-only", f"{base_ref}..HEAD"])
    return [path for path in changed.splitlines() if path.startswith(RELEASE_FACING_PREFIXES)]


def _manifest_version_from_worktree() -> str:
    return str(json.loads((ROOT / MANIFEST_PATH).read_text())["version"])


def _manifest_version_from_ref(ref: str) -> str:
    raw = _git(["show", f"{ref}:{MANIFEST_PATH}"])
    return str(json.loads(raw)["version"])


def _version_key(version: str) -> tuple[int, int, int, int, int]:
    match = VERSION_RE.fullmatch(version)
    if match is None:
        raise ValueError(f"{version!r} is not a supported release version")
    major, minor, patch, prerelease, prerelease_num = match.groups()
    prerelease_rank = {"a": 0, "b": 1, "rc": 2, None: 3}[prerelease]
    return (
        int(major),
        int(minor),
        int(patch),
        prerelease_rank,
        int(prerelease_num or 0),
    )


def validate_version_bump(base_ref: str) -> list[str]:
    """Return errors for release-facing diffs that do not bump the version."""
    changed_files = _changed_release_files(base_ref)
    if not changed_files:
        return []

    current_version = _manifest_version_from_worktree()
    base_version = _manifest_version_from_ref(base_ref)
    if _version_key(current_version) <= _version_key(base_version):
        return [
            "custom_components/esphome_mcp changed but manifest version did not increase "
            f"over {base_ref}: {current_version!r} <= {base_version!r}"
        ]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "base_ref",
        nargs="?",
        default="origin/master",
        help="Base ref to compare against. Defaults to origin/master.",
    )
    args = parser.parse_args(argv)

    errors = validate_version_bump(args.base_ref)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Version bump check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
