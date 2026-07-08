"""FastMCP server for ESPHome data exposed by Home Assistant."""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from fastmcp import FastMCP
from homeassistant.core import HomeAssistant

from .const import DOMAIN, VERSION

_T = TypeVar("_T")


class EspHomeMCPServer:
    """Small ESPHome-focused MCP server running inside Home Assistant."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Create the server and register the initial tool surface."""
        self.hass = hass
        self.mcp = FastMCP(
            name="esphome-mcp",
            version=VERSION,
            instructions=(
                "ESPHome MCP exposes ESPHome device and entity context from "
                "Home Assistant. Tool names use the esp_ prefix."
            ),
        )
        self._register_tools()

    def _register_tools(self) -> None:
        """Register the initial ESPHome tool set."""

        @self.mcp.tool(name="esp_overview")
        async def esp_overview() -> dict[str, Any]:
            """Return counts and status for the Home Assistant ESPHome integration."""
            return await _run_on_hass(self.hass, _async_overview(self.hass))

        @self.mcp.tool(name="esp_list_devices")
        async def esp_list_devices(limit: int = 100) -> dict[str, Any]:
            """List ESPHome devices known to Home Assistant."""
            snapshot = await _run_on_hass(self.hass, _async_snapshot(self.hass))
            devices = snapshot["devices"][: max(0, min(limit, 500))]
            return {"success": True, "count": len(devices), "devices": devices}

        @self.mcp.tool(name="esp_list_entities")
        async def esp_list_entities(limit: int = 200) -> dict[str, Any]:
            """List ESPHome entities known to Home Assistant."""
            snapshot = await _run_on_hass(self.hass, _async_snapshot(self.hass))
            entities = snapshot["entities"][: max(0, min(limit, 1000))]
            return {"success": True, "count": len(entities), "entities": entities}


async def _run_on_hass(hass: HomeAssistant, coro: Awaitable[_T]) -> _T:
    """Run a coroutine on Home Assistant's event loop from the MCP worker loop."""
    future = asyncio.run_coroutine_threadsafe(coro, hass.loop)
    return await asyncio.wrap_future(future)


async def _call_on_hass(hass: HomeAssistant, func: Callable[[], _T]) -> _T:
    """Run a synchronous callback on Home Assistant's event loop."""
    future: concurrent.futures.Future[_T] = concurrent.futures.Future()

    def _run() -> None:
        try:
            future.set_result(func())
        except Exception as err:  # pragma: no cover - defensive bridge guard
            future.set_exception(err)

    hass.loop.call_soon_threadsafe(_run)
    return await asyncio.wrap_future(future)


async def _async_overview(hass: HomeAssistant) -> dict[str, Any]:
    """Build the overview payload on the Home Assistant event loop."""
    snapshot = await _async_snapshot(hass)
    return {
        "success": True,
        "integration_domain": "esphome",
        "mcp_domain": DOMAIN,
        "server_version": VERSION,
        "config_entry_count": len(snapshot["config_entries"]),
        "device_count": len(snapshot["devices"]),
        "entity_count": len(snapshot["entities"]),
    }


async def _async_snapshot(hass: HomeAssistant) -> dict[str, Any]:
    """Collect ESPHome config entries, devices, and entities."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    entries = list(hass.config_entries.async_entries("esphome"))
    entry_ids = {entry.entry_id for entry in entries}

    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)

    devices = []
    for device in device_registry.devices.values():
        config_entries = set(getattr(device, "config_entries", set()) or set())
        identifiers = sorted([list(item) for item in getattr(device, "identifiers", set())])
        if not (config_entries & entry_ids or any(item[0] == "esphome" for item in identifiers)):
            continue
        devices.append(
            {
                "id": device.id,
                "name": device.name_by_user or device.name,
                "manufacturer": device.manufacturer,
                "model": device.model,
                "sw_version": device.sw_version,
                "config_entries": sorted(config_entries),
                "identifiers": identifiers,
            }
        )

    entities = []
    for entity in entity_registry.entities.values():
        platform = getattr(entity, "platform", None)
        config_entry_id = getattr(entity, "config_entry_id", None)
        if platform != "esphome" and config_entry_id not in entry_ids:
            continue
        state = hass.states.get(entity.entity_id)
        entities.append(
            {
                "entity_id": entity.entity_id,
                "name": entity.name,
                "original_name": entity.original_name,
                "device_id": entity.device_id,
                "platform": platform,
                "disabled_by": str(entity.disabled_by) if entity.disabled_by else None,
                "state": state.state if state is not None else None,
            }
        )

    return {
        "config_entries": [
            {
                "entry_id": entry.entry_id,
                "title": entry.title,
                "state": str(entry.state),
                "disabled_by": str(entry.disabled_by) if entry.disabled_by else None,
            }
            for entry in entries
        ],
        "devices": sorted(devices, key=lambda item: (item.get("name") or "", item["id"])),
        "entities": sorted(entities, key=lambda item: item["entity_id"]),
    }


def register_status_routes(
    mcp: FastMCP,
    server: EspHomeMCPServer,
    secret_path: str,
) -> None:
    """Register a small status page under the same secret path as MCP."""
    from starlette.responses import HTMLResponse, JSONResponse

    prefix = secret_path.rstrip("/")
    if not prefix:
        return

    @mcp.custom_route(f"{prefix}/settings", methods=["GET"])
    async def settings_page(request: Any) -> HTMLResponse:
        """Return a minimal status page for the sidebar panel."""
        return HTMLResponse(
            """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ESPHome MCP</title>
  <style>
    body { margin: 0; font-family: system-ui, sans-serif; background: #f7f8fa; color: #111827; }
    main { max-width: 760px; margin: 0 auto; padding: 32px 20px; }
    h1 { font-size: 28px; font-weight: 650; margin: 0 0 12px; }
    p { line-height: 1.5; }
    code { background: #e5e7eb; padding: 2px 5px; border-radius: 4px; }
  </style>
</head>
<body>
  <main>
    <h1>ESPHome MCP</h1>
    <p>
      The in-process MCP server is running. Initial tools:
      <code>esp_overview</code>, <code>esp_list_devices</code>,
      <code>esp_list_entities</code>.
    </p>
  </main>
</body>
</html>"""
        )

    @mcp.custom_route(f"{prefix}/api/settings/info", methods=["GET"])
    async def settings_info(request: Any) -> JSONResponse:
        """Return basic server info."""
        overview = await _run_on_hass(server.hass, _async_overview(server.hass))
        return JSONResponse(overview)
