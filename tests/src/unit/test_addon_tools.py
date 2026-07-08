"""Behavior tests for ESPHome add-on and Device Builder tools."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

try:
    import aiohttp  # noqa: F401
except ModuleNotFoundError:
    aiohttp_stub = ModuleType("aiohttp")

    class _WSMsgType(Enum):
        TEXT = "TEXT"
        BINARY = "BINARY"
        CLOSE = "CLOSE"
        CLOSED = "CLOSED"
        ERROR = "ERROR"

    class _ClientError(Exception):
        pass

    class _ClientTimeout:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    aiohttp_stub.WSMsgType = _WSMsgType
    aiohttp_stub.ClientError = _ClientError
    aiohttp_stub.ClientTimeout = _ClientTimeout
    aiohttp_stub.ClientSession = object
    sys.modules["aiohttp"] = aiohttp_stub

ROOT = Path(__file__).resolve().parents[3]
ADDON_TOOLS_PATH = ROOT / "custom_components" / "esphome_mcp" / "addon_tools.py"
spec = importlib.util.spec_from_file_location("esphome_mcp_addon_tools", ADDON_TOOLS_PATH)
assert spec is not None and spec.loader is not None
addon_tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(addon_tools)


def _run(coro: Any) -> Any:
    """Run an async helper without requiring pytest-asyncio."""
    return asyncio.run(coro)


def _esphome_addon(**overrides: Any) -> dict[str, Any]:
    addon: dict[str, Any] = {
        "slug": "5c53de3b_esphome",
        "name": "ESPHome Device Builder",
        "description": "ESPHome Device Builder",
        "repository": "https://github.com/esphome/home-assistant-addon",
        "state": "started",
        "ip_address": "172.30.33.2",
        "ingress": True,
        "ingress_port": 6052,
        "ingress_entry": "/api/hassio_ingress/esphome",
    }
    addon.update(overrides)
    return addon


def _text_frame(payload: dict[str, Any] | str) -> SimpleNamespace:
    if isinstance(payload, str):
        data = payload
    else:
        data = json.dumps(payload)
    return SimpleNamespace(type=addon_tools.aiohttp.WSMsgType.TEXT, data=data)


class _FakeWebSocket:
    def __init__(self, frames: list[SimpleNamespace]) -> None:
        self.frames = frames
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def receive(self) -> SimpleNamespace:
        if not self.frames:
            await asyncio.sleep(60)
        return self.frames.pop(0)

    async def send_str(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def close(self) -> None:
        self.closed = True


class _FakeWebSocketContext:
    def __init__(self, ws: _FakeWebSocket) -> None:
        self.ws = ws

    async def __aenter__(self) -> _FakeWebSocket:
        return self.ws

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _FakeClientSession:
    ws: _FakeWebSocket
    url: str
    headers: dict[str, str]

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def __aenter__(self) -> _FakeClientSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def ws_connect(self, url: str, **kwargs: Any) -> _FakeWebSocketContext:
        type(self).url = url
        type(self).headers = dict(kwargs.get("headers") or {})
        return _FakeWebSocketContext(type(self).ws)


def _install_fake_ws(
    monkeypatch: pytest.MonkeyPatch,
    frames: list[SimpleNamespace],
) -> _FakeWebSocket:
    ws = _FakeWebSocket(frames)
    _FakeClientSession.ws = ws
    monkeypatch.setattr(addon_tools.aiohttp, "ClientSession", _FakeClientSession)
    return ws


def test_ingress_route_uses_home_assistant_core_headers() -> None:
    """Ingress routing mirrors HA add-on ingress headers."""
    route = addon_tools._route_for_addon(
        _esphome_addon(),
        "ws",
        port=None,
        websocket=True,
    )

    assert route == (
        "ws://172.30.33.2:6052/ws",
        {
            "X-Ingress-Path": "/api/hassio_ingress/esphome",
            "X-Hass-Source": "core.ingress",
        },
    )


def test_explicit_install_uses_store_action_without_resolving_installed_addon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Install can target a store slug before the add-on is installed."""
    calls: list[tuple[object, str, str]] = []

    async def execute_action(hass: object, slug: str, action: str) -> dict[str, Any]:
        calls.append((hass, slug, action))
        return {"success": True, "slug": slug, "action": action}

    async def resolve(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("install with slug must not fetch /addons/{slug}/info")

    monkeypatch.setattr(addon_tools, "_execute_action", execute_action)
    monkeypatch.setattr(addon_tools, "_resolve_esphome_addon", resolve)
    hass = object()

    result = _run(
        addon_tools.manage_esphome_addon(
            hass,
            slug="5c53de3b_esphome",
            action="install",
            path=None,
            method="GET",
            body=None,
            websocket=False,
            wait_for_close=True,
            message_limit=None,
            message_offset=0,
            options=None,
            network=None,
            boot=None,
            auto_update=None,
            watchdog=None,
            port=None,
            timeout=60,
            debug=False,
            request_headers=None,
        )
    )

    assert result == {"success": True, "slug": "5c53de3b_esphome", "action": "install"}
    assert calls == [(hass, "5c53de3b_esphome", "install")]


def test_manage_addon_defaults_to_devices_http_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No action/config/path means read the Device Builder devices endpoint."""
    seen: dict[str, Any] = {}

    async def resolve(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"success": True, "addon": _esphome_addon()}

    async def call_addon_http(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"success": True, "response": []}

    monkeypatch.setattr(addon_tools, "_resolve_esphome_addon", resolve)
    monkeypatch.setattr(addon_tools, "_call_addon_http", call_addon_http)

    result = _run(
        addon_tools.manage_esphome_addon(
            object(),
            slug=None,
            action=None,
            path=None,
            method="GET",
            body=None,
            websocket=False,
            wait_for_close=True,
            message_limit=None,
            message_offset=0,
            options=None,
            network=None,
            boot=None,
            auto_update=None,
            watchdog=None,
            port=None,
            timeout=60,
            debug=False,
            request_headers=None,
        )
    )

    assert result == {"success": True, "response": []}
    assert seen["slug"] == "5c53de3b_esphome"
    assert seen["path"] == "/devices"
    assert seen["method"] == "GET"


def test_config_update_merges_options_and_ignores_unknown_schema_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config updates preserve existing options and avoid schema-invalid keys."""
    sent: dict[str, Any] = {}
    addon = _esphome_addon(
        options={"existing": True, "nested": {"keep": 1}},
        schema=[{"name": "existing"}, {"name": "nested"}],
    )

    async def supervisor_api_call(
        _hass: object,
        endpoint: str,
        *,
        method: str = "GET",
        data: Any | None = None,
        timeout: int | None = 30,
    ) -> dict[str, Any]:
        sent.update({"endpoint": endpoint, "method": method, "data": data, "timeout": timeout})
        return {"success": True, "result": {}}

    monkeypatch.setattr(addon_tools, "_supervisor_api_call", supervisor_api_call)

    result = _run(
        addon_tools._execute_config_update(
            object(),
            "5c53de3b_esphome",
            addon,
            {
                "options": {"nested": {"added": 2}, "unknown": "drop me"},
                "boot": "manual",
            },
        )
    )

    assert result["success"] is True
    assert result["status"] == "pending_restart"
    assert result["ignored_fields"] == ["unknown"]
    assert sent["endpoint"] == "/addons/5c53de3b_esphome/options"
    assert sent["method"] == "POST"
    assert sent["data"] == {
        "options": {"existing": True, "nested": {"keep": 1, "added": 2}},
        "boot": "manual",
    }


def test_device_builder_command_refuses_stopped_addon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Current Device Builder commands require the ESPHome add-on to be running."""

    async def resolve(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"success": True, "addon": _esphome_addon(state="stopped")}

    async def call_ws(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("stopped add-on must not be called")

    monkeypatch.setattr(addon_tools, "_resolve_esphome_addon", resolve)
    monkeypatch.setattr(addon_tools, "_call_device_builder_ws_command", call_ws)

    result = _run(
        addon_tools._device_builder_command(
            object(),
            slug=None,
            command="devices/list",
            args={},
            timeout=60,
            debug=False,
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "addon_not_running"


def test_device_builder_ws_reads_server_info_then_sends_current_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Device Builder calls use the current command/message_id/args WS envelope."""
    ws = _install_fake_ws(
        monkeypatch,
        [
            _text_frame({"server_version": "2026.6.0", "requires_auth": False}),
            _text_frame(
                {
                    "message_id": "esp-mcp-1",
                    "result": {"configured": [{"name": "kitchen"}]},
                }
            ),
        ],
    )

    result = _run(
        addon_tools._call_device_builder_ws_command(
            _esphome_addon(),
            slug="5c53de3b_esphome",
            command="devices/list",
            args={},
            timeout=30,
            debug=True,
        )
    )

    assert result["success"] is True
    assert result["result"] == {"configured": [{"name": "kitchen"}]}
    assert result["_debug"]["server_info"] == {
        "server_version": "2026.6.0",
        "requires_auth": False,
    }
    assert _FakeClientSession.url == "ws://172.30.33.2:6052/ws"
    assert _FakeClientSession.headers["X-Hass-Source"] == "core.ingress"
    assert ws.sent == [{"command": "devices/list", "message_id": "esp-mcp-1", "args": {}}]
    assert ws.closed is True


def test_device_builder_ws_reports_untrusted_ingress_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A requires_auth server_info frame is surfaced as a routing/auth problem."""
    ws = _install_fake_ws(
        monkeypatch,
        [_text_frame({"server_version": "2026.6.0", "requires_auth": True})],
    )

    result = _run(
        addon_tools._call_device_builder_ws_command(
            _esphome_addon(),
            slug="5c53de3b_esphome",
            command="devices/list",
            args={},
            timeout=30,
            debug=False,
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "device_builder_auth_required"
    assert ws.sent == []


def test_device_builder_stoppable_stream_sends_stop_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bounded log streams are cancelled with Device Builder's stop command."""
    ws = _install_fake_ws(
        monkeypatch,
        [
            _text_frame({"server_version": "2026.6.0", "requires_auth": False}),
            _text_frame({"message_id": "esp-mcp-1", "event": "output", "data": "line one"}),
        ],
    )

    result = _run(
        addon_tools._call_device_builder_ws_command(
            _esphome_addon(),
            slug="5c53de3b_esphome",
            command="devices/logs",
            args={"configuration": "kitchen.yaml", "port": "OTA"},
            timeout=30,
            debug=True,
            stream=True,
            message_limit=1,
        )
    )

    assert result["success"] is True
    assert result["events"] == [{"message_id": "esp-mcp-1", "event": "output", "data": "line one"}]
    assert result["closed_by"] == "message_limit"
    assert result["_debug"]["stop_stream_sent"] is True
    assert ws.sent == [
        {
            "command": "devices/logs",
            "message_id": "esp-mcp-1",
            "args": {"configuration": "kitchen.yaml", "port": "OTA"},
        },
        {
            "command": "devices/stop_stream",
            "message_id": "esp-mcp-stop",
            "args": {"stream_id": "esp-mcp-1"},
        },
    ]


def test_device_builder_device_list_filters_configured_and_importable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP-side list filtering keeps query/state/limit behavior predictable."""

    async def device_builder_command(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "success": True,
            "result": {
                "configured": [
                    {"name": "kitchen", "state": "ONLINE"},
                    {"name": "garage", "state": "OFFLINE"},
                ],
                "importable": [{"name": "kitchen-ble"}, {"name": "office"}],
            },
        }

    monkeypatch.setattr(addon_tools, "_device_builder_command", device_builder_command)

    result = _run(
        addon_tools.list_device_builder_devices(
            object(),
            slug=None,
            query="kitchen",
            state="online",
            include_importable=True,
            limit=10,
            timeout=60,
            debug=False,
        )
    )

    assert result == {
        "success": True,
        "configured_count": 1,
        "configured": [{"name": "kitchen", "state": "ONLINE"}],
        "importable_count": 1,
        "importable": [{"name": "kitchen-ble"}],
    }


def test_yaml_update_wrapper_passes_current_device_builder_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Write operations use devices/update_config with allow_wipe guard."""
    seen: dict[str, Any] = {}

    async def device_builder_command(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"success": True, "result": {"ok": True}}

    monkeypatch.setattr(addon_tools, "_device_builder_command", device_builder_command)

    result = _run(
        addon_tools.write_device_builder_config(
            object(),
            slug="5c53de3b_esphome",
            configuration="kitchen.yaml",
            content="esphome:\n  name: kitchen\n",
            allow_wipe=False,
            timeout=60,
            debug=False,
        )
    )

    assert result == {
        "success": True,
        "configuration": "kitchen.yaml",
        "message": "Config updated.",
    }
    assert seen == {
        "slug": "5c53de3b_esphome",
        "command": "devices/update_config",
        "args": {
            "configuration": "kitchen.yaml",
            "content": "esphome:\n  name: kitchen\n",
            "allow_wipe": False,
        },
        "timeout": 60,
        "debug": False,
    }


def test_stream_wrapper_returns_output_and_terminal_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Streaming wrappers expose bounded output lines plus the terminal event."""

    async def device_builder_command(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["stream"] is True
        assert kwargs["message_limit"] == 2
        return {
            "success": True,
            "events": [
                {"event": "output", "data": "line one"},
                {"event": "output", "data": "line two"},
                {"event": "result", "data": {"success": True}},
            ],
            "terminal_event": {"event": "result", "data": {"success": True}},
            "closed_by": "event_result",
        }

    monkeypatch.setattr(addon_tools, "_device_builder_command", device_builder_command)

    result = _run(
        addon_tools.run_device_builder_stream(
            object(),
            slug=None,
            command="devices/validate",
            args={"configuration": "kitchen.yaml"},
            timeout=300,
            debug=False,
            message_limit=2,
        )
    )

    assert result == {
        "success": True,
        "command": "devices/validate",
        "output": ["line one", "line two"],
        "output_line_count": 2,
        "terminal_event": {"event": "result", "data": {"success": True}},
        "closed_by": "event_result",
    }


def test_firmware_jobs_wrapper_filters_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Firmware job listing sends filters to Device Builder and bounds results."""

    async def device_builder_command(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["command"] == "firmware/get_jobs"
        assert kwargs["args"] == {"status": "running", "configuration": "kitchen.yaml"}
        return {
            "success": True,
            "result": [
                {
                    "job_id": "1",
                    "status": "running",
                    "configuration": "kitchen.yaml",
                },
                {
                    "job_id": "2",
                    "status": "completed",
                    "configuration": "kitchen.yaml",
                },
                {
                    "job_id": "3",
                    "status": "running",
                    "configuration": "garage.yaml",
                },
            ],
        }

    monkeypatch.setattr(addon_tools, "_device_builder_command", device_builder_command)

    result = _run(
        addon_tools.list_device_builder_firmware_jobs(
            object(),
            slug=None,
            status="running",
            configuration="kitchen.yaml",
            limit=1,
            timeout=60,
            debug=False,
        )
    )

    assert result == {
        "success": True,
        "count": 1,
        "jobs": [
            {
                "job_id": "1",
                "status": "running",
                "configuration": "kitchen.yaml",
            }
        ],
    }


@pytest.mark.parametrize(
    ("helper", "expected_command", "expected_args"),
    [
        (
            addon_tools.search_device_builder_yaml,
            "yaml/search",
            {
                "query": "wifi",
                "max_results": 50,
                "case_sensitive": False,
                "context_lines": None,
            },
        ),
        (
            addon_tools.read_device_builder_config,
            "devices/get_config",
            {"configuration": "kitchen.yaml"},
        ),
        (
            addon_tools.queue_device_builder_firmware_job,
            "firmware/compile",
            {"configuration": "kitchen.yaml"},
        ),
        (
            addon_tools.get_device_builder_firmware_job,
            "firmware/get_job",
            {"job_id": "job-1"},
        ),
        (
            addon_tools.follow_device_builder_firmware_job,
            "firmware/follow_job",
            {"job_id": "job-1"},
        ),
    ],
)
def test_device_builder_wrappers_use_expected_commands(
    monkeypatch: pytest.MonkeyPatch,
    helper: Any,
    expected_command: str,
    expected_args: dict[str, Any],
) -> None:
    """Wrappers stay aligned with the current Device Builder command names."""
    seen: dict[str, Any] = {}

    async def device_builder_command(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        if expected_command == "yaml/search":
            return {"success": True, "result": []}
        if expected_command == "devices/get_config":
            return {"success": True, "result": "wifi:\n"}
        if expected_command == "firmware/get_job":
            return {"success": True, "result": {"job_id": "job-1"}}
        if expected_command == "firmware/follow_job":
            return {"success": True, "events": [], "closed_by": "result"}
        return {"success": True, "result": {"job_id": "job-1"}}

    monkeypatch.setattr(addon_tools, "_device_builder_command", device_builder_command)

    common = {
        "hass": object(),
        "slug": None,
        "timeout": 60,
        "debug": False,
    }
    if expected_command == "yaml/search":
        result = _run(
            helper(
                **common,
                query="wifi",
                max_results=50,
                case_sensitive=False,
                context_lines=None,
            )
        )
    elif expected_command == "devices/get_config":
        result = _run(helper(**common, configuration="kitchen.yaml"))
    elif expected_command == "firmware/compile":
        result = _run(
            helper(
                **common,
                command="firmware/compile",
                args={"configuration": "kitchen.yaml"},
            )
        )
    elif expected_command == "firmware/get_job":
        result = _run(helper(**common, job_id="job-1"))
    else:
        result = _run(helper(**common, job_id="job-1", message_limit=500))

    assert result["success"] is True
    assert seen["command"] == expected_command
    assert seen["args"] == expected_args
