"""ESPHome add-on management helpers.

This is the ESPHome-focused starting point copied from ha-mcp's
``ha_manage_addon`` behavior: Supervisor lifecycle/config calls plus HTTP and
WebSocket proxying into an add-on's ingress endpoint.

ha-mcp reaches Supervisor by authenticating to Home Assistant's WebSocket API
and sending ``supervisor/api``. That HA WebSocket command immediately delegates
to the same loaded ``hassio`` client used here. This custom component runs
inside Home Assistant Core and the webhook auth layer has already validated the
admin user, so using the in-process ``hassio`` client is the equivalent local
bridge without inventing an unavailable external bearer token.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

import aiohttp

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_MAX_RESPONSE_SIZE = 50 * 1024
_MAX_WS_MESSAGES = 1000
_DEVICE_BUILDER_RESULT_EVENTS = {"result"}
_DEVICE_BUILDER_STOPPABLE_STREAMS = {
    "devices/logs",
    "devices/subscribe_reachability",
}

_VALID_HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}
_ACTION_ENDPOINTS: dict[str, tuple[str, int]] = {
    "install": ("/store/addons/{slug}/install", 1800),
    "update": ("/store/addons/{slug}/update", 1800),
    "rebuild": ("/addons/{slug}/rebuild", 1800),
    "start": ("/addons/{slug}/start", 120),
    "stop": ("/addons/{slug}/stop", 60),
    "restart": ("/addons/{slug}/restart", 120),
    "uninstall": ("/addons/{slug}/uninstall", 120),
}


def _error(message: str, *, code: str = "error", **extra: Any) -> dict[str, Any]:
    """Return a stable tool error payload without raising through FastMCP."""
    return {"success": False, "error_code": code, "error": message, **extra}


def _normalize_supervisor_payload(payload: Any) -> Any:
    """Normalize Supervisor responses from HA's ``hassio.send_command`` wrapper."""
    if not isinstance(payload, dict):
        return payload
    if payload.get("result") == "ok" and "data" in payload:
        return payload["data"]
    if "data" in payload and set(payload).issubset({"result", "data"}):
        return payload["data"]
    return payload


async def _supervisor_api_call(
    hass: HomeAssistant,
    endpoint: str,
    *,
    method: str = "GET",
    data: Any | None = None,
    timeout: int | None = 30,
) -> dict[str, Any]:
    """Call Supervisor through HA Core's loaded hassio client.

    HA's public WebSocket ``supervisor/api`` command does the same delegation
    after its admin check. ESPHome MCP's webhook layer performs the admin check
    before FastMCP receives the request.
    """
    try:
        from homeassistant.components.hassio.const import DATA_COMPONENT
        from homeassistant.components.hassio.handler import HassioAPIError
    except ImportError:
        return _error(
            "Home Assistant's hassio integration is not available.",
            code="supervisor_unavailable",
        )

    supervisor = hass.data.get(DATA_COMPONENT)
    if supervisor is None:
        return _error(
            "Supervisor is not loaded. This tool requires Home Assistant OS or Supervised.",
            code="supervisor_unavailable",
        )

    try:
        result = await supervisor.send_command(
            endpoint,
            method=method.lower(),
            payload=data,
            timeout=timeout,
            source="esphome_mcp.addon_tool",
        )
    except HassioAPIError as err:
        return _error(
            f"Supervisor API call failed: {endpoint}",
            code="supervisor_api_failed",
            details=str(err),
        )
    except Exception as err:
        return _error(
            f"Unexpected Supervisor API error calling {endpoint}",
            code="supervisor_api_failed",
            details=str(err),
        )

    return {"success": True, "result": _normalize_supervisor_payload(result)}


async def _get_addon_info(hass: HomeAssistant, slug: str) -> dict[str, Any]:
    """Return detailed Supervisor info for one add-on."""
    response = await _supervisor_api_call(hass, f"/addons/{slug}/info")
    if not response.get("success"):
        return response
    addon = response.get("result")
    if not isinstance(addon, dict):
        return _error("Supervisor returned malformed add-on info.", code="bad_response")
    return {"success": True, "addon": addon}


