"""Native Device Builder protocol and compatibility behavior."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from .test_addon_tools import _esphome_addon, _hass, _run, addon_tools


def _api(name: str) -> Any:
    function = getattr(addon_tools, name, None)
    assert callable(function), f"Native Device Builder support missing: {name}"
    return function


class Response:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self.status = status
        self.headers = {"Content-Type": "application/json"}
        self.raw = json.dumps(payload).encode()
        self.content = self

    async def read(self, size: int = -1) -> bytes:
        return self.raw if size < 0 else self.raw[:size]

    async def __aenter__(self) -> Response:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        pass


def _native_server(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    class Session:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            pass

        def request(self, method: str, url: str, **kwargs: Any) -> Response:
            assert method == "POST"
            assert url == "http://127.0.0.1:8123/api/hassio_ingress/esphome/api/mcp"
            assert kwargs["headers"]["Cookie"] == "ingress_session=private-cookie"
            assert "Authorization" not in kwargs["headers"]
            body = kwargs["json"]
            calls.append(body)
            rpc_method = body["method"]
            if rpc_method == "notifications/initialized":
                return Response(None, 202)
            if rpc_method != "initialize":
                assert kwargs["headers"]["MCP-Protocol-Version"] == "2025-11-25"
            result = {
                "initialize": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "ESPHome Device Builder", "version": "1.25.0"},
                },
                "tools/list": {
                    "tools": [
                        {
                            "name": "get_config",
                            "description": "Read device YAML",
                            "inputSchema": {"type": "object", "required": ["configuration"]},
                        },
                        {
                            "name": "update_config",
                            "inputSchema": {"type": "object", "required": ["expected"]},
                        },
                    ]
                },
                "tools/call": {"content": [{"type": "text", "text": "host:\\n"}], "isError": False},
            }[rpc_method]
            replacement = overrides.get(rpc_method, result)
            if isinstance(replacement, Exception):
                raise replacement
            if isinstance(replacement, Response):
                return replacement
            return Response({"jsonrpc": "2.0", "id": body["id"], "result": replacement})

    monkeypatch.setattr(addon_tools.aiohttp, "ClientSession", Session)
    monkeypatch.setattr(
        addon_tools, "_resolve_esphome_addon",
        AsyncMock(return_value={"success": True, "addon": _esphome_addon()}),
    )
    monkeypatch.setattr(
        addon_tools, "_create_ingress_session",
        AsyncMock(return_value={"success": True, "session": "private-cookie"}),
    )
    return calls


def test_native_discovery_returns_installed_schemas(monkeypatch: pytest.MonkeyPatch) -> None:
    discover = _api("native_device_builder_tools")
    calls = _native_server(monkeypatch)
    result = _run(discover(_hass(), tool="update_config"))
    assert result["success"] is True
    assert result["server_info"]["version"] == "1.25.0"
    assert result["tools"] == [
        {"name": "update_config", "inputSchema": {"type": "object", "required": ["expected"]}}
    ]
    assert [call["method"] for call in calls] == [
        "initialize", "notifications/initialized", "tools/list"
    ]


def test_native_call_preserves_tool_error_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    call = _api("call_native_device_builder_tool")
    native_result = {
        "content": [{"type": "text", "text": "precondition_failed: config changed"}],
        "isError": True,
    }
    calls = _native_server(monkeypatch, **{"tools/call": native_result})
    arguments = {"configuration": "fixture.yaml", "content": "new", "expected": "old"}
    result = _run(call(_hass(), name="update_config", arguments=arguments))
    assert result["success"] is False
    assert result["result"] == native_result
    assert [row["params"] for row in calls if row["method"] == "tools/call"] == [
        {"name": "update_config", "arguments": arguments}
    ]


@pytest.mark.parametrize("status", [401, 403, 500])
def test_native_http_errors_are_not_capability_absence(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    discover = _api("native_device_builder_tools")
    _native_server(monkeypatch, initialize=Response({}, status))
    result = _run(discover(_hass()))
    assert result["success"] is False
    assert result["error_code"] != "native_mcp_unavailable"


@pytest.mark.parametrize("status", [404, 405])
def test_native_missing_endpoint_is_explicit(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    discover = _api("native_device_builder_tools")
    _native_server(monkeypatch, initialize=Response({}, status))
    result = _run(discover(_hass()))
    assert result["success"] is False
    assert result["error_code"] == "native_mcp_unavailable"


def test_native_unknown_tool_is_not_called(monkeypatch: pytest.MonkeyPatch) -> None:
    call = _api("call_native_device_builder_tool")
    calls = _native_server(monkeypatch)
    result = _run(call(_hass(), name="future_tool"))
    assert result["error_code"] == "native_tool_unavailable"
    assert not any(row["method"] == "tools/call" for row in calls)


def test_native_timeout_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    call = _api("call_native_device_builder_tool")
    calls = _native_server(monkeypatch, **{"tools/call": TimeoutError()})
    result = _run(call(_hass(), name="update_config", arguments={"expected": "old"}))
    assert result["error_code"] == "timeout"
    assert len([row for row in calls if row["method"] == "tools/call"]) == 1


def test_yaml_read_uses_native_content(monkeypatch: pytest.MonkeyPatch) -> None:
    _api("call_native_device_builder_tool")
    _native_server(monkeypatch)
    legacy = AsyncMock(side_effect=AssertionError("native reads must not use WS"))
    monkeypatch.setattr(addon_tools, "_device_builder_command", legacy)
    result = _run(addon_tools.read_device_builder_config(
        _hass(), slug=None, configuration="fixture.yaml", timeout=60, debug=False
    ))
    assert result == {"success": True, "configuration": "fixture.yaml", "content": "host:\\n"}


@pytest.mark.parametrize("code, fallback", [
    ("native_mcp_unavailable", True), ("native_tool_unavailable", True),
    ("timeout", False), ("native_tool_error", False), ("native_protocol_error", False),
])
def test_yaml_read_fallback_only_for_missing_capability(
    monkeypatch: pytest.MonkeyPatch, code: str, fallback: bool
) -> None:
    _api("call_native_device_builder_tool")
    monkeypatch.setattr(addon_tools, "call_native_device_builder_tool", AsyncMock(
        return_value={"success": False, "error_code": code}
    ))
    legacy = AsyncMock(return_value={"success": True, "result": "legacy YAML"})
    monkeypatch.setattr(addon_tools, "_device_builder_command", legacy)
    result = _run(addon_tools.read_device_builder_config(
        _hass(), slug=None, configuration="fixture.yaml", timeout=60, debug=False
    ))
    assert result["success"] is fallback
    assert legacy.call_count == int(fallback)


def test_native_cancellation_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    discover = _api("native_device_builder_tools")
    monkeypatch.setattr(addon_tools, "_resolve_esphome_addon", AsyncMock(
        side_effect=asyncio.CancelledError
    ))
    with pytest.raises(asyncio.CancelledError):
        _run(discover(_hass()))
