"""Dependencies supplied by newer HA Core but absent from older supported Core."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

from packaging.requirements import Requirement

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

HTTPX2_REQUIREMENT = "httpx2>=2.10.0"


def _has_compatible_httpx2() -> bool:
    """Inspect metadata in the executor without importing the HTTP client."""
    try:
        installed = version("httpx2")
    except PackageNotFoundError:
        return False
    return Requirement(HTTPX2_REQUIREMENT).specifier.contains(installed, prereleases=True)


async def async_ensure_runtime_compat(hass: HomeAssistant) -> None:
    """Leave Core's compatible httpx2 alone; let HA install it on older Core.

    Current hassfest forbids declaring Core-owned packages in the manifest.
    HA 2026.10 still uses httpx, so it needs this compatibility dependency before
    importing our vendored MCP runtime. HA's installer retains its normal package
    constraints and dependency lock; no independent pip process is launched.
    """
    if await hass.async_add_executor_job(_has_compatible_httpx2):
        return
    from homeassistant.requirements import async_process_requirements

    await async_process_requirements(hass, "esphome_mcp", [HTTPX2_REQUIREMENT])