async def _list_addons(hass: HomeAssistant) -> dict[str, Any]:
    """Return installed add-ons from Supervisor."""
    response = await _supervisor_api_call(hass, "/addons")
    if not response.get("success"):
        return response
    data = response.get("result")
    addons = data.get("addons") if isinstance(data, dict) else None
    if not isinstance(addons, list):
        return _error("Supervisor returned malformed add-on list.", code="bad_response")
    return {"success": True, "addons": addons}


def _looks_like_esphome_addon(addon: dict[str, Any]) -> bool:
    """Return True when Supervisor metadata looks like the ESPHome add-on."""
    values = [
        str(addon.get("slug") or ""),
        str(addon.get("name") or ""),
        str(addon.get("description") or ""),
        str(addon.get("repository") or ""),
    ]
    return any("esphome" in value.lower() for value in values)


async def _resolve_esphome_addon(hass: HomeAssistant, slug: str | None) -> dict[str, Any]:
    """Resolve an explicit or auto-discovered ESPHome add-on."""
    if slug:
        info = await _get_addon_info(hass, slug)
        if not info.get("success"):
            return info
        addon = info["addon"]
        if not _looks_like_esphome_addon(addon):
            return _error(
                f"Add-on {slug!r} does not look like the ESPHome add-on.",
                code="not_esphome_addon",
                slug=slug,
            )
        return info

    listed = await _list_addons(hass)
    if not listed.get("success"):
        return listed
    matches = [addon for addon in listed["addons"] if _looks_like_esphome_addon(addon)]
    if not matches:
        return _error(
            "Could not auto-detect an installed ESPHome add-on. Pass slug explicitly.",
            code="esphome_addon_not_found",
        )
    if len(matches) > 1:
        return _error(
            "Multiple ESPHome-looking add-ons found. Pass slug explicitly.",
            code="ambiguous_esphome_addon",
            matches=[{"slug": addon.get("slug"), "name": addon.get("name")} for addon in matches],
        )
    return await _get_addon_info(hass, str(matches[0]["slug"]))


def _build_config_payload(
    *,
    options: dict[str, Any] | None,
    network: dict[str, Any] | None,
    boot: str | None,
    auto_update: bool | None,
    watchdog: bool | None,
) -> dict[str, Any]:
    """Build a Supervisor add-on options payload."""
    payload: dict[str, Any] = {}
    if options:
        payload["options"] = options
    if network:
        payload["network"] = network
    if boot is not None:
        payload["boot"] = boot
    if auto_update is not None:
        payload["auto_update"] = auto_update
    if watchdog is not None:
        payload["watchdog"] = watchdog
    return payload


