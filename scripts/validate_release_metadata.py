"""Validate release metadata used by HACS and GitHub releases."""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?$")


def _load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text())


def _normalize_version(version: str) -> str:
    return version.removeprefix("v")


def _load_const_version(path: Path) -> str | None:
    module = ast.parse(path.read_text())
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


def validate_release_metadata(expected_version: str | None = None) -> list[str]:
    """Return metadata errors that would make a HACS release unsafe."""
    errors: list[str] = []

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    manifest = _load_json(ROOT / "custom_components" / "esphome_mcp" / "manifest.json")
    hacs = _load_json(ROOT / "hacs.json")

    project_version = str(pyproject["project"]["version"])
    manifest_version = str(manifest["version"])
    const_version = _load_const_version(ROOT / "custom_components" / "esphome_mcp" / "const.py")

    if project_version != manifest_version:
        errors.append(
            "pyproject.toml project.version must match "
            "custom_components/esphome_mcp/manifest.json version"
        )
    if const_version != manifest_version:
        errors.append(
            "const.VERSION must match custom_components/esphome_mcp/manifest.json version"
        )

    if expected_version is not None:
        normalized_expected = _normalize_version(expected_version)
        if not VERSION_PATTERN.fullmatch(normalized_expected):
            errors.append(f"release version {expected_version!r} must look like 0.1.0 or v0.1.0")
        if normalized_expected != manifest_version:
            errors.append(
                f"release version {normalized_expected!r} must match manifest version "
                f"{manifest_version!r}"
            )

    if hacs.get("name") != "ESPHome MCP":
        errors.append("root hacs.json must define the HACS display name")
    if hacs.get("homeassistant") != "2025.9.1":
        errors.append("root hacs.json must preserve the minimum Home Assistant version")
    if hacs.get("render_readme") is not True:
        errors.append("root hacs.json must keep render_readme enabled")
    if hacs.get("hide_default_branch") is not True:
        errors.append("root hacs.json must hide default branch downloads so HACS installs releases")
    if hacs.get("zip_release"):
        errors.append(
            "zip_release is intentionally not used; HACS should install the release tag archive"
        )
    if "filename" in hacs:
        errors.append("filename is only valid for zip_release/single-file HACS content")
    if (ROOT / "custom_components" / "esphome_mcp" / "hacs.json").exists():
        errors.append("hacs.json belongs at the repository root, not inside the component")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "version",
        nargs="?",
        help="Release version to validate, with or without a leading v prefix.",
    )
    args = parser.parse_args(argv)

    errors = validate_release_metadata(args.version)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("Release metadata is valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
