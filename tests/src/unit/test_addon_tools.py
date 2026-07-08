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


def _hass(*, port: int = 8123) -> SimpleNamespace:
    """Minimal HA object for route helpers that only need hass.http.server_port."""
    return SimpleNamespace(http=SimpleNamespace(server_port=port))


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


def _manage_defaults(**overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "slug": None,
        "action": None,
        "path": None,
        "method": "GET",
        "body": None,
        "websocket": False,
        "wait_for_close": True,
        "message_limit": None,
        "message_offset": 0,
        "options": None,
        "network": None,
        "boot": None,
        "auto_update": None,
        "watchdog": None,
        "port": None,
        "timeout": 60,
        "debug": False,
        "request_headers": None,
    }
    args.update(overrides)
    return args


def _text_frame(payload: dict[str, Any] | str) -> SimpleNamespace:
    if isinstance(payload, str):
        data = payload
    else:
        data = json.dumps(payload)
    return SimpleNamespace(type=addon_tools.aiohttp.WSMsgType.TEXT, data=data)


def _ws_frame(frame_type: Any) -> SimpleNamespace:
    return SimpleNamespace(type=frame_type, data="")


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

    async def create_ingress_session(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"success": True, "session": "test-ingress-session"}

    monkeypatch.setattr(addon_tools, "_create_ingress_session", create_ingress_session)
    return ws