def _merge_options(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge caller options into current options with one-level dict merge."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


async def _execute_action(hass: HomeAssistant, slug: str, action: str) -> dict[str, Any]:
    """Run a Supervisor lifecycle action."""
    key = action.lower().strip()
    endpoint_tmpl, timeout = _ACTION_ENDPOINTS.get(key, (None, 0))
    if endpoint_tmpl is None:
        return _error(
            f"Invalid action {action!r}.",
            code="invalid_action",
            valid_actions=sorted(_ACTION_ENDPOINTS),
        )
    result = await _supervisor_api_call(
        hass,
        endpoint_tmpl.format(slug=slug),
        method="POST",
        timeout=timeout,
    )
    if not result.get("success"):
        return result
    return {
        "success": True,
        "action": key,
        "slug": slug,
        "message": f"ESPHome add-on {slug} {key} completed.",
    }


async def _execute_config_update(
    hass: HomeAssistant,
    slug: str,
    addon: dict[str, Any],
    config_data: dict[str, Any],
) -> dict[str, Any]:
    """Update Supervisor add-on configuration."""
    ignored_fields: list[str] = []
    if "options" in config_data:
        current_options = addon.get("options") if isinstance(addon.get("options"), dict) else {}
        merged_options = _merge_options(current_options, config_data["options"])

        schema = addon.get("schema")
        if isinstance(schema, list):
            allowed_keys = {
                item["name"] for item in schema if isinstance(item, dict) and "name" in item
            }
            ignored_fields = [key for key in config_data["options"] if key not in allowed_keys]
            for key in ignored_fields:
                merged_options.pop(key, None)

        config_data = {**config_data, "options": merged_options}

    result = await _supervisor_api_call(
        hass,
        f"/addons/{slug}/options",
        method="POST",
        data=config_data,
    )
    if not result.get("success"):
        return result

    response: dict[str, Any] = {
        "success": True,
        "slug": slug,
        "submitted_fields": list(config_data),
        "message": f"Configuration updated for ESPHome add-on {slug}.",
    }
    if {"options", "network"} & config_data.keys():
        response["status"] = "pending_restart"
        response["message"] = (
            f"Configuration submitted for ESPHome add-on {slug}. "
            "Restart the add-on for options/network changes to take effect."
        )
    if ignored_fields:
        response["ignored_fields"] = ignored_fields
        response.setdefault("warnings", []).append(
            f"{len(ignored_fields)} field(s) not in the add-on schema were ignored."
        )
    return response


def _normalize_path(path: str) -> str | None:
    """Normalize an add-on API path and reject traversal."""
    normalized = unquote(path).lstrip("/")
    if ".." in normalized.split("/"):
        return None
    return normalized


def _route_for_addon(
    addon: dict[str, Any],
    normalized_path: str,
    *,
    port: int | None,
    websocket: bool,
) -> tuple[str, dict[str, str]] | dict[str, Any]:
    """Build the direct add-on URL and ingress headers."""
    addon_ip = addon.get("ip_address")
    if not addon_ip:
        return _error("ESPHome add-on is missing ip_address.", code="bad_addon_info")

    scheme = "ws" if websocket else "http"
    headers: dict[str, str] = {}
    if port is not None:
        return f"{scheme}://{addon_ip}:{port}/{normalized_path}", headers

    if not addon.get("ingress"):
        return _error("ESPHome add-on does not support ingress.", code="no_ingress")
    ingress_port = addon.get("ingress_port")
    ingress_entry = addon.get("ingress_entry")
    if not ingress_port or not ingress_entry:
        return _error(
            "ESPHome add-on is missing ingress route details.",
            code="bad_addon_info",
        )

    headers["X-Ingress-Path"] = str(ingress_entry)
    headers["X-Hass-Source"] = "core.ingress"
    return f"{scheme}://{addon_ip}:{ingress_port}/{normalized_path}", headers


def _parse_http_body(content_type: str, body: bytes) -> Any:
    """Parse an add-on HTTP response."""
    text = body.decode("utf-8", errors="replace")
    if "application/json" not in content_type:
        return text
    try:
        return json.loads(text)
    except ValueError:
        return text


def _truncate_response(data: Any) -> tuple[Any, bool]:
    """Keep MCP responses bounded."""
    serialized = data if isinstance(data, str) else json.dumps(data, default=str)
    if len(serialized) <= _MAX_RESPONSE_SIZE:
        return data, False
    if isinstance(data, str):
        return data[:_MAX_RESPONSE_SIZE], True
    return {
        "error": "RESPONSE_TOO_LARGE",
        "message": f"Response exceeds {_MAX_RESPONSE_SIZE // 1024}KB.",
    }, True


async def _call_addon_http(
    addon: dict[str, Any],
    *,
    slug: str,
    path: str,
    method: str,
    body: dict[str, Any] | list[Any] | str | None,
    port: int | None,
    timeout: int,
    debug: bool,
    request_headers: dict[str, str] | None,
) -> dict[str, Any]:
    """Call an ESPHome add-on HTTP endpoint."""
    normalized = _normalize_path(path)
    if normalized is None:
        return _error("Path contains '..' traversal component.", code="invalid_path")

    route = _route_for_addon(addon, normalized, port=port, websocket=False)
    if isinstance(route, dict):
        return route
    url, headers = route
    if request_headers:
        merged = dict(request_headers)
        merged.update(headers)
        headers = merged

    kwargs: dict[str, Any] = {}
    if isinstance(body, (dict, list)):
        kwargs["json"] = body
    elif isinstance(body, str):
        headers.setdefault("Content-Type", "application/json")
        kwargs["data"] = body

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.request(method.upper(), url, headers=headers, **kwargs) as response:
                raw = await response.read()
                response_data = _parse_http_body(
                    response.headers.get("Content-Type", ""),
                    raw,
                )
                response_data, truncated = _truncate_response(response_data)
                result: dict[str, Any] = {
                    "success": response.status < 400,
                    "status_code": response.status,
                    "response": response_data,
                    "content_type": response.headers.get("Content-Type", ""),
                    "addon_name": addon.get("name"),
                    "slug": slug,
                }
                if truncated:
                    result["truncated"] = True
                if debug:
                    result["_debug"] = {
                        "url": url,
                        "request_headers": headers,
                        "response_headers": dict(response.headers),
                    }
                if response.status >= 400:
                    result["error"] = f"ESPHome add-on API returned HTTP {response.status}"
                return result
    except TimeoutError:
        return _error(
            f"Timed out calling ESPHome add-on endpoint {path!r}.",
            code="timeout",
            slug=slug,
        )
    except aiohttp.ClientError as err:
        return _error(
            f"Failed to connect to ESPHome add-on endpoint {path!r}.",
            code="connection_failed",
            details=str(err),
            slug=slug,
        )


async def _collect_ws(
    ws: aiohttp.ClientWebSocketResponse,
    *,
    limit: int,
    timeout: int,
    wait_for_close: bool,
) -> tuple[list[Any], str]:
    """Collect WebSocket messages from the ESPHome add-on."""
    messages: list[Any] = []
    deadline = asyncio.get_running_loop().time() + timeout
    while len(messages) < limit:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return messages, "timeout"
        recv_timeout = remaining if wait_for_close else min(remaining, 2.0)
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=recv_timeout)
        except TimeoutError:
            return messages, "silence" if not wait_for_close else "timeout"
        if msg.type == aiohttp.WSMsgType.TEXT:
            try:
                messages.append(json.loads(msg.data))
            except ValueError:
                messages.append(msg.data)
            continue
        if msg.type == aiohttp.WSMsgType.BINARY:
            continue
        if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
            return messages, "server_closed"
        if msg.type == aiohttp.WSMsgType.ERROR:
            return messages, "error"
    return messages, "message_limit"


