"""Tests for Renovate's ESPHome MCP component version bump."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = ROOT / "scripts" / "bump_component_version.py"


def _load_bump_script() -> ModuleType:
    assert SCRIPT_PATH.is_file(), "the Renovate component-version bump script is missing"
    spec = importlib.util.spec_from_file_location("component_version_bump", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_versions(root: Path, version: str) -> None:
    component = root / "custom_components" / "esphome_mcp"
    component.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "esphome-mcp-custom-component"\nversion = "{version}"\n'
    )
    (component / "manifest.json").write_text(
        json.dumps({"domain": "esphome_mcp", "version": version}, indent=2) + "\n"
    )
    (component / "const.py").write_text(
        f'"""Test component constants."""\n\nVERSION = "{version}"\n'
    )


def _release_versions(root: Path) -> tuple[str, str, str]:
    project = tomllib.loads((root / "pyproject.toml").read_text())
    manifest = json.loads(
        (root / "custom_components" / "esphome_mcp" / "manifest.json").read_text()
    )
    const = (root / "custom_components" / "esphome_mcp" / "const.py").read_text()
    const_version = const.split('VERSION = "', 1)[1].split('"', 1)[0]
    return str(project["project"]["version"]), str(manifest["version"]), const_version


def _initialize_release_repo(root: Path, version: str) -> None:
    _write_versions(root, version)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=ESPHome MCP Tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "-qm",
            "base release",
        ],
        cwd=root,
        check=True,
    )


def test_bump_updates_every_release_version_source(tmp_path: Path) -> None:
    """Dropping any one metadata write would leave Renovate's PR unreleasable."""
    module = _load_bump_script()
    _initialize_release_repo(tmp_path, "1.2.3")
    module.ROOT = tmp_path

    assert module.bump_component_version("HEAD") == "1.2.4"
    assert _release_versions(tmp_path) == ("1.2.4", "1.2.4", "1.2.4")


def test_bump_is_idempotent_after_renovate_reruns(tmp_path: Path) -> None:
    """A repeated post-upgrade task must not turn one update into many releases."""
    module = _load_bump_script()
    _initialize_release_repo(tmp_path, "2.4.8")
    module.ROOT = tmp_path

    assert module.bump_component_version("HEAD") == "2.4.9"
    first_result = {
        path: path.read_text()
        for path in (
            tmp_path / "pyproject.toml",
            tmp_path / "custom_components" / "esphome_mcp" / "manifest.json",
            tmp_path / "custom_components" / "esphome_mcp" / "const.py",
        )
    }

    assert module.bump_component_version("HEAD") == "2.4.9"
    assert {path: path.read_text() for path in first_result} == first_result


def test_bump_rejects_misaligned_current_metadata_without_writing(tmp_path: Path) -> None:
    """A partial prior edit must fail closed instead of being silently overwritten."""
    module = _load_bump_script()
    _initialize_release_repo(tmp_path, "3.0.0")
    component = tmp_path / "custom_components" / "esphome_mcp"
    (component / "const.py").write_text('VERSION = "3.0.1"\n')
    before = {
        path: path.read_text()
        for path in (
            tmp_path / "pyproject.toml",
            component / "manifest.json",
            component / "const.py",
        )
    }
    module.ROOT = tmp_path

    with pytest.raises(RuntimeError, match="release version metadata is not aligned"):
        module.bump_component_version("HEAD")

    assert {path: path.read_text() for path in before} == before
