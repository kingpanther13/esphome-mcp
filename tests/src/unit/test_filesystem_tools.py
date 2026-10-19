"""Behavioral checks for confined ESPHome filesystem creation."""

from __future__ import annotations

import asyncio
import importlib.util
import os
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
    assert filesystem._create(str(tmp_path), "packages/common/base.yaml", True, "new")[
        "error_code"
    ] == "already_exists"
    assert (tmp_path / "packages/common/base.yaml").read_text() == "# café\n"


def test_file_creation_makes_missing_parent_folders_by_default(tmp_path: Path) -> None:
    root = tmp_path / "esphome"
    root.mkdir()

    async def executor(func, *args):
        return func(*args)

    hass = SimpleNamespace(
        config=SimpleNamespace(path=lambda _path: str(root)),
        async_add_executor_job=executor,
    )
    result = asyncio.run(
        filesystem.create_esphome_path(hass, path="packages/common/base.txt", content="content")
    )
    assert result == {"success": True, "path": "packages/common/base.txt", "created": True}
    assert (root / "packages/common/base.txt").read_text() == "content"


def test_top_level_yaml_is_refused_but_nested_yaml_is_allowed(tmp_path: Path) -> None:
    result = filesystem._create(str(tmp_path), "kitchen.yaml", True, "esphome: {}")
    assert result["error_code"] == "device_yaml_not_allowed"
    assert not (tmp_path / "kitchen.yaml").exists()
    assert filesystem._create(str(tmp_path), "packages/kitchen.yaml", True, "substitutions: {}")[
        "success"
    ]


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
    assert (
        filesystem._create(str(root), "linked/new", True, content)["error_code"]
        == "symlink_refused"
    )
    assert filesystem._create(str(root), "linked", True, content)["error_code"] == "symlink_refused"
    assert (
        filesystem._create(str(root / "linked"), "new", True, content)["error_code"]
        == "symlink_refused"
    )
    assert list(outside.iterdir()) == []


def test_missing_root_parents_and_file_collision(tmp_path: Path) -> None:
    assert (
        filesystem._create(str(tmp_path / "missing"), "new", True, None)["error_code"]
        == "esphome_dir_missing"
    )
    assert not (tmp_path / "missing").exists()
    assert (
        filesystem._create(str(tmp_path), "missing/new", False, None)["error_code"]
        == "parent_not_found"
    )
    (tmp_path / "file").write_text("original")
    assert filesystem._create(str(tmp_path), "file", True, None)["error_code"] == "already_exists"
    assert (
        filesystem._create(str(tmp_path), "file/new", True, None)["error_code"]
        == "not_a_directory"
    )
    assert (tmp_path / "file").read_text() == "original"


def test_symlink_root_is_reported_as_refused(tmp_path: Path) -> None:
    actual_root = tmp_path / "actual"
    actual_root.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(actual_root, target_is_directory=True)
    assert (
        filesystem._create(str(linked_root), "new", True, None)["error_code"]
        == "symlink_refused"
    )


def test_failed_file_write_removes_new_leaf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "esphome"
    root.mkdir()

    class FailingWriter:
        def __init__(self, fd: int, _mode: str, **_kwargs: object) -> None:
            self.fd = fd

        def __enter__(self) -> FailingWriter:
            return self

        def __exit__(self, *_args: object) -> None:
            os.close(self.fd)

        def write(self, _content: str) -> None:
            raise OSError("disk full")

    monkeypatch.setattr(filesystem.os, "fdopen", FailingWriter)
    result = filesystem._create(str(root), "packages/failed.txt", True, "content")
    assert result["error_code"] == "filesystem_error"
    assert not (root / "packages/failed.txt").exists()


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