def test_ingress_route_uses_home_assistant_core_proxy_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Custom-component ingress routes through HA Core with a Supervisor session."""

    async def create_ingress_session(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"success": True, "session": "test-ingress-session"}

    monkeypatch.setattr(addon_tools, "_create_ingress_session", create_ingress_session)

    route = _run(
        addon_tools._route_for_addon(
            _hass(port=8124),
            _esphome_addon(),
            "ws",
            port=None,
            websocket=True,
        )
    )

    assert route == (
        "ws://127.0.0.1:8124/api/hassio_ingress/esphome/ws",
        {"Cookie": "ingress_session=test-ingress-session"},
    )


def test_direct_port_route_uses_addon_ip_without_ingress_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit direct-port mode is the only path that bypasses HA Core ingress."""

    async def create_ingress_session(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("direct port routes must not mint ingress sessions")

    monkeypatch.setattr(addon_tools, "_create_ingress_session", create_ingress_session)

    route = _run(
        addon_tools._route_for_addon(
            _hass(),
            _esphome_addon(),
            "ws",
            port=6052,
            websocket=True,
        )
    )

    assert route == ("ws://172.30.33.2:6052/ws", {})


def test_debug_headers_redact_ingress_cookie() -> None:
    """Debug payloads never leak the per-call Supervisor ingress session."""
    assert addon_tools._debug_headers(
        {"Cookie": "ingress_session=test-ingress-session", "X-Test": "ok"}
    ) == {"Cookie": "ingress_session=**REDACTED**", "X-Test": "ok"}


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


def test_http_proxy_devices_endpoint_sends_json_body_and_redacts_debug_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Device Builder /devices proxy sends JSON bodies through trusted ingress."""
    captured: dict[str, Any] = {}

    async def create_ingress_session(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"success": True, "session": "test-ingress-session"}

    class FakeResponse:
        status = 200
        headers = {"Content-Type": "application/json; charset=utf-8"}

        async def read(self) -> bytes:
            return b'{"devices": [{"name": "kitchen"}]}'

    class FakeRequestContext:
        async def __aenter__(self) -> FakeResponse:
            return FakeResponse()

        async def __aexit__(self, *_exc: object) -> None:
            return None

    class FakeHTTPClientSession:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        async def __aenter__(self) -> FakeHTTPClientSession:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        def request(self, method: str, url: str, **kwargs: Any) -> FakeRequestContext:
            captured.update({"method": method, "url": url, **kwargs})
            return FakeRequestContext()

    monkeypatch.setattr(addon_tools, "_create_ingress_session", create_ingress_session)
    monkeypatch.setattr(addon_tools.aiohttp, "ClientSession", FakeHTTPClientSession)

    result = _run(
        addon_tools._call_addon_http(
            _hass(port=8124),
            _esphome_addon(),
            slug="5c53de3b_esphome",
            path="/devices",
            method="post",
            body={"configuration": "kitchen.yaml"},
            port=None,
            timeout=30,
            debug=True,
            request_headers={"Cookie": "caller=ignored", "X-Test": "ok"},
        )
    )

    assert result["success"] is True
    assert result["response"] == {"devices": [{"name": "kitchen"}]}
    assert captured["method"] == "POST"
    assert captured["url"] == "http://127.0.0.1:8124/api/hassio_ingress/esphome/devices"
    assert captured["headers"] == {
        "Cookie": "ingress_session=test-ingress-session",
        "X-Test": "ok",
    }
    assert captured["json"] == {"configuration": "kitchen.yaml"}
    assert "data" not in captured
    assert result["_debug"]["request_headers"]["Cookie"] == "ingress_session=**REDACTED**"


def test_manage_addon_rejects_non_esphome_store_install_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Store installs are limited to ESPHome-looking add-on slugs."""

    async def execute_action(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("non-ESPHome install slug must not run an action")

    monkeypatch.setattr(addon_tools, "_execute_action", execute_action)

    result = _run(
        addon_tools.manage_esphome_addon(
            object(),
            **_manage_defaults(slug="core_mosquitto", action="install"),
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "not_esphome_addon"
    assert result["slug"] == "core_mosquitto"


def test_manage_addon_rejects_action_combined_with_path_or_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lifecycle actions must not be mixed with proxy/config modes."""

    async def resolve(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("invalid install mode should fail before add-on lookup")

    async def execute_action(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("invalid install mode must not execute")

    monkeypatch.setattr(addon_tools, "_resolve_esphome_addon", resolve)
    monkeypatch.setattr(addon_tools, "_execute_action", execute_action)

    result = _run(
        addon_tools.manage_esphome_addon(
            object(),
            **_manage_defaults(
                slug="5c53de3b_esphome",
                action="install",
                path="/devices",
                options={"relative_url": "/"},
            ),
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "invalid_mode"


def test_manage_addon_rejects_invalid_http_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP proxy mode only allows the bounded Supervisor method set."""

    async def resolve(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"success": True, "addon": _esphome_addon()}

    async def call_addon_http(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("invalid method must not reach the add-on HTTP client")

    monkeypatch.setattr(addon_tools, "_resolve_esphome_addon", resolve)
    monkeypatch.setattr(addon_tools, "_call_addon_http", call_addon_http)

    result = _run(
        addon_tools.manage_esphome_addon(
            object(),
            **_manage_defaults(method="TRACE", path="/devices"),
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "invalid_method"
    assert result["valid_methods"] == ["DELETE", "GET", "PATCH", "POST", "PUT"]


def test_manage_addon_rejects_http_message_paging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WebSocket-only paging flags are rejected before HTTP proxying."""

    async def resolve(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"success": True, "addon": _esphome_addon()}

    async def call_addon_http(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("invalid mode must not call the HTTP proxy")

    monkeypatch.setattr(addon_tools, "_resolve_esphome_addon", resolve)
    monkeypatch.setattr(addon_tools, "_call_addon_http", call_addon_http)

    result = _run(
        addon_tools.manage_esphome_addon(
            object(),
            **_manage_defaults(path="/devices", message_limit=10),
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "invalid_mode"


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


def test_http_and_ws_proxy_reject_path_traversal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decoded '..' path components never reach aiohttp clients."""

    def client_session(*_args: Any, **_kwargs: Any) -> object:
        raise AssertionError("path traversal must fail before opening a session")

    monkeypatch.setattr(addon_tools.aiohttp, "ClientSession", client_session)
    addon = _esphome_addon()

    http_result = _run(
        addon_tools._call_addon_http(
            _hass(),
            addon,
            slug="5c53de3b_esphome",
            path="/api/%2e%2e/secrets.yaml",
            method="GET",
            body=None,
            port=None,
            timeout=30,
            debug=False,
            request_headers=None,
        )
    )
    ws_result = _run(
        addon_tools._call_addon_ws(
            _hass(),
            addon,
            slug="5c53de3b_esphome",
            path="/ws/../secrets",
            body=None,
            port=None,
            timeout=30,
            debug=False,
            wait_for_close=True,
            message_limit=None,
            message_offset=0,
        )
    )

    assert http_result["success"] is False
    assert http_result["error_code"] == "invalid_path"
    assert ws_result["success"] is False
    assert ws_result["error_code"] == "invalid_path"


def test_resolve_esphome_addon_reports_ambiguous_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto-discovery refuses multiple ESPHome-looking add-ons."""

    async def list_addons(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "success": True,
            "addons": [
                {"slug": "5c53de3b_esphome", "name": "ESPHome Device Builder"},
                {"slug": "local_esphome_beta", "name": "ESPHome Beta"},
            ],
        }

    async def get_addon_info(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("ambiguous discovery must not fetch one arbitrary add-on")

    monkeypatch.setattr(addon_tools, "_list_addons", list_addons)
    monkeypatch.setattr(addon_tools, "_get_addon_info", get_addon_info)

    result = _run(addon_tools._resolve_esphome_addon(object(), slug=None))

    assert result["success"] is False
    assert result["error_code"] == "ambiguous_esphome_addon"
    assert result["matches"] == [
        {"slug": "5c53de3b_esphome", "name": "ESPHome Device Builder"},
        {"slug": "local_esphome_beta", "name": "ESPHome Beta"},
    ]


def test_resolve_explicit_slug_rejects_non_esphome_addon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit add-on slugs still have to match ESPHome metadata."""

    async def get_addon_info(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"success": True, "addon": {"slug": "core_mosquitto", "name": "Mosquitto"}}

    monkeypatch.setattr(addon_tools, "_get_addon_info", get_addon_info)

    result = _run(addon_tools._resolve_esphome_addon(object(), slug="core_mosquitto"))

    assert result["success"] is False
    assert result["error_code"] == "not_esphome_addon"
    assert result["slug"] == "core_mosquitto"


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
            _hass(),
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
    assert _FakeClientSession.url == "ws://127.0.0.1:8123/api/hassio_ingress/esphome/ws"
    assert _FakeClientSession.headers["Cookie"] == "ingress_session=test-ingress-session"
    assert result["_debug"]["request_headers"]["Cookie"] == "ingress_session=**REDACTED**"
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
            _hass(),
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


@pytest.mark.parametrize(
    ("frame_type", "expected_code"),
    [
        (addon_tools.aiohttp.WSMsgType.CLOSE, "connection_closed"),
        (addon_tools.aiohttp.WSMsgType.CLOSED, "connection_closed"),
        (addon_tools.aiohttp.WSMsgType.ERROR, "connection_failed"),
    ],
)
def test_device_builder_ws_reports_failure_before_server_info(
    monkeypatch: pytest.MonkeyPatch,
    frame_type: Any,
    expected_code: str,
) -> None:
    """The server-info handshake fails clearly on early close/error frames."""
    ws = _install_fake_ws(monkeypatch, [_ws_frame(frame_type)])

    result = _run(
        addon_tools._call_device_builder_ws_command(
            _hass(),
            _esphome_addon(),
            slug="5c53de3b_esphome",
            command="devices/list",
            args={},
            timeout=30,
            debug=False,
        )
    )

    assert result["success"] is False
    assert result["error_code"] == expected_code
    assert ws.sent == []


def test_device_builder_ws_command_error_frame_returns_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Device Builder command error frames are surfaced without being wrapped as success."""
    ws = _install_fake_ws(
        monkeypatch,
        [
            _text_frame({"server_version": "2026.6.0", "requires_auth": False}),
            _text_frame(
                {
                    "message_id": "esp-mcp-1",
                    "error_code": "validation_failed",
                    "details": "YAML is invalid",
                }
            ),
        ],
    )

    result = _run(
        addon_tools._call_device_builder_ws_command(
            _hass(),
            _esphome_addon(),
            slug="5c53de3b_esphome",
            command="devices/validate",
            args={"configuration": "bad.yaml"},
            timeout=30,
            debug=False,
        )
    )

    assert result == {
        "success": False,
        "error_code": "validation_failed",
        "error": "YAML is invalid",
        "command": "devices/validate",
        "server_info": {"server_version": "2026.6.0", "requires_auth": False},
    }
    assert ws.sent == [
        {
            "command": "devices/validate",
            "message_id": "esp-mcp-1",
            "args": {"configuration": "bad.yaml"},
        }
    ]


def test_generic_ws_proxy_sends_json_command_body_and_pages_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generic /ws proxy mode forwards caller envelopes and applies paging."""
    ws = _install_fake_ws(
        monkeypatch,
        [
            _text_frame({"message_id": "one", "result": 1}),
            _text_frame({"message_id": "two", "result": 2}),
            _text_frame({"message_id": "three", "result": 3}),
        ],
    )
    body = {"command": "devices/list", "message_id": "caller-1", "args": {}}

    result = _run(
        addon_tools._call_addon_ws(
            _hass(),
            _esphome_addon(),
            slug="5c53de3b_esphome",
            path="/ws",
            body=body,
            port=None,
            timeout=30,
            debug=True,
            wait_for_close=True,
            message_limit=1,
            message_offset=1,
        )
    )

    assert result["success"] is True
    assert result["messages"] == [{"message_id": "two", "result": 2}]
    assert result["message_count"] == 1
    assert result["closed_by"] == "message_limit"
    assert result["_debug"]["url"] == "ws://127.0.0.1:8123/api/hassio_ingress/esphome/ws"
    assert ws.sent == [body]


def test_device_builder_ws_ignores_malformed_and_unrelated_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only frames for the command message_id drive the returned result."""
    ws = _install_fake_ws(
        monkeypatch,
        [
            _text_frame("not-json"),
            _text_frame({"message_id": "other", "result": {"ignored": True}}),
            _text_frame("still-not-json"),
            _text_frame({"message_id": "esp-mcp-1", "result": {"ok": True}}),
        ],
    )

    result = _run(
        addon_tools._call_device_builder_ws_command(
            _hass(),
            _esphome_addon(),
            slug="5c53de3b_esphome",
            command="devices/get_config",
            args={"configuration": "kitchen.yaml"},
            timeout=30,
            debug=True,
        )
    )

    assert result["success"] is True
    assert result["result"] == {"ok": True}
    assert result["closed_by"] == "result"
    assert result["_debug"]["server_info"] is None
    assert result["_debug"]["command_message_count"] == 1
    assert result["_debug"]["messages"] == [
        "not-json",
        {"message_id": "other", "result": {"ignored": True}},
        "still-not-json",
        {"message_id": "esp-mcp-1", "result": {"ok": True}},
    ]
    assert ws.sent == [
        {
            "command": "devices/get_config",
            "message_id": "esp-mcp-1",
            "args": {"configuration": "kitchen.yaml"},
        }
    ]


def test_device_builder_ws_stream_message_limit_stops_without_terminal_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finite streams stop at the requested command-message count."""
    _install_fake_ws(
        monkeypatch,
        [
            _text_frame({"server_version": "2026.6.0", "requires_auth": False}),
            _text_frame({"message_id": "esp-mcp-1", "event": "output", "data": "one"}),
            _text_frame({"message_id": "esp-mcp-1", "event": "output", "data": "two"}),
        ],
    )

    result = _run(
        addon_tools._call_device_builder_ws_command(
            _hass(),
            _esphome_addon(),
            slug="5c53de3b_esphome",
            command="firmware/follow_job",
            args={"job_id": "job-1"},
            timeout=30,
            debug=True,
            stream=True,
            message_limit=2,
        )
    )

    assert result["success"] is True
    assert result["closed_by"] == "message_limit"
    assert result["events"] == [
        {"message_id": "esp-mcp-1", "event": "output", "data": "one"},
        {"message_id": "esp-mcp-1", "event": "output", "data": "two"},
    ]
    assert result["_debug"]["stop_stream_sent"] is False


def test_device_builder_ws_result_event_terminates_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Streaming Device Builder commands stop cleanly on result events."""
    _install_fake_ws(
        monkeypatch,
        [
            _text_frame({"server_version": "2026.6.0", "requires_auth": False}),
            _text_frame({"message_id": "esp-mcp-1", "event": "output", "data": "ok"}),
            _text_frame(
                {
                    "message_id": "esp-mcp-1",
                    "event": "result",
                    "data": {"success": True},
                }
            ),
        ],
    )

    result = _run(
        addon_tools._call_device_builder_ws_command(
            _hass(),
            _esphome_addon(),
            slug="5c53de3b_esphome",
            command="devices/validate",
            args={"configuration": "kitchen.yaml"},
            timeout=30,
            debug=True,
            stream=True,
            message_limit=10,
        )
    )

    assert result["success"] is True
    assert result["closed_by"] == "event_result"
    assert result["terminal_event"] == {
        "message_id": "esp-mcp-1",
        "event": "result",
        "data": {"success": True},
    }
    assert result["events"] == [
        {"message_id": "esp-mcp-1", "event": "output", "data": "ok"},
        {
            "message_id": "esp-mcp-1",
            "event": "result",
            "data": {"success": True},
        },
    ]
    assert result["_debug"]["stop_stream_sent"] is False


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
            _hass(),
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


@pytest.mark.parametrize(
    ("call_wrapper", "expected_command"),
    [
        pytest.param(
            lambda hass: addon_tools.list_device_builder_devices(
                hass,
                slug=None,
                query=None,
                state=None,
                include_importable=True,
                limit=100,
                timeout=60,
                debug=False,
            ),
            "devices/list",
            id="list_devices",
        ),
        pytest.param(
            lambda hass: addon_tools.search_device_builder_yaml(
                hass,
                slug=None,
                query="wifi",
                max_results=50,
                case_sensitive=False,
                context_lines=None,
                timeout=60,
                debug=False,
            ),
            "yaml/search",
            id="search_yaml",
        ),
        pytest.param(
            lambda hass: addon_tools.read_device_builder_config(
                hass,
                slug=None,
                configuration="kitchen.yaml",
                timeout=60,
                debug=False,
            ),
            "devices/get_config",
            id="read_yaml",
        ),
        pytest.param(
            lambda hass: addon_tools.write_device_builder_config(
                hass,
                slug=None,
                configuration="kitchen.yaml",
                content="esphome:\n  name: kitchen\n",
                allow_wipe=False,
                timeout=60,
                debug=False,
            ),
            "devices/update_config",
            id="write_yaml",
        ),
        pytest.param(
            lambda hass: addon_tools.run_device_builder_stream(
                hass,
                slug=None,
                command="devices/validate",
                args={"configuration": "kitchen.yaml"},
                timeout=300,
                debug=False,
                message_limit=200,
            ),
            "devices/validate",
            id="stream",
        ),
        pytest.param(
            lambda hass: addon_tools.queue_device_builder_firmware_job(
                hass,
                slug=None,
                command="firmware/compile",
                args={"configuration": "kitchen.yaml"},
                timeout=60,
                debug=False,
            ),
            "firmware/compile",
            id="queue_job",
        ),
        pytest.param(
            lambda hass: addon_tools.list_device_builder_firmware_jobs(
                hass,
                slug=None,
                status=None,
                configuration=None,
                limit=50,
                timeout=60,
                debug=False,
            ),
            "firmware/get_jobs",
            id="list_jobs",
        ),
        pytest.param(
            lambda hass: addon_tools.get_device_builder_firmware_job(
                hass,
                slug=None,
                job_id="job-1",
                timeout=60,
                debug=False,
            ),
            "firmware/get_job",
            id="get_job",
        ),
        pytest.param(
            lambda hass: addon_tools.follow_device_builder_firmware_job(
                hass,
                slug=None,
                job_id="job-1",
                message_limit=500,
                timeout=60,
                debug=False,
            ),
            "firmware/follow_job",
            id="follow_job",
        ),
    ],
)
def test_device_builder_wrappers_return_failures_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    call_wrapper: Any,
    expected_command: str,
) -> None:
    """Wrappers preserve lower-level Device Builder failures exactly."""
    seen_commands: list[str] = []
    failure = {
        "success": False,
        "error_code": "device_builder_failed",
        "error": "Device Builder rejected the command.",
    }

    async def device_builder_command(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        seen_commands.append(str(kwargs["command"]))
        return failure

    monkeypatch.setattr(addon_tools, "_device_builder_command", device_builder_command)

    result = _run(call_wrapper(object()))

    assert result is failure
    assert seen_commands == [expected_command]


@pytest.mark.parametrize(
    ("call_wrapper", "bad_result", "expected_error"),
    [
        pytest.param(
            lambda hass: addon_tools.list_device_builder_devices(
                hass,
                slug=None,
                query=None,
                state=None,
                include_importable=True,
                limit=100,
                timeout=60,
                debug=False,
            ),
            [],
            "Device Builder returned malformed devices/list result.",
            id="devices_list",
        ),
        pytest.param(
            lambda hass: addon_tools.search_device_builder_yaml(
                hass,
                slug=None,
                query="wifi",
                max_results=50,
                case_sensitive=False,
                context_lines=None,
                timeout=60,
                debug=False,
            ),
            {"matches": []},
            "Device Builder returned malformed yaml/search result.",
            id="yaml_search",
        ),
        pytest.param(
            lambda hass: addon_tools.list_device_builder_firmware_jobs(
                hass,
                slug=None,
                status=None,
                configuration=None,
                limit=50,
                timeout=60,
                debug=False,
            ),
            {"jobs": []},
            "Device Builder returned malformed firmware/get_jobs result.",
            id="firmware_jobs",
        ),
    ],
)
def test_device_builder_wrappers_reject_malformed_results(
    monkeypatch: pytest.MonkeyPatch,
    call_wrapper: Any,
    bad_result: Any,
    expected_error: str,
) -> None:
    """Structured wrappers fail closed when Device Builder returns the wrong shape."""

    async def device_builder_command(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"success": True, "result": bad_result}

    monkeypatch.setattr(addon_tools, "_device_builder_command", device_builder_command)

    result = _run(call_wrapper(object()))

    assert result["success"] is False
    assert result["error"] == expected_error


def test_read_write_wrappers_preserve_success_payload_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read returns raw content while write hides Device Builder's internal result."""
    responses = [
        {"success": True, "result": "wifi:\n  ssid: test\n"},
        {"success": True, "result": {"internal": "ignored"}},
    ]

    async def device_builder_command(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return responses.pop(0)

    monkeypatch.setattr(addon_tools, "_device_builder_command", device_builder_command)

    read_result = _run(
        addon_tools.read_device_builder_config(
            object(),
            slug=None,
            configuration="kitchen.yaml",
            timeout=60,
            debug=False,
        )
    )
    write_result = _run(
        addon_tools.write_device_builder_config(
            object(),
            slug=None,
            configuration="kitchen.yaml",
            content="wifi:\n",
            allow_wipe=True,
            timeout=60,
            debug=False,
        )
    )

    assert read_result == {
        "success": True,
        "configuration": "kitchen.yaml",
        "content": "wifi:\n  ssid: test\n",
    }
    assert write_result == {
        "success": True,
        "configuration": "kitchen.yaml",
        "message": "Config updated.",
    }


def test_http_body_parsing_handles_json_text_and_invalid_json() -> None:
    """HTTP parsing keeps invalid JSON as text instead of raising."""

    assert addon_tools._parse_http_body(
        "application/json; charset=utf-8",
        b'{"devices": []}',
    ) == {"devices": []}
    assert addon_tools._parse_http_body("text/plain", b"plain text") == "plain text"
    assert addon_tools._parse_http_body("application/json", b"{not-json") == "{not-json"


def test_truncate_response_bounds_large_text_and_structured_payloads() -> None:
    """Large add-on responses are bounded before reaching MCP clients."""
    large_text = "x" * (addon_tools._MAX_RESPONSE_SIZE + 5)
    truncated_text, text_was_truncated = addon_tools._truncate_response(large_text)

    assert text_was_truncated is True
    assert truncated_text == "x" * addon_tools._MAX_RESPONSE_SIZE

    large_payload = {"items": ["x" * addon_tools._MAX_RESPONSE_SIZE]}
    truncated_payload, payload_was_truncated = addon_tools._truncate_response(large_payload)

    assert payload_was_truncated is True
    assert truncated_payload == {
        "error": "RESPONSE_TOO_LARGE",
        "message": "Response exceeds 50KB.",
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
    ("command", "args"),
    [
        (
            "firmware/compile",
            {"configuration": "kitchen.yaml", "force_local": True},
        ),
        (
            "firmware/install",
            {
                "configuration": "kitchen.yaml",
                "port": "OTA",
                "force_local": True,
                "bootloader": False,
            },
        ),
    ],
)
def test_firmware_queue_wrapper_returns_job_and_preserves_args(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    args: dict[str, Any],
) -> None:
    """Firmware queue helper preserves compile/install command payloads."""
    seen: dict[str, Any] = {}
    job = {"job_id": "job-1", "status": "queued"}

    async def device_builder_command(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"success": True, "result": job}

    monkeypatch.setattr(addon_tools, "_device_builder_command", device_builder_command)

    result = _run(
        addon_tools.queue_device_builder_firmware_job(
            object(),
            slug="5c53de3b_esphome",
            command=command,
            args=args,
            timeout=60,
            debug=True,
        )
    )

    assert result == {"success": True, "job": job}
    assert seen == {
        "slug": "5c53de3b_esphome",
        "command": command,
        "args": args,
        "timeout": 60,
        "debug": True,
    }


def test_follow_job_uses_terminal_job_output_when_stream_has_no_output_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Job following falls back to the terminal job output array."""
    terminal_event = {
        "event": "result",
        "data": {"job_id": "job-1", "exit_code": 0, "output": ["one", 2]},
    }

    async def device_builder_command(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["stream"] is True
        assert kwargs["message_limit"] == 5
        return {
            "success": True,
            "events": [terminal_event],
            "terminal_event": terminal_event,
            "closed_by": "event_result",
        }

    monkeypatch.setattr(addon_tools, "_device_builder_command", device_builder_command)

    result = _run(
        addon_tools.follow_device_builder_firmware_job(
            object(),
            slug=None,
            job_id="job-1",
            message_limit=5,
            timeout=60,
            debug=False,
        )
    )

    assert result == {
        "success": True,
        "job_id": "job-1",
        "output": ["one", "2"],
        "output_line_count": 2,
        "job": {"job_id": "job-1", "exit_code": 0, "output": ["one", 2]},
        "exit_code": 0,
        "terminal_event": terminal_event,
        "closed_by": "event_result",
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
