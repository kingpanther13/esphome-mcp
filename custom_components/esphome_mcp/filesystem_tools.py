"""Confined creation of ESPHome configuration folders and supporting files."""

from __future__ import annotations

import errno
import logging
import os
import stat
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def _failure(path: str, code: str, message: str) -> dict[str, Any]:
    """Return and log a stable, actionable filesystem error."""
    _LOGGER.warning("ESPHome filesystem operation failed (%s) for %r: %s", code, path, message)
    return {"success": False, "error_code": code, "error": message, "path": path}


def _component_error(descriptor: int, name: str, path: str) -> dict[str, Any]:
    """Classify a component that could not be opened as a directory."""
    try:
        info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return _failure(path, "parent_not_found", f"Parent folder {name!r} does not exist.")
    if stat.S_ISLNK(info.st_mode):
        return _failure(path, "symlink_refused", f"Symlink component {name!r} is not allowed.")
    return _failure(path, "not_a_directory", f"Parent component {name!r} is not a folder.")


def _create(root: str, path: str, parents: bool, content: str | None) -> dict[str, Any]:
    """Walk directory descriptors without following symlinks, then create a leaf."""
    parts = path.split("/")
    if (
        not path
        or any(part in {"", ".", ".."} or part.startswith(".") for part in parts)
        or "\\" in path
        or "\x00" in path
    ):
        return _failure(
            path,
            "invalid_path",
            "Use a relative path without hidden names, empty parts, or traversal.",
        )
    if content is not None and len(parts) == 1 and parts[0].lower().endswith((".yaml", ".yml")):
        return _failure(
            path,
            "device_yaml_not_allowed",
            "Create supporting files in a subfolder; use Device Builder and esp_update_yaml "
            "for device YAML configurations.",
        )

    descriptor = None
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        # Never create the root: an absent shared mount must fail visibly.
        try:
            descriptor = os.open(root, flags)
        except FileNotFoundError:
            return _failure(
                path,
                "esphome_dir_missing",
                "Home Assistant's ESPHome config directory is missing; ensure it is the "
                "existing folder shared with Device Builder.",
            )
        except OSError as err:
            if err.errno in (errno.ELOOP, errno.ENOTDIR):
                return _failure(
                    path,
                    "symlink_refused",
                    "The ESPHome config root must be a real folder.",
                )
            raise

        for part in parts[:-1]:
            if parents:
                try:
                    os.mkdir(part, dir_fd=descriptor)
                except FileExistsError:
                    pass
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except OSError as err:
                if err.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENOENT):
                    return _component_error(descriptor, part, path)
                raise
            os.close(descriptor)
            descriptor = child

        if content is None:
            try:
                os.mkdir(parts[-1], dir_fd=descriptor)
            except FileExistsError:
                try:
                    child = os.open(parts[-1], flags, dir_fd=descriptor)
                except OSError as err:
                    if err.errno in (errno.ELOOP, errno.ENOTDIR):
                        try:
                            info = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
                        except FileNotFoundError:
                            raise err
                        code = "symlink_refused" if stat.S_ISLNK(info.st_mode) else "already_exists"
                        message = (
                            f"Path {path!r} is a symlink and was not followed."
                            if code == "symlink_refused"
                            else f"Path {path!r} already exists and is not a folder."
                        )
                        return _failure(path, code, message)
                    raise
                os.close(child)
                created = False
            else:
                created = True
        else:
            try:
                file_fd = os.open(
                    parts[-1],
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o644,
                    dir_fd=descriptor,
                )
            except FileExistsError:
                try:
                    info = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    return _failure(path, "filesystem_error", "The path changed during creation.")
                if stat.S_ISLNK(info.st_mode):
                    return _failure(
                        path,
                        "symlink_refused",
                        "The target is a symlink and was not followed.",
                    )
                return _failure(
                    path,
                    "already_exists",
                    "The path already exists and was left unchanged. Use esp_update_yaml for "
                    "existing device configurations; supporting files cannot be overwritten.",
                )

            created_info = os.fstat(file_fd)
            fdopen_fd = file_fd
            try:
                file = os.fdopen(file_fd, "w", encoding="utf-8")
                fdopen_fd = -1
                with file:
                    file.write(content)
            except BaseException:
                if fdopen_fd >= 0:
                    os.close(fdopen_fd)
                # Only remove the inode created by this call, even if another process races us.
                try:
                    current = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) == (
                        created_info.st_dev,
                        created_info.st_ino,
                    ):
                        os.unlink(parts[-1], dir_fd=descriptor)
                except FileNotFoundError:
                    pass
                raise
            created = True
        return {"success": True, "path": path, "created": created}
    except OSError as err:
        if err.errno in (errno.ELOOP,):
            return _failure(path, "symlink_refused", "Symlink paths are not allowed.")
        return _failure(path, "filesystem_error", str(err))
    finally:
        if descriptor is not None:
            os.close(descriptor)


async def create_esphome_path(
    hass: HomeAssistant, *, path: str, parents: bool = True, content: str | None = None
) -> dict[str, Any]:
    """Create under HA's shared ESPHome config root, off the event loop."""
    return await hass.async_add_executor_job(
        _create, hass.config.path("esphome"), path, parents, content
    )
