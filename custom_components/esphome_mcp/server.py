"""FastMCP server for ESPHome data exposed by Home Assistant."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from homeassistant.core import HomeAssistant
from pydantic import Field

from .addon_tools import (
    follow_device_builder_firmware_job,
    get_device_builder_firmware_job,
    list_device_builder_devices,
    list_device_builder_firmware_jobs,
    manage_esphome_addon,
    queue_device_builder_firmware_job,
    read_device_builder_config,
    run_device_builder_stream,
    search_device_builder_yaml,
    write_device_builder_config,
)
from .const import DOMAIN, VERSION


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
        async def esp_list_devices(
            query: str | None = None,
            area: str | None = None,
            config_entry_state: str | None = None,
            limit: int = 100,
        ) -> dict[str, Any]:
            """Search ESPHome devices known to Home Assistant."""
            snapshot = await _run_on_hass(self.hass, _async_snapshot(self.hass))
            devices = [
                device
                for device in snapshot["devices"]
                if _matches_query(device, query)
                and _matches_optional(device.get("area"), area)
                and _matches_optional(
                    device.get("config_entry_states"),
                    config_entry_state,
                )
            ][: max(0, min(limit, 500))]
            return {"success": True, "count": len(devices), "devices": devices}

        @self.mcp.tool(name="esp_list_entities")
        async def esp_list_entities(
            query: str | None = None,
            domain: str | None = None,
            device_id: str | None = None,
            state: str | None = None,
            disabled: bool | None = None,
            limit: int = 200,
        ) -> dict[str, Any]:
            """Search ESPHome entities known to Home Assistant."""
            snapshot = await _run_on_hass(self.hass, _async_snapshot(self.hass))
            entities = [
                entity
                for entity in snapshot["entities"]
                if _matches_query(entity, query)
                and _matches_optional(entity.get("domain"), domain)
                and _matches_optional(entity.get("device_id"), device_id)
                and _matches_optional(entity.get("state"), state)
                and (disabled is None or bool(entity.get("disabled_by")) is disabled)
            ][: max(0, min(limit, 1000))]
            return {"success": True, "count": len(entities), "entities": entities}

        @self.mcp.tool(
            name="esp_manage_addon",
            annotations={
                "destructiveHint": True,
                "idempotentHint": False,
                "readOnlyHint": False,
                "title": "Manage ESPHome Add-on",
            },
        )
        async def esp_manage_addon(
            slug: Annotated[
                str | None,
                Field(
                    description=(
                        "ESPHome add-on slug. Omit to auto-detect the installed ESPHome add-on."
                    ),
                    default=None,
                ),
            ] = None,
            action: Annotated[
                Literal[
                    "install",
                    "update",
                    "rebuild",
                    "start",
                    "stop",
                    "restart",
                    "uninstall",
                ]
                | None,
                Field(
                    description="Supervisor lifecycle action for the ESPHome add-on.",
                    default=None,
                ),
            ] = None,
            path: Annotated[
                str | None,
                Field(
                    description=(
                        "ESPHome dashboard API path. Defaults to /devices when "
                        "omitted and no action/config update is requested. Use /ws "
                        "with websocket=True for dashboard commands."
                    ),
                    default=None,
                ),
            ] = None,
            method: Annotated[
                Literal["GET", "POST", "PUT", "DELETE", "PATCH"],
                Field(
                    description="HTTP method for non-WebSocket proxy mode.",
                    default="GET",
                ),
            ] = "GET",
            body: Annotated[
                dict[str, Any] | list[Any] | str | None,
                Field(
                    description=(
                        "HTTP request body, or the initial WebSocket message when websocket=True."
                    ),
                    default=None,
                ),
            ] = None,
            websocket: Annotated[
                bool,
                Field(
                    description="Use the ESPHome dashboard WebSocket command channel.",
                    default=False,
                ),
            ] = False,
            wait_for_close: Annotated[
                bool,
                Field(
                    description=(
                        "WebSocket: wait for server close/timeout. Set false for "
                        "one-shot commands or bounded logs."
                    ),
                    default=True,
                ),
            ] = True,
            message_limit: Annotated[
                int | None,
                Field(
                    description=("WebSocket: maximum messages returned after message_offset."),
                    default=None,
                ),
            ] = None,
            message_offset: Annotated[
                int,
                Field(
                    description=("WebSocket: skip this many collected messages before returning."),
                    default=0,
                ),
            ] = 0,
            options: Annotated[
                dict[str, Any] | None,
                Field(
                    description=(
                        "Supervisor config update: add-on options to merge into current options."
                    ),
                    default=None,
                ),
            ] = None,
            network: Annotated[
                dict[str, Any] | None,
                Field(
                    description="Supervisor config update: host port mappings.",
                    default=None,
                ),
            ] = None,
            boot: Annotated[
                str | None,
                Field(
                    description=(
                        "Supervisor config update: boot strategy, such as auto or manual."
                    ),
                    default=None,
                ),
            ] = None,
            auto_update: Annotated[
                bool | None,
                Field(
                    description=("Supervisor config update: enable or disable auto-update."),
                    default=None,
                ),
            ] = None,
            watchdog: Annotated[
                bool | None,
                Field(
                    description=("Supervisor config update: enable or disable watchdog."),
                    default=None,
                ),
            ] = None,
            port: Annotated[
                int | None,
                Field(
                    description=(
                        "Proxy mode: connect to this add-on container port instead of ingress."
                    ),
                    default=None,
                ),
            ] = None,
            timeout: Annotated[
                int,
                Field(description="Request timeout seconds.", default=60, ge=1, le=1800),
            ] = 60,
            debug: Annotated[
                bool,
                Field(
                    description="Include diagnostic routing details.",
                    default=False,
                ),
            ] = False,
            request_headers: Annotated[
                dict[str, str] | None,
                Field(
                    description="HTTP proxy mode: extra request headers.",
                    default=None,
                ),
            ] = None,
        ) -> dict[str, Any]:
            """Manage the ESPHome add-on and call its dashboard API."""
            return await _run_on_hass(
                self.hass,
                manage_esphome_addon(
                    self.hass,
                    slug=slug,
                    action=action,
                    path=path,
                    method=method,
                    body=body,
                    websocket=websocket,
                    wait_for_close=wait_for_close,
                    message_limit=message_limit,
                    message_offset=message_offset,
                    options=options,
                    network=network,
                    boot=boot,
                    auto_update=auto_update,
                    watchdog=watchdog,
                    port=port,
                    timeout=timeout,
                    debug=debug,
                    request_headers=request_headers,
                ),
            )

        @self.mcp.tool(
            name="esp_dashboard_devices",
            annotations={
                "readOnlyHint": True,
                "idempotentHint": True,
                "destructiveHint": False,
                "title": "List ESPHome Dashboard Devices",
            },
        )
        async def esp_dashboard_devices(
            slug: str | None = None,
            query: str | None = None,
            state: Literal["unknown", "online", "offline"] | None = None,
            include_importable: bool = True,
            limit: int = 100,
            timeout: int = 60,
            debug: bool = False,
        ) -> dict[str, Any]:
            """List/search devices from ESPHome Device Builder's current API."""
            return await _run_on_hass(
                self.hass,
                list_device_builder_devices(
                    self.hass,
                    slug=slug,
                    query=query,
                    state=state,
                    include_importable=include_importable,
                    limit=limit,
                    timeout=timeout,
                    debug=debug,
                ),
            )

        @self.mcp.tool(
            name="esp_search_yaml",
            annotations={
                "readOnlyHint": True,
                "idempotentHint": True,
                "destructiveHint": False,
                "title": "Search ESPHome YAML",
            },
        )
        async def esp_search_yaml(
            query: str,
            slug: str | None = None,
            max_results: int = 50,
            case_sensitive: bool = False,
            context_lines: int | None = None,
            timeout: int = 60,
            debug: bool = False,
        ) -> dict[str, Any]:
            """Search raw YAML across ESPHome Device Builder configurations."""
            return await _run_on_hass(
                self.hass,
                search_device_builder_yaml(
                    self.hass,
                    slug=slug,
                    query=query,
                    max_results=max_results,
                    case_sensitive=case_sensitive,
                    context_lines=context_lines,
                    timeout=timeout,
                    debug=debug,
                ),
            )

        @self.mcp.tool(
            name="esp_get_yaml",
            annotations={
                "readOnlyHint": True,
                "idempotentHint": True,
                "destructiveHint": False,
                "title": "Read ESPHome YAML",
            },
        )
        async def esp_get_yaml(
            configuration: str,
            slug: str | None = None,
            timeout: int = 60,
            debug: bool = False,
        ) -> dict[str, Any]:
            """Read one ESPHome YAML file by configuration filename."""
            return await _run_on_hass(
                self.hass,
                read_device_builder_config(
                    self.hass,
                    slug=slug,
                    configuration=configuration,
                    timeout=timeout,
                    debug=debug,
                ),
            )

        @self.mcp.tool(
            name="esp_update_yaml",
            annotations={
                "readOnlyHint": False,
                "idempotentHint": False,
                "destructiveHint": True,
                "title": "Update ESPHome YAML",
            },
        )
        async def esp_update_yaml(
            configuration: str,
            content: str,
            slug: str | None = None,
            allow_wipe: bool = False,
            timeout: int = 60,
            debug: bool = False,
        ) -> dict[str, Any]:
            """Write one ESPHome YAML file through Device Builder guards."""
            return await _run_on_hass(
                self.hass,
                write_device_builder_config(
                    self.hass,
                    slug=slug,
                    configuration=configuration,
                    content=content,
                    allow_wipe=allow_wipe,
                    timeout=timeout,
                    debug=debug,
                ),
            )

        @self.mcp.tool(
            name="esp_validate_yaml",
            annotations={
                "readOnlyHint": True,
                "idempotentHint": True,
                "destructiveHint": False,
                "title": "Validate ESPHome YAML",
            },
        )
        async def esp_validate_yaml(
            configuration: str,
            slug: str | None = None,
            show_secrets: bool = False,
            message_limit: int = 200,
            timeout: int = 300,
            debug: bool = False,
        ) -> dict[str, Any]:
            """Run Device Builder's devices/validate stream for one YAML."""
            return await _run_on_hass(
                self.hass,
                run_device_builder_stream(
                    self.hass,
                    slug=slug,
                    command="devices/validate",
                    args={
                        "configuration": configuration,
                        "show_secrets": show_secrets,
                    },
                    timeout=timeout,
                    debug=debug,
                    message_limit=message_limit,
                ),
            )

        @self.mcp.tool(
            name="esp_device_logs",
            annotations={
                "readOnlyHint": True,
                "idempotentHint": False,
                "destructiveHint": False,
                "title": "Read ESPHome Device Logs",
            },
        )
        async def esp_device_logs(
            configuration: str,
            slug: str | None = None,
            port: str = "OTA",
            no_states: bool = False,
            message_limit: int = 120,
            timeout: int = 60,
            debug: bool = False,
        ) -> dict[str, Any]:
            """Collect a bounded batch from Device Builder's devices/logs stream."""
            return await _run_on_hass(
                self.hass,
                run_device_builder_stream(
                    self.hass,
                    slug=slug,
                    command="devices/logs",
                    args={
                        "configuration": configuration,
                        "port": port,
                        "no_states": no_states,
                    },
                    timeout=timeout,
                    debug=debug,
                    message_limit=message_limit,
                ),
            )

        @self.mcp.tool(
            name="esp_compile_firmware",
            annotations={
                "readOnlyHint": False,
                "idempotentHint": False,
                "destructiveHint": True,
                "title": "Compile ESPHome Firmware",
            },
        )
        async def esp_compile_firmware(
            configuration: str,
            slug: str | None = None,
            force_local: bool = False,
            timeout: int = 60,
            debug: bool = False,
        ) -> dict[str, Any]:
            """Queue a Device Builder firmware/compile job."""
            return await _run_on_hass(
                self.hass,
                queue_device_builder_firmware_job(
                    self.hass,
                    slug=slug,
                    command="firmware/compile",
                    args={"configuration": configuration, "force_local": force_local},
                    timeout=timeout,
                    debug=debug,
                ),
            )

        @self.mcp.tool(
            name="esp_install_firmware",
            annotations={
                "readOnlyHint": False,
                "idempotentHint": False,
                "destructiveHint": True,
                "title": "Install ESPHome Firmware",
            },
        )
        async def esp_install_firmware(
            configuration: str,
            slug: str | None = None,
            port: str = "OTA",
            force_local: bool = False,
            bootloader: bool = False,
            timeout: int = 60,
            debug: bool = False,
        ) -> dict[str, Any]:
            """Queue a Device Builder firmware/install job."""
            return await _run_on_hass(
                self.hass,
                queue_device_builder_firmware_job(
                    self.hass,
                    slug=slug,
                    command="firmware/install",
                    args={
                        "configuration": configuration,
                        "port": port,
                        "force_local": force_local,
                        "bootloader": bootloader,
                    },
                    timeout=timeout,
                    debug=debug,
                ),
            )

        @self.mcp.tool(
            name="esp_firmware_jobs",
            annotations={
                "readOnlyHint": True,
                "idempotentHint": True,
                "destructiveHint": False,
                "title": "List ESPHome Firmware Jobs",
            },
        )
        async def esp_firmware_jobs(
            slug: str | None = None,
            status: Literal[
                "queued",
                "running",
                "completed",
                "failed",
                "cancelled",
            ]
            | None = None,
            configuration: str | None = None,
            limit: int = 50,
            timeout: int = 60,
            debug: bool = False,
        ) -> dict[str, Any]:
            """List Device Builder firmware jobs with optional filters."""
            return await _run_on_hass(
                self.hass,
                list_device_builder_firmware_jobs(
                    self.hass,
                    slug=slug,
                    status=status,
                    configuration=configuration,
                    limit=limit,
                    timeout=timeout,
                    debug=debug,
                ),
            )

        @self.mcp.tool(
            name="esp_get_firmware_job",
            annotations={
                "readOnlyHint": True,
                "idempotentHint": True,
                "destructiveHint": False,
                "title": "Get ESPHome Firmware Job",
            },
        )
        async def esp_get_firmware_job(
            job_id: str,
            slug: str | None = None,
            timeout: int = 60,
            debug: bool = False,
        ) -> dict[str, Any]:
            """Return one Device Builder firmware job."""
            return await _run_on_hass(
                self.hass,
                get_device_builder_firmware_job(
                    self.hass,
                    slug=slug,
                    job_id=job_id,
                    timeout=timeout,
                    debug=debug,
                ),
            )

        @self.mcp.tool(
            name="esp_follow_firmware_job",
            annotations={
                "readOnlyHint": True,
                "idempotentHint": False,
                "destructiveHint": False,
                "title": "Follow ESPHome Firmware Job",
            },
        )
        async def esp_follow_firmware_job(
            job_id: str,
            slug: str | None = None,
            message_limit: int = 500,
            timeout: int = 1800,
            debug: bool = False,
        ) -> dict[str, Any]:
            """Follow Device Builder's firmware/follow_job stream."""
            return await _run_on_hass(
                self.hass,
                follow_device_builder_firmware_job(
                    self.hass,
                    slug=slug,
                    job_id=job_id,
                    message_limit=message_limit,
                    timeout=timeout,
                    debug=debug,
                ),
            )