async def _call_addon_ws(
    addon: dict[str, Any],
    *,
    slug: str,
    path: str,
    body: dict[str, Any] | list[Any] | str | None,
    port: int | None,
    timeout: int,
    debug: bool,
    wait_for_close: bool,
    message_limit: int | None,
    message_offset: int,
) -> dict[str, Any]:
    """Call an ESPHome add-on WebSocket endpoint."""
    normalized = _normalize_path(path)
    if normalized is None:
        return _error("Path contains '..' traversal component.", code="invalid_path")

    route = _route_for_addon(addon, normalized, port=port, websocket=True)
    if isinstance(route, dict):
        return route
    url, headers = route
    requested_limit = (message_limit or _MAX_WS_MESSAGES) + message_offset
    limit = min(_MAX_WS_MESSAGES, max(0, requested_limit))

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout + 10)
        ) as session:
            async with session.ws_connect(
                url,
                headers=headers,
                autoclose=False,
                autoping=True,
                max_msg_size=5 * 1024 * 1024,
            ) as ws:
                if body is not None:
                    outbound = body if isinstance(body, str) else json.dumps(body)
                    await ws.send_str(outbound)
                messages, closed_by = await _collect_ws(
                    ws,
                    limit=limit,
                    timeout=timeout,
                    wait_for_close=wait_for_close,
                )
    except TimeoutError:
        return _error(
            f"Timed out connecting to ESPHome add-on WebSocket {path!r}.",
            code="timeout",
            slug=slug,
        )
    except aiohttp.ClientError as err:
        return _error(
            f"Failed to connect to ESPHome add-on WebSocket {path!r}.",
            code="connection_failed",
            details=str(err),
            slug=slug,
        )

    sliced = messages[message_offset:]
    if message_limit is not None:
        sliced = sliced[:message_limit]
    result: dict[str, Any] = {
        "success": True,
        "messages": sliced,
        "message_count": len(sliced),
        "closed_by": closed_by,
        "addon_name": addon.get("name"),
        "slug": slug,
    }
    if debug:
        result["_debug"] = {"url": url, "request_headers": headers, "body": body}
    return result


