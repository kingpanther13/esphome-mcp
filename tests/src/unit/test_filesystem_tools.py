"""Behavioral checks for confined ESPHome filesystem creation."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location(
    "esphome_filesystem_tools",
    ROOT / "custom_components/esphome_mcp/filesystem_tools.py",
)
assert spec is not None and spec.loader is not None
filesystem = importlib.util.module_from_spec(spec)
spec.loader.exec_module(filesystem)


def test_nested_directory_and_file_creation(tmp_path: Path) -> None:
    result = filesystem._create(str(tmp_path), "packages/common", True, None)
    assert result == {"success": True, "path": "packages/common", "created": True}
    assert filesystem._create(str(tmp_path), "packages/common", True, None)["created"] is False
    result = filesystem._create(str(tmp_path), "packages/common/base.yaml", False, "# café\n")
    assert result["success"]
    assert (tmp_path / "packages/common/base.yaml").read_text() == "# café\n"
    assert not filesystem._create(str(tmp_path), "packages/common/base.yaml", True, "new")[
        "success"
    ]
    assert (tmp_path / "packages/common/base.yaml").read_text() == "# café\n"


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/etc",
        "../escape",
        "a/../../escape",
        "a//b",
        "a/",
        ".",
        "a/./b",
        "a\\b",
        "a\x00b",
        ".esphome",
        "a/.git/config",
    ],
)
def test_invalid_paths_do_not_mutate_root(tmp_path: Path, path: str) -> None:
    assert filesystem._create(str(tmp_path), path, True, None)["error_code"] == "invalid_path"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("content", [None, "test"])
def test_symlinks_are_refused(tmp_path: Path, content: str | None) -> None:
    root = tmp_path / "esphome"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    assert not filesystem._create(str(root), "linked/new", True, content)["success"]
    assert not filesystem._create(str(root), "linked", True, content)["success"]
    assert not filesystem._create(str(root / "linked"), "new", True, content)["success"]
    assert list(outside.iterdir()) == []


def test_missing_root_parents_and_file_collision(tmp_path: Path) -> None:
    assert not filesystem._create(str(tmp_path / "missing"), "new", True, None)["success"]
    assert not (tmp_path / "missing").exists()
    assert not filesystem._create(str(tmp_path), "missing/new", False, None)["success"]
    (tmp_path / "file").write_text("original")
    assert not filesystem._create(str(tmp_path), "file", True, None)["success"]
    assert not filesystem._create(str(tmp_path), "file/new", True, None)["success"]
    assert (tmp_path / "file").read_text() == "original"


def test_ha_executor_uses_config_directory(tmp_path: Path) -> None:
    root = tmp_path / "esphome"
    root.mkdir()
    calls = []

    async def executor(func, *args):
        calls.append(args)
        return func(*args)

    hass = SimpleNamespace(
        config=SimpleNamespace(path=lambda path: str(tmp_path / path)),
        async_add_executor_job=executor,
    )
    result = asyncio.run(filesystem.create_esphome_path(hass, path="packages", parents=True))
    assert result["success"]
    assert calls == [(str(root), "packages", True, None)]
    assert (root / "packages").is_dir()