async def _run_on_hass[T](hass: HomeAssistant, coro: Awaitable[T]) -> T:
    """Run a coroutine on Home Assistant's event loop from the MCP worker loop."""
    future = asyncio.run_coroutine_threadsafe(coro, hass.loop)
    return await asyncio.wrap_future(future)


async def _call_on_hass[T](hass: HomeAssistant, func: Callable[[], T]) -> T:
    """Run a synchronous callback on Home Assistant's event loop."""
    future: concurrent.futures.Future[T] = concurrent.futures.Future()

    def _run() -> None:
        try:
            future.set_result(func())
        except Exception as err:  # pragma: no cover - defensive bridge guard
            future.set_exception(err)

    hass.loop.call_soon_threadsafe(_run)
    return await asyncio.wrap_future(future)


def _matches_query(payload: dict[str, Any], query: str | None) -> bool:
    """Return True when query is absent or contained in the serialized payload."""
    if not query:
        return True
    return query.lower() in json.dumps(payload, default=str).lower()


def _matches_optional(value: Any, expected: str | None) -> bool:
    """Return True when expected is absent or matches a scalar/list value."""
    if expected is None:
        return True
    needle = expected.lower()
    if isinstance(value, list):
        return any(needle in str(item).lower() for item in value)
    return needle in str(value or "").lower()


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
    entry_states = {entry.entry_id: str(entry.state) for entry in entries}

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
                "area": getattr(device, "area_id", None),
                "config_entries": sorted(config_entries),
                "config_entry_states": [
                    entry_states[entry_id]
                    for entry_id in sorted(config_entries & entry_ids)
                    if entry_id in entry_states
                ],
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
                "domain": entity.entity_id.split(".", 1)[0],
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