def _text_matches(item: Any, query: str | None) -> bool:
    """Return True when *query* is absent or present in a JSON-ish payload."""
    if not query:
        return True
    haystack = json.dumps(item, default=str).lower()
    return query.lower() in haystack


def _bounded_limit(limit: int, *, default: int = 100, maximum: int = 500) -> int:
    """Clamp caller limits to a bounded positive range."""
    if limit <= 0:
        return default
    return min(limit, maximum)


async def _call_device_builder_ws_command(
    addon: dict[str, Any],
    *,
    slug: str,
    command: str,
    args: dict[str, Any],
    timeout: int,
    debug: bool,
    stream: bool = False,
    message_limit: int | None = None,
) -> dict[str, Any]:
    """Call ESPHome Device Builder's current multiplexed ``/ws`` API."""
    route = _route_for_addon(addon, "ws", port=None, websocket=True)
    if isinstance(route, dict):
        return route
    url, headers = route
    message_id = "esp-mcp-1"
    payload = {"command": command, "message_id": message_id, "args": args}
    collected_limit = min(_MAX_WS_MESSAGES, message_limit or _MAX_WS_MESSAGES)
    server_info: dict[str, Any] | None = None
    events: list[dict[str, Any]] = []
    messages: list[Any] = []
    terminal_event: dict[str, Any] | None = None
    result: Any = None
    command_message_count = 0
    stop_stream_sent = False
    close_reason = "timeout"

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout + 10)
        ) as session:
            async with session.ws_connect(
                url,
                headers=headers,
                autoclose=False,
                autoping=True,
                max_msg_size=5 * 1024 * 1024,
            ) as ws:
                try:
                    msg = await asyncio.wait_for(
                        ws.receive(),
                        timeout=min(timeout, 10),
                    )
                except TimeoutError:
                    return _error(
                        "Timed out waiting for ESPHome Device Builder server info.",
                        code="timeout",
                        slug=slug,
                    )
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except ValueError:
                        data = msg.data
                    messages.append(data)
                    if isinstance(data, dict) and "server_version" in data:
                        server_info = data
                        if data.get("requires_auth"):
                            return _error(
                                "ESPHome Device Builder requires its own auth; "
                                "the ingress route did not arrive as trusted.",
                                code="device_builder_auth_required",
                                server_info=server_info,
                            )
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                    return _error(
                        "ESPHome Device Builder closed before server info.",
                        code="connection_closed",
                        slug=slug,
                    )
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    return _error(
                        "ESPHome Device Builder WebSocket failed before server info.",
                        code="connection_failed",
                        slug=slug,
                    )

                await ws.send_str(json.dumps(payload))
                deadline = asyncio.get_running_loop().time() + timeout
                while command_message_count < collected_limit:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    recv_timeout = remaining if stream else min(remaining, 10.0)
                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=recv_timeout)
                    except TimeoutError:
                        close_reason = "silence"
                        break

                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            data = json.loads(msg.data)
                        except ValueError:
                            data = msg.data
                        messages.append(data)
                        if not isinstance(data, dict):
                            continue
                        if data.get("message_id") != message_id:
                            continue
                        command_message_count += 1
                        if "error_code" in data:
                            return _error(
                                data.get("details") or f"{command} failed.",
                                code=str(data.get("error_code") or "command_failed"),
                                command=command,
                                server_info=server_info,
                            )
                        if "event" in data:
                            events.append(data)
                            if data.get("event") in _DEVICE_BUILDER_RESULT_EVENTS:
                                terminal_event = data
                                close_reason = "event_result"
                                break
                            continue
                        if "result" in data:
                            result = data.get("result")
                            close_reason = "result"
                            if not stream:
                                break
                            continue
                    elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                        close_reason = "server_closed"
                        break
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        close_reason = "error"
                        break

                if command_message_count >= collected_limit and close_reason not in {
                    "event_result",
                    "result",
                }:
                    close_reason = "message_limit"
                if stream and command in _DEVICE_BUILDER_STOPPABLE_STREAMS:
                    stop_payload = {
                        "command": "devices/stop_stream",
                        "message_id": "esp-mcp-stop",
                        "args": {"stream_id": message_id},
                    }
                    await ws.send_str(json.dumps(stop_payload))
                    stop_stream_sent = True
                await ws.close()
    except TimeoutError:
        return _error(
            f"Timed out calling ESPHome Device Builder command {command!r}.",
            code="timeout",
            slug=slug,
        )
    except aiohttp.ClientError as err:
        return _error(
            f"Failed to connect to ESPHome Device Builder command {command!r}.",
            code="connection_failed",
            details=str(err),
            slug=slug,
        )

    response: dict[str, Any] = {
        "success": True,
        "command": command,
        "result": result,
        "events": events,
        "terminal_event": terminal_event,
        "closed_by": close_reason,
        "slug": slug,
    }
    if debug:
        response["_debug"] = {
            "url": url,
            "request_headers": headers,
            "server_info": server_info,
            "message_count": len(messages),
            "command_message_count": command_message_count,
            "stop_stream_sent": stop_stream_sent,
            "messages": messages,
        }
    return response


