"""Confined creation of ESPHome configuration folders and supporting files."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


def _create(root: str, path: str, parents: bool, content: str | None) -> dict[str, Any]:
    """Walk directory descriptors without following symlinks, then create a leaf."""
    parts = path.split("/")
    if (
        not path
        or any(part in {"", ".", ".."} or part.startswith(".") for part in parts)
        or "\\" in path
        or "\x00" in path
    ):
        return {
            "success": False,
            "error_code": "invalid_path",
            "error": "Use a relative path without hidden names, empty parts, or traversal.",
        }
    descriptor = None
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        # Never create the root: an absent shared mount must fail visibly.
        descriptor = os.open(root, flags)
        for part in parts[:-1]:
            if parents:
                try:
                    os.mkdir(part, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        if content is None:
            created = True
            try:
                os.mkdir(parts[-1], dir_fd=descriptor)
            except FileExistsError:
                # An existing directory is success; files and symlinks are errors.
                child = os.open(parts[-1], flags, dir_fd=descriptor)
                os.close(child)
                created = False
        else:
            file_fd = os.open(
                parts[-1],
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o644,
                dir_fd=descriptor,
            )
            with os.fdopen(file_fd, "w", encoding="utf-8") as file:
                file.write(content)
            created = True
        return {"success": True, "path": path, "created": created}
    except OSError as err:
        return {
            "success": False,
            "error_code": "filesystem_error",
            "error": str(err),
            "path": path,
        }
    finally:
        if descriptor is not None:
            os.close(descriptor)


async def create_esphome_path(
    hass: HomeAssistant, *, path: str, parents: bool, content: str | None = None
) -> dict[str, Any]:
    """Create under HA's shared ESPHome config root, off the event loop."""
    return await hass.async_add_executor_job(
        _create, hass.config.path("esphome"), path, parents, content
    )
