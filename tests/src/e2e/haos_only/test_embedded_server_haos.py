"""HAOS E2E for ESPHome MCP as an in-process custom component."""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import requests
from haos_runtime import (
    ESPHOME_FIXTURE_DEVICE_ID,
    ESPHOME_FIXTURE_ENTITY_ID,
    ESPHOME_MCP_SERVER_ENTRY_ID,
    ESPHOME_MCP_SERVER_WEBHOOK_ID,
    HAOS_IMAGE_ENV,
    boot_haos_qemu,
    collect_runtime_logs,
    enable_config_entry,
    login_for_token,
)

from ..utilities.streamable_http import parse_mcp_response

LOG = logging.getLogger(__name__)

HAOS_BOOT_TIMEOUT_S = 180 + 600
WEBHOOK_READY_TIMEOUT_S = 600
DEVICE_BUILDER_READY_TIMEOUT_S = 180
PYTEST_TIMEOUT_S = (
    HAOS_BOOT_TIMEOUT_S + WEBHOOK_READY_TIMEOUT_S + DEVICE_BUILDER_READY_TIMEOUT_S + 120
)
READY_POLL_S = 5

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.slow,
    pytest.mark.haos_only,
    pytest.mark.timeout(PYTEST_TIMEOUT_S),
]


def _mcp_post(
    base_url: str,
    payload: dict[str, Any],
    *,
    session_id: str | None = None,
    timeout: float = 60.0,
) -> requests.Response:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return requests.post(
        f"{base_url}/api/webhook/{ESPHOME_MCP_SERVER_WEBHOOK_ID}",
        headers=headers,
        data=json.dumps(payload),
        timeout=timeout,
    )


def _parse_mcp(resp: requests.Response) -> dict[str, Any] | None:
    return parse_mcp_response(resp.headers.get("Content-Type", ""), resp.content)


def _initialize(base_url: str) -> tuple[bool, str | None]:
    resp = _mcp_post(
        base_url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "esphome-mcp-haos-e2e", "version": "1.0"},
            },
        },
    )
    parsed = _parse_mcp(resp)
    if not parsed or "result" not in parsed:
        return False, None
    session_id = resp.headers.get("Mcp-Session-Id")
    if session_id:
        _mcp_post(
            base_url,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            session_id=session_id,
        )
    return True, session_id


@pytest.fixture(scope="module")
def embedded_server() -> Iterator[tuple[str, str | None]]:
    """Boot HAOS, enable the baked ESPHome MCP entry, and wait for its webhook."""
    image_raw = os.environ.get(HAOS_IMAGE_ENV)
    if not image_raw:
        pytest.skip(f"{HAOS_IMAGE_ENV} is not set")
    image_path = Path(image_raw)
    if not image_path.exists():
        raise AssertionError(f"HAOS image does not exist: {image_path}")

    with boot_haos_qemu(image_path) as base_url:
        token = login_for_token(base_url)
        try:
            enable_config_entry(base_url, token, ESPHOME_MCP_SERVER_ENTRY_ID)
            LOG.info(
                "Enabled %s; waiting for the ESPHome MCP webhook",
                ESPHOME_MCP_SERVER_ENTRY_ID,
            )
            deadline = time.monotonic() + WEBHOOK_READY_TIMEOUT_S
            session_id: str | None = None
            ready = False
            while time.monotonic() < deadline:
                try:
                    ready, session_id = _initialize(base_url)
                except requests.exceptions.RequestException:
                    ready = False
                if ready:
                    break
                time.sleep(READY_POLL_S)
            if not ready:
                raise AssertionError(
                    "ESPHome MCP did not become reachable through the HA webhook "
                    f"within {WEBHOOK_READY_TIMEOUT_S}s of enabling "
                    f"{ESPHOME_MCP_SERVER_ENTRY_ID} at /api/webhook/"
                    f"{ESPHOME_MCP_SERVER_WEBHOOK_ID}. See ha-core-runtime.log "
                    "and supervisor-runtime.log in the HAOS diagnostics artifact."
                )
            LOG.info("ESPHome MCP webhook is ready")
            yield base_url, session_id
        finally:
            collect_runtime_logs(base_url, token)