async def _device_builder_command(
    hass: HomeAssistant,
    *,
    slug: str | None,
    command: str,
    args: dict[str, Any],
    timeout: int,
    debug: bool,
    stream: bool = False,
    message_limit: int | None = None,
) -> dict[str, Any]:
    """Resolve the ESPHome add-on and call one Device Builder WS command."""
    resolved = await _resolve_esphome_addon(hass, slug)
    if not resolved.get("success"):
        return resolved
    addon = resolved["addon"]
    resolved_slug = str(addon.get("slug") or slug or "")
    if addon.get("state") != "started":
        return _error(
            f"ESPHome add-on is not running (state: {addon.get('state')}).",
            code="addon_not_running",
            slug=resolved_slug,
        )
    return await _call_device_builder_ws_command(
        addon,
        slug=resolved_slug,
        command=command,
        args=args,
        timeout=timeout,
        debug=debug,
        stream=stream,
        message_limit=message_limit,
    )


async def list_device_builder_devices(
    hass: HomeAssistant,
    *,
    slug: str | None,
    query: str | None,
    state: str | None,
    include_importable: bool,
    limit: int,
    timeout: int,
    debug: bool,
) -> dict[str, Any]:
    """List ESPHome Device Builder devices with simple MCP-side filtering."""
    response = await _device_builder_command(
        hass,
        slug=slug,
        command="devices/list",
        args={},
        timeout=timeout,
        debug=debug,
    )
    if not response.get("success"):
        return response
    result = response.get("result") or {}
    if not isinstance(result, dict):
        return _error("Device Builder returned malformed devices/list result.")

    state_filter = state.lower() if state else None
    configured = [
        device
        for device in result.get("configured", [])
        if isinstance(device, dict)
        and _text_matches(device, query)
        and (state_filter is None or str(device.get("state")).lower() == state_filter)
    ]
    configured = configured[: _bounded_limit(limit)]
    payload: dict[str, Any] = {
        "success": True,
        "configured_count": len(configured),
        "configured": configured,
    }
    if include_importable:
        importable = [
            device
            for device in result.get("importable", [])
            if isinstance(device, dict) and _text_matches(device, query)
        ]
        payload["importable_count"] = len(importable)
        payload["importable"] = importable[: _bounded_limit(limit)]
    if debug and "_debug" in response:
        payload["_debug"] = response["_debug"]
    return payload


async def search_device_builder_yaml(
    hass: HomeAssistant,
    *,
    slug: str | None,
    query: str,
    max_results: int,
    case_sensitive: bool,
    context_lines: int | None,
    timeout: int,
    debug: bool,
) -> dict[str, Any]:
    """Search raw YAML across ESPHome Device Builder configs."""
    response = await _device_builder_command(
        hass,
        slug=slug,
        command="yaml/search",
        args={
            "query": query,
            "max_results": max(1, min(max_results, 200)),
            "case_sensitive": case_sensitive,
            "context_lines": context_lines,
        },
        timeout=timeout,
        debug=debug,
    )
    if not response.get("success"):
        return response
    matches = response.get("result") or []
    if not isinstance(matches, list):
        return _error("Device Builder returned malformed yaml/search result.")
    return {"success": True, "count": len(matches), "matches": matches}


