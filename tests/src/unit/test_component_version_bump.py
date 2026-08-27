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


_CONTRACT_TEMPLATE = '''"""Test contract."""

HA_MCP_MASTER_SHA = "{sha}"
HA_MCP_SERVER_REQUIREMENTS = ("fastmcp=={fastmcp}",)
'''


def _write_contract(root: Path, *, sha: str, fastmcp: str = "1.0.0") -> None:
    runtime = root / "custom_components" / "esphome_mcp" / "ha_mcp_runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "contract.py").write_text(_CONTRACT_TEMPLATE.format(sha=sha, fastmcp=fastmcp))


def _initialize_release_repo(root: Path, version: str) -> None:
    _write_versions(root, version)
    _write_contract(root, sha="a" * 40)
    (root / "ci.yml").write_text("name: ci\n")
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


def _touch_component(root: Path) -> None:
    const = root / "custom_components" / "esphome_mcp" / "const.py"
    const.write_text(const.read_text() + "\n# component-facing change\n")


def test_bump_updates_every_release_version_source(tmp_path: Path) -> None:
    """Dropping any one metadata write would leave Renovate's PR unreleasable."""
    module = _load_bump_script()
    _initialize_release_repo(tmp_path, "1.2.3")
    module.ROOT = tmp_path
    _touch_component(tmp_path)

    assert module.bump_component_version("HEAD") == "1.2.4"
    assert _release_versions(tmp_path) == ("1.2.4", "1.2.4", "1.2.4")


def test_bump_is_idempotent_after_renovate_reruns(tmp_path: Path) -> None:
    """A repeated post-upgrade task must not turn one update into many releases."""
    module = _load_bump_script()
    _initialize_release_repo(tmp_path, "2.4.8")
    module.ROOT = tmp_path
    _touch_component(tmp_path)

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


def _load_check_script() -> ModuleType:
    script = ROOT / "scripts" / "check_version_bump.py"
    spec = importlib.util.spec_from_file_location("component_version_check", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bump_skips_when_nothing_reaches_the_component(tmp_path: Path) -> None:
    """CI-only or no-op updates must not cut a release."""
    module = _load_bump_script()
    _initialize_release_repo(tmp_path, "1.2.3")
    module.ROOT = tmp_path
    (tmp_path / "ci.yml").write_text("name: ci\non: push\n")

    assert module.bump_component_version("HEAD") == "1.2.3"
    assert _release_versions(tmp_path) == ("1.2.3", "1.2.3", "1.2.3")


def test_bump_skips_a_sha_only_contract_update(tmp_path: Path) -> None:
    """A server-only upstream move changes just the pinned SHA -- no release."""
    module = _load_bump_script()
    _initialize_release_repo(tmp_path, "1.2.3")
    module.ROOT = tmp_path
    _write_contract(tmp_path, sha="b" * 40)

    assert module.bump_component_version("HEAD") == "1.2.3"
    assert _release_versions(tmp_path) == ("1.2.3", "1.2.3", "1.2.3")


def test_bump_releases_a_contract_update_that_changes_requirements(tmp_path: Path) -> None:
    """A contract change beyond the SHA reaches the component and must release."""
    module = _load_bump_script()
    _initialize_release_repo(tmp_path, "1.2.3")
    module.ROOT = tmp_path
    _write_contract(tmp_path, sha="b" * 40, fastmcp="2.0.0")

    assert module.bump_component_version("HEAD") == "1.2.4"
    assert _release_versions(tmp_path) == ("1.2.4", "1.2.4", "1.2.4")


def test_check_requires_a_bump_only_for_component_facing_changes(tmp_path: Path) -> None:
    """The CI check must accept sha-only and CI-only diffs without a bump."""
    module = _load_check_script()
    _initialize_release_repo(tmp_path, "1.2.3")
    module.ROOT = tmp_path

    _write_contract(tmp_path, sha="b" * 40)
    (tmp_path / "ci.yml").write_text("name: ci\non: push\n")
    assert module.validate_version_bump("HEAD") == []

    _touch_component(tmp_path)
    errors = module.validate_version_bump("HEAD")
    assert len(errors) == 1
    assert "did not increase" in errors[0]


def test_check_rejects_anything_but_a_patch_bump(tmp_path: Path) -> None:
    """Only patch releases are ever cut from this repository."""
    module = _load_check_script()
    _initialize_release_repo(tmp_path, "1.2.3")
    module.ROOT = tmp_path
    _touch_component(tmp_path)

    for bad in ("1.3.0", "2.0.0"):
        _write_versions(tmp_path, bad)
        errors = module.validate_version_bump("HEAD")
        assert len(errors) == 1
        assert "only cuts patch releases" in errors[0]

    _write_versions(tmp_path, "1.2.4")
    assert module.validate_version_bump("HEAD") == []