def _tool_call(
    base_url: str,
    session_id: str | None,
    name: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resp = _mcp_post(
        base_url,
        {
            "jsonrpc": "2.0",
            "id": 100,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
        session_id=session_id,
    )
    parsed = _parse_mcp(resp)
    assert parsed is not None, f"unparseable tools/call response: {resp.text[:500]}"
    assert "result" in parsed, parsed
    return parsed


def _content_text(parsed: dict[str, Any]) -> str:
    content = parsed["result"].get("content", [])
    assert content, parsed
    return "\n".join(str(item.get("text", "")) for item in content if isinstance(item, dict))


def _tool_payload(parsed: dict[str, Any]) -> dict[str, Any]:
    text = _content_text(parsed)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as err:
        raise AssertionError(f"tool response was not JSON: {text[:500]}") from err
    assert isinstance(payload, dict), payload
    return payload


class TestEmbeddedServerOnHaos:
    def test_initialize_and_list_esp_tools(
        self,
        embedded_server: tuple[str, str | None],
    ) -> None:
        base_url, session_id = embedded_server
        resp = _mcp_post(
            base_url,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            session_id=session_id,
        )
        parsed = _parse_mcp(resp)
        assert parsed is not None, f"unparseable tools/list response: {resp.text[:500]}"
        assert "result" in parsed, parsed
        tools = parsed["result"].get("tools", [])
        names = {tool.get("name") for tool in tools}

        assert "esp_overview" in names
        assert "esp_list_devices" in names
        assert "esp_list_entities" in names
        assert "esp_manage_addon" in names
        assert "esp_dashboard_devices" in names
        assert "esp_search_yaml" in names
        assert "esp_compile_firmware" in names
        assert all(str(name).startswith("esp_") for name in names)

    def test_overview_tool_runs_inside_haos(
        self,
        embedded_server: tuple[str, str | None],
    ) -> None:
        base_url, session_id = embedded_server
        parsed = _tool_call(base_url, session_id, "esp_overview")
        payload = _tool_payload(parsed)

        assert payload["success"] is True
        assert payload["mcp_domain"] == "esphome_mcp"
        assert "device_count" in payload

    def test_home_assistant_esphome_registry_search_tools(
        self,
        embedded_server: tuple[str, str | None],
    ) -> None:
        base_url, session_id = embedded_server
        devices = _tool_payload(
            _tool_call(
                base_url,
                session_id,
                "esp_list_devices",
                {"query": "Kitchen ESPHome", "area": "kitchen", "limit": 10},
            )
        )
        assert devices["success"] is True, devices
        assert any(
            device.get("id") == ESPHOME_FIXTURE_DEVICE_ID
            and ["esphome", "kitchen-node"] in device.get("identifiers", [])
            for device in devices.get("devices", [])
        ), devices

        entities = _tool_payload(
            _tool_call(
                base_url,
                session_id,
                "esp_list_entities",
                {
                    "query": "Kitchen ESPHome Temperature",
                    "domain": "sensor",
                    "device_id": ESPHOME_FIXTURE_DEVICE_ID,
                    "limit": 10,
                },
            )
        )
        assert entities["success"] is True, entities
        assert any(
            entity.get("entity_id") == ESPHOME_FIXTURE_ENTITY_ID
            and entity.get("platform") == "esphome"
            for entity in entities.get("entities", [])
        ), entities

    def test_device_builder_list_tool_reaches_supervisor_addon_ingress(
        self,
        embedded_server: tuple[str, str | None],
    ) -> None:
        base_url, session_id = embedded_server
        deadline = time.monotonic() + DEVICE_BUILDER_READY_TIMEOUT_S
        last_payload: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            parsed = _tool_call(
                base_url,
                session_id,
                "esp_dashboard_devices",
                {"limit": 5, "debug": True},
            )
            last_payload = _tool_payload(parsed)
            if last_payload.get("success") is True:
                break
            time.sleep(5)

        assert last_payload is not None
        assert last_payload["success"] is True, last_payload
        assert "configured_count" in last_payload
        assert "configured" in last_payload