async def read_device_builder_config(
    hass: HomeAssistant,
    *,
    slug: str | None,
    configuration: str,
    timeout: int,
    debug: bool,
) -> dict[str, Any]:
    """Read one ESPHome YAML file from Device Builder."""
    response = await _device_builder_command(
        hass,
        slug=slug,
        command="devices/get_config",
        args={"configuration": configuration},
        timeout=timeout,
        debug=debug,
    )
    if not response.get("success"):
        return response
    return {
        "success": True,
        "configuration": configuration,
        "content": response.get("result"),
    }


async def write_device_builder_config(
    hass: HomeAssistant,
    *,
    slug: str | None,
    configuration: str,
    content: str,
    allow_wipe: bool,
    timeout: int,
    debug: bool,
) -> dict[str, Any]:
    """Write one ESPHome YAML file through Device Builder validation guards."""
    response = await _device_builder_command(
        hass,
        slug=slug,
        command="devices/update_config",
        args={
            "configuration": configuration,
            "content": content,
            "allow_wipe": allow_wipe,
        },
        timeout=timeout,
        debug=debug,
    )
    if not response.get("success"):
        return response
    return {"success": True, "configuration": configuration, "message": "Config updated."}


async def run_device_builder_stream(
    hass: HomeAssistant,
    *,
    slug: str | None,
    command: str,
    args: dict[str, Any],
    timeout: int,
    debug: bool,
    message_limit: int,
) -> dict[str, Any]:
    """Run a Device Builder streaming command and return collected events."""
    response = await _device_builder_command(
        hass,
        slug=slug,
        command=command,
        args=args,
        timeout=timeout,
        debug=debug,
        stream=True,
        message_limit=message_limit,
    )
    if not response.get("success"):
        return response
    events = response.get("events") or []
    output = [
        event.get("data")
        for event in events
        if isinstance(event, dict) and event.get("event") == "output"
    ]
    terminal = response.get("terminal_event")
    return {
        "success": True,
        "command": command,
        "output": output,
        "output_line_count": len(output),
        "terminal_event": terminal,
        "closed_by": response.get("closed_by"),
    }


async def queue_device_builder_firmware_job(
    hass: HomeAssistant,
    *,
    slug: str | None,
    command: str,
    args: dict[str, Any],
    timeout: int,
    debug: bool,
) -> dict[str, Any]:
    """Queue an ESPHome Device Builder firmware job."""
    response = await _device_builder_command(
        hass,
        slug=slug,
        command=command,
        args=args,
        timeout=timeout,
        debug=debug,
    )
    if not response.get("success"):
        return response
    return {"success": True, "job": response.get("result")}


async def list_device_builder_firmware_jobs(
    hass: HomeAssistant,
    *,
    slug: str | None,
    status: str | None,
    configuration: str | None,
    limit: int,
    timeout: int,
    debug: bool,
) -> dict[str, Any]:
    """List Device Builder firmware jobs."""
    args = {
        key: value
        for key, value in {
            "status": status,
            "configuration": configuration,
        }.items()
        if value is not None
    }
    response = await _device_builder_command(
        hass,
        slug=slug,
        command="firmware/get_jobs",
        args=args,
        timeout=timeout,
        debug=debug,
    )
    if not response.get("success"):
        return response
    jobs = response.get("result") or []
    if not isinstance(jobs, list):
        return _error("Device Builder returned malformed firmware/get_jobs result.")
    bounded = jobs[: _bounded_limit(limit)]
    return {"success": True, "count": len(bounded), "jobs": bounded}


async def get_device_builder_firmware_job(
    hass: HomeAssistant,
    *,
    slug: str | None,
    job_id: str,
    timeout: int,
    debug: bool,
) -> dict[str, Any]:
    """Return one Device Builder firmware job."""
    response = await _device_builder_command(
        hass,
        slug=slug,
        command="firmware/get_job",
        args={"job_id": job_id},
        timeout=timeout,
        debug=debug,
    )
    if not response.get("success"):
        return response
    job = response.get("result")
    return {"success": True, "found": job is not None, "job": job}


async def follow_device_builder_firmware_job(
    hass: HomeAssistant,
    *,
    slug: str | None,
    job_id: str,
    message_limit: int,
    timeout: int,
    debug: bool,
) -> dict[str, Any]:
    """Follow a Device Builder firmware job stream."""
    response = await _device_builder_command(
        hass,
        slug=slug,
        command="firmware/follow_job",
        args={"job_id": job_id},
        timeout=timeout,
        debug=debug,
        stream=True,
        message_limit=message_limit,
    )
    if not response.get("success"):
        return response
    events = response.get("events") or []
    output = [
        event.get("data")
        for event in events
        if isinstance(event, dict) and event.get("event") == "output"
    ]
    terminal = response.get("terminal_event")
    job = terminal.get("data") if isinstance(terminal, dict) else None
    if not output and isinstance(job, dict) and isinstance(job.get("output"), list):
        output = [str(line) for line in job["output"]]
    return {
        "success": True,
        "job_id": job_id,
        "output": output,
        "output_line_count": len(output),
        "job": job,
        "exit_code": job.get("exit_code") if isinstance(job, dict) else None,
        "terminal_event": terminal,
        "closed_by": response.get("closed_by"),
    }


async def manage_esphome_addon(
    hass: HomeAssistant,
    *,
    slug: str | None,
    action: str | None,
    path: str | None,
    method: str,
    body: dict[str, Any] | list[Any] | str | None,
    websocket: bool,
    wait_for_close: bool,
    message_limit: int | None,
    message_offset: int,
    options: dict[str, Any] | None,
    network: dict[str, Any] | None,
    boot: str | None,
    auto_update: bool | None,
    watchdog: bool | None,
    port: int | None,
    timeout: int,
    debug: bool,
    request_headers: dict[str, str] | None,
) -> dict[str, Any]:
    """Manage the ESPHome add-on or call its dashboard API."""
    config_data = _build_config_payload(
        options=options,
        network=network,
        boot=boot,
        auto_update=auto_update,
        watchdog=watchdog,
    )
    action_key = action.lower().strip() if action is not None else None

    if action_key == "install" and slug:
        if path is not None or config_data:
            return _error(
                "action cannot be combined with path or config parameters.",
                code="invalid_mode",
            )
        if "esphome" not in slug.lower():
            return _error(
                f"Add-on slug {slug!r} does not look like the ESPHome add-on.",
                code="not_esphome_addon",
                slug=slug,
            )
        return await _execute_action(hass, slug, action_key)

    resolved = await _resolve_esphome_addon(hass, slug)
    if not resolved.get("success"):
        return resolved

    addon = resolved["addon"]
    resolved_slug = str(addon.get("slug") or slug or "")

    if action_key is not None:
        if path is not None or config_data:
            return _error(
                "action cannot be combined with path or config parameters.",
                code="invalid_mode",
            )
        return await _execute_action(hass, resolved_slug, action_key)

    if config_data:
        if path is not None:
            return _error(
                "path cannot be combined with config parameters.",
                code="invalid_mode",
            )
        return await _execute_config_update(hass, resolved_slug, addon, config_data)

    effective_path = path or "/devices"
    if addon.get("state") != "started":
        return _error(
            f"ESPHome add-on is not running (state: {addon.get('state')}).",
            code="addon_not_running",
            slug=resolved_slug,
        )
    if method.upper() not in _VALID_HTTP_METHODS:
        return _error(
            f"Invalid HTTP method {method!r}.",
            code="invalid_method",
            valid_methods=sorted(_VALID_HTTP_METHODS),
        )

    if websocket:
        return await _call_addon_ws(
            addon,
            slug=resolved_slug,
            path=effective_path,
            body=body,
            port=port,
            timeout=timeout,
            debug=debug,
            wait_for_close=wait_for_close,
            message_limit=message_limit,
            message_offset=message_offset,
        )

    if message_limit is not None or message_offset:
        return _error(
            "message_limit and message_offset apply only to websocket mode.",
            code="invalid_mode",
        )
    return await _call_addon_http(
        addon,
        slug=resolved_slug,
        path=effective_path,
        method=method,
        body=body,
        port=port,
        timeout=timeout,
        debug=debug,
        request_headers=request_headers,
    )
