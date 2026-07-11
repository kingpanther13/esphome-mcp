"""HAOS E2E for ESPHome MCP as an in-process custom component."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
    websocket_command,
)

from ..utilities.esphome_host import (
    HAOS_HOST_GATEWAY,
    compile_esphome,
    connected_api,
    host_yaml,
    reserve_port,
    run_binary,
)
from ..utilities.esphome_host import (
    wait_for_port as wait_for_host_port,
)
from ..utilities.streamable_http import parse_mcp_response

LOG = logging.getLogger(__name__)

HAOS_BOOT_TIMEOUT_S = 180 + 600
WEBHOOK_READY_TIMEOUT_S = 900
DEVICE_BUILDER_READY_TIMEOUT_S = 180
PYTEST_TIMEOUT_S = (
    HAOS_BOOT_TIMEOUT_S + WEBHOOK_READY_TIMEOUT_S + DEVICE_BUILDER_READY_TIMEOUT_S + 120
)
READY_POLL_S = 5
DEVICE_BUILDER_CONFIG_TIMEOUT_S = 180
FIRMWARE_JOB_TIMEOUT_S = 120

EXPECTED_ESP_TOOLS = {
    "esp_overview",
    "esp_list_devices",
    "esp_list_entities",
    "esp_manage_addon",
    "esp_dashboard_devices",
    "esp_search_yaml",
    "esp_get_yaml",
    "esp_update_yaml",
    "esp_validate_yaml",
    "esp_device_logs",
    "esp_compile_firmware",
    "esp_install_firmware",
    "esp_firmware_jobs",
    "esp_get_firmware_job",
    "esp_follow_firmware_job",
}

E2E_DEVICE_NAME = "ESP MCP E2E"
E2E_CONFIGURATION = "esp-mcp-e2e.yaml"
E2E_MARKER = "ESP MCP E2E Temperature"
E2E_UPDATED_MARKER = "ESP MCP E2E Humidity"
E2E_YAML = """\
esphome:
  name: esp-mcp-e2e
  friendly_name: ESP MCP E2E

host:

api:

logger:

sensor:
  - platform: template
    name: ESP MCP E2E Temperature
    id: esp_mcp_e2e_temperature
    unit_of_measurement: C
    accuracy_decimals: 1
    lambda: return 21.5;
    update_interval: 60s
"""
E2E_UPDATED_YAML = E2E_YAML.replace(E2E_MARKER, E2E_UPDATED_MARKER).replace(
    "esp_mcp_e2e_temperature",
    "esp_mcp_e2e_humidity",
)
LIVE_DEVICE_NAME = "esp-mcp-e2e-live"
LIVE_FRIENDLY_NAME = "ESP MCP E2E Live"
LIVE_CONFIGURATION = f"{LIVE_DEVICE_NAME}.yaml"
LIVE_SENSOR_NAME = "ESP MCP E2E Live Sensor"
LIVE_LOG_MARKER = "esp-mcp-e2e-live heartbeat"
LIVE_DEVICE_TIMEOUT_S = 420

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
def embedded_server() -> Iterator[tuple[str, str | None, str]]:
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
            configuration = _prepare_device_builder_fixture(base_url, session_id)
            yield base_url, session_id, configuration
        finally:
            collect_runtime_logs(base_url, token)


def _tool_call(
    base_url: str,
    session_id: str | None,
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    timeout: float = 60.0,
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
        timeout=timeout,
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


def _device_builder_command(
    base_url: str,
    session_id: str | None,
    command: str,
    args: dict[str, Any],
    *,
    message_limit: int = 2,
    timeout: float = 60.0,
) -> dict[str, Any]:
    parsed = _tool_call(
        base_url,
        session_id,
        "esp_manage_addon",
        {
            "path": "/ws",
            "websocket": True,
            "wait_for_close": True,
            "message_limit": message_limit,
            "body": {
                "command": command,
                "message_id": "haos-e2e-command",
                "args": args,
            },
            "debug": True,
            "timeout": int(timeout),
        },
        timeout=timeout,
    )
    payload = _tool_payload(parsed)
    assert payload["success"] is True, payload
    messages = payload.get("messages", [])
    assert isinstance(messages, list), payload
    server_info = next(
        (
            message
            for message in messages
            if isinstance(message, dict) and "server_version" in message
        ),
        None,
    )
    assert isinstance(server_info, dict), payload
    assert server_info.get("requires_auth") is False, payload
    assert server_info.get("ha_ingress") is True, payload
    matching = [
        message
        for message in messages
        if isinstance(message, dict) and message.get("message_id") == "haos-e2e-command"
    ]
    assert matching, payload
    for message in matching:
        if "error_code" in message:
            raise AssertionError(message)
        if "result" in message:
            return message
        if message.get("event") == "result":
            return message
    raise AssertionError(payload)


def _prepare_device_builder_fixture(base_url: str, session_id: str | None) -> str:
    create = _device_builder_command(
        base_url,
        session_id,
        "devices/create",
        {
            "name": E2E_DEVICE_NAME,
            "file_content": E2E_YAML,
            "overwrite": True,
        },
        timeout=DEVICE_BUILDER_CONFIG_TIMEOUT_S,
    )
    result = create.get("result")
    assert isinstance(result, dict), create
    configuration = str(result.get("configuration") or "")
    assert configuration == E2E_CONFIGURATION, create

    deadline = time.monotonic() + DEVICE_BUILDER_READY_TIMEOUT_S
    last_payload: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last_payload = _tool_payload(
            _tool_call(
                base_url,
                session_id,
                "esp_dashboard_devices",
                {"query": E2E_DEVICE_NAME, "limit": 10, "debug": True},
            )
        )
        if last_payload.get("success") is True and any(
            device.get("configuration") == configuration
            for device in last_payload.get("configured", [])
            if isinstance(device, dict)
        ):
            return configuration
        time.sleep(5)

    raise AssertionError(f"ESPHome Device Builder did not list {configuration!r}: {last_payload}")


def _assert_device_builder_configured(
    payload: dict[str, Any],
    configuration: str,
) -> None:
    assert payload["success"] is True, payload
    assert "configured_count" in payload
    assert "configured" in payload
    assert any(
        device.get("configuration") == configuration
        for device in payload.get("configured", [])
        if isinstance(device, dict)
    ), payload


def _job_id_from_payload(payload: dict[str, Any]) -> str:
    assert payload["success"] is True, payload
    job = payload.get("job")
    assert isinstance(job, dict), payload
    job_id = str(job.get("job_id") or "")
    assert job_id, payload
    return job_id


def _cancel_firmware_job(
    base_url: str,
    session_id: str | None,
    job_id: str,
) -> None:
    try:
        _device_builder_command(
            base_url,
            session_id,
            "firmware/cancel",
            {"job_id": job_id},
            timeout=FIRMWARE_JOB_TIMEOUT_S,
        )
    except AssertionError as err:
        LOG.warning("Could not cancel firmware job %s: %s", job_id, err)


def _ha_json(
    base_url: str,
    token: str,
    method: str,
    path: str,
    body: dict[str, Any],
    *,
    timeout: float = 60.0,
) -> dict[str, Any]:
    response = requests.request(
        method,
        f"{base_url}{path}",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
        timeout=timeout,
    )
    assert response.status_code < 400, response.text[:1000]
    data = response.json() if response.content else {}
    assert isinstance(data, dict), data
    return data


def _create_live_esphome_config_entry(
    base_url: str,
    token: str,
    *,
    api_port: int,
) -> dict[str, Any]:
    flow = _ha_json(
        base_url,
        token,
        "POST",
        "/api/config/config_entries/flow",
        {"handler": "esphome"},
    )
    flow_id = str(flow["flow_id"])
    result = _ha_json(
        base_url,
        token,
        "POST",
        f"/api/config/config_entries/flow/{flow_id}",
        {"host": HAOS_HOST_GATEWAY, "port": api_port},
        timeout=90.0,
    )
    assert result.get("type") == "create_entry", result
    return result


def _start_esphome_mcp_options_flow(base_url: str, token: str) -> dict[str, Any]:
    """Open the ESPHome MCP Configure form through HA's options-flow endpoint."""
    return _ha_json(
        base_url,
        token,
        "POST",
        "/api/config/config_entries/options/flow",
        {"handler": ESPHOME_MCP_SERVER_ENTRY_ID},
    )


async def _exercise_live_host_device_in_haos(
    base_url: str,
    session_id: str | None,
    *,
    tmp_path: Path,
) -> None:
    token = await asyncio.to_thread(login_for_token, base_url)
    with (
        reserve_port() as (api_port, api_socket),
        reserve_port() as (ota_port, ota_socket),
    ):
        live_yaml = host_yaml(
            api_port=api_port,
            ota_port=ota_port,
            name=LIVE_DEVICE_NAME,
            friendly_name=LIVE_FRIENDLY_NAME,
            log_marker=LIVE_LOG_MARKER,
            sensor_name=LIVE_SENSOR_NAME,
        )
        create = await asyncio.to_thread(
            _device_builder_command,
            base_url,
            session_id,
            "devices/create",
            {
                "name": LIVE_FRIENDLY_NAME,
                "file_content": live_yaml,
                "overwrite": True,
            },
            timeout=DEVICE_BUILDER_CONFIG_TIMEOUT_S,
        )
        result = create.get("result")
        assert isinstance(result, dict), create
        configuration = str(result.get("configuration") or "")
        assert configuration == LIVE_CONFIGURATION, create

        validate = _tool_payload(
            await asyncio.to_thread(
                _tool_call,
                base_url,
                session_id,
                "esp_validate_yaml",
                {
                    "configuration": configuration,
                    "message_limit": 200,
                    "debug": True,
                },
                timeout=300,
            )
        )
        assert validate["success"] is True, validate
        terminal = validate.get("terminal_event")
        assert isinstance(terminal, dict), validate
        terminal_data = terminal.get("data")
        assert isinstance(terminal_data, dict), validate
        assert terminal_data.get("success") is True or terminal_data.get("code") == 0, validate

        fetched = _tool_payload(
            await asyncio.to_thread(
                _tool_call,
                base_url,
                session_id,
                "esp_get_yaml",
                {"configuration": configuration},
            )
        )
        assert fetched["success"] is True, fetched
        config_path = tmp_path / configuration
        config_path.write_text(str(fetched["content"]), encoding="utf-8")
        binary_path = await compile_esphome(config_path, tmp_path)
        api_socket.close()
        ota_socket.close()

        async with run_binary(binary_path) as (process, _lines):
            await wait_for_host_port(api_port)
            assert process.returncode is None
            async with connected_api(api_port) as client:
                info = await client.device_info()
            assert info.name == LIVE_DEVICE_NAME

            await asyncio.to_thread(
                _create_live_esphome_config_entry,
                base_url,
                token,
                api_port=api_port,
            )

            deadline = time.monotonic() + LIVE_DEVICE_TIMEOUT_S
            last_devices: dict[str, Any] | None = None
            last_entities: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                last_devices = _tool_payload(
                    await asyncio.to_thread(
                        _tool_call,
                        base_url,
                        session_id,
                        "esp_list_devices",
                        {
                            "query": LIVE_FRIENDLY_NAME,
                            "config_entry_state": "loaded",
                            "limit": 10,
                        },
                    )
                )
                last_entities = _tool_payload(
                    await asyncio.to_thread(
                        _tool_call,
                        base_url,
                        session_id,
                        "esp_list_entities",
                        {
                            "query": LIVE_SENSOR_NAME,
                            "domain": "sensor",
                            "state": "42",
                            "limit": 10,
                        },
                    )
                )
                if (
                    last_devices.get("success") is True
                    and last_entities.get("success") is True
                    and last_devices.get("count", 0) >= 1
                    and last_entities.get("count", 0) >= 1
                ):
                    break
                await asyncio.sleep(5)
            else:
                raise AssertionError(
                    "Live ESPHome host device did not appear in HA registries: "
                    f"devices={last_devices} entities={last_entities}"
                )

            logs = _tool_payload(
                await asyncio.to_thread(
                    _tool_call,
                    base_url,
                    session_id,
                    "esp_device_logs",
                    {
                        "configuration": configuration,
                        "port": HAOS_HOST_GATEWAY,
                        "message_limit": 80,
                        "timeout": 60,
                        "debug": True,
                    },
                    timeout=120,
                )
            )
            assert logs["success"] is True, logs
            assert logs["command"] == "devices/logs", logs
            assert any(LIVE_LOG_MARKER in str(line) for line in logs.get("output", [])), logs


class TestEmbeddedServerOnHaos:
    def test_initialize_and_list_esp_tools(
        self,
        embedded_server: tuple[str, str | None, str],
    ) -> None:
        base_url, session_id, _configuration = embedded_server
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

        assert EXPECTED_ESP_TOOLS <= names
        assert all(str(name).startswith("esp_") for name in names)

    def test_overview_tool_runs_inside_haos(
        self,
        embedded_server: tuple[str, str | None, str],
    ) -> None:
        base_url, session_id, _configuration = embedded_server
        parsed = _tool_call(base_url, session_id, "esp_overview")
        payload = _tool_payload(parsed)

        assert payload["success"] is True
        assert payload["mcp_domain"] == "esphome_mcp"
        assert "device_count" in payload

    def test_local_brand_icon_uses_home_assistant_authenticated_proxy(
        self,
        embedded_server: tuple[str, str | None, str],
    ) -> None:
        base_url, _session_id, _configuration = embedded_server
        token = login_for_token(base_url)
        brands_auth = websocket_command(
            base_url,
            token,
            {"type": "brands/access_token"},
        )

        assert isinstance(brands_auth, dict), brands_auth
        brands_token = brands_auth.get("token")
        assert isinstance(brands_token, str) and brands_token, brands_auth

        response = requests.get(
            f"{base_url}/api/brands/integration/esphome_mcp/icon.png",
            params={"token": brands_token},
            timeout=60,
        )
        assert response.status_code == 200, response.text[:1000]
        assert response.headers.get("Content-Type", "").split(";", 1)[0] == "image/png"

        expected_icon = (
            Path(__file__).resolve().parents[4]
            / "custom_components"
            / "esphome_mcp"
            / "brand"
            / "icon.png"
        ).read_bytes()
        assert response.content == expected_icon

    def test_options_flow_shows_resolved_webhook_connect_url(
        self,
        embedded_server: tuple[str, str | None, str],
    ) -> None:
        base_url, _session_id, _configuration = embedded_server
        token = login_for_token(base_url)

        flow = _start_esphome_mcp_options_flow(base_url, token)
        assert flow.get("type") == "form", flow
        data_schema = flow.get("data_schema")
        assert isinstance(data_schema, list), flow
        field_names = {str(field.get("name")) for field in data_schema if isinstance(field, dict)}
        assert {
            "server_port",
            "bind_host",
            "webhook_auth",
            "enable_webhook",
            "external_url",
            "webhook_id_override",
            "secret_path_override",
            "regenerate_secrets",
        } <= field_names, flow
        assert "pip_spec" not in field_names
        placeholders = flow.get("description_placeholders")
        assert isinstance(placeholders, dict), flow
        connect_url = str(placeholders.get("connect_url") or "")
        urls = [
            line[2:].strip()
            for line in connect_url.splitlines()
            if line.startswith("- http") and "/api/webhook/" in line
        ]

        assert "Connect URL(s):" in connect_url, flow
        assert f"/api/webhook/{ESPHOME_MCP_SERVER_WEBHOOK_ID}" in connect_url
        assert urls, connect_url
        for url in urls:
            parsed = urlparse(url)
            assert parsed.scheme in {"http", "https"}, url
            assert parsed.netloc, url
            assert parsed.path == f"/api/webhook/{ESPHOME_MCP_SERVER_WEBHOOK_ID}", url
            assert "None" not in url
        assert "<your-home-assistant-url>" not in connect_url
        assert "Home Assistant URL unavailable" not in connect_url

    def test_sidebar_panel_is_not_registered(
        self,
        embedded_server: tuple[str, str | None, str],
    ) -> None:
        base_url, _session_id, _configuration = embedded_server
        token = login_for_token(base_url)

        panels = websocket_command(base_url, token, {"type": "get_panels"})

        assert isinstance(panels, dict), panels
        assert "esphome-mcp" not in panels
        assert all(
            not isinstance(panel, dict) or panel.get("config_panel_domain") != "esphome_mcp"
            for panel in panels.values()
        ), panels

    def test_home_assistant_esphome_registry_search_tools(
        self,
        embedded_server: tuple[str, str | None, str],
    ) -> None:
        base_url, session_id, _configuration = embedded_server
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

    def test_live_host_esphome_device_tools_work_end_to_end(
        self,
        embedded_server: tuple[str, str | None, str],
        tmp_path: Path,
    ) -> None:
        base_url, session_id, _configuration = embedded_server

        asyncio.run(
            _exercise_live_host_device_in_haos(
                base_url,
                session_id,
                tmp_path=tmp_path,
            )
        )

    def test_device_builder_list_tool_reaches_supervisor_addon_ingress(
        self,
        embedded_server: tuple[str, str | None, str],
    ) -> None:
        base_url, session_id, configuration = embedded_server
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
        _assert_device_builder_configured(last_payload, configuration)

        legacy_devices = _tool_payload(
            _tool_call(
                base_url,
                session_id,
                "esp_manage_addon",
                {"path": "/devices", "debug": True},
            )
        )
        assert legacy_devices["success"] is True, legacy_devices
        assert legacy_devices["status_code"] == 200, legacy_devices
        response = legacy_devices.get("response")
        assert isinstance(response, dict), legacy_devices
        assert "configured" in response
        assert "importable" in response

    def test_device_builder_yaml_tools_round_trip(
        self,
        embedded_server: tuple[str, str | None, str],
    ) -> None:
        base_url, session_id, configuration = embedded_server

        read_original = _tool_payload(
            _tool_call(base_url, session_id, "esp_get_yaml", {"configuration": configuration})
        )
        assert read_original["success"] is True, read_original
        assert E2E_MARKER in read_original.get("content", "")

        search_original = _tool_payload(
            _tool_call(
                base_url,
                session_id,
                "esp_search_yaml",
                {"query": E2E_MARKER, "max_results": 10},
            )
        )
        assert search_original["success"] is True, search_original
        assert search_original["count"] >= 1, search_original

        updated = _tool_payload(
            _tool_call(
                base_url,
                session_id,
                "esp_update_yaml",
                {
                    "configuration": configuration,
                    "content": E2E_UPDATED_YAML,
                    "allow_wipe": False,
                },
            )
        )
        assert updated["success"] is True, updated

        read_updated = _tool_payload(
            _tool_call(base_url, session_id, "esp_get_yaml", {"configuration": configuration})
        )
        assert read_updated["success"] is True, read_updated
        assert E2E_UPDATED_MARKER in read_updated.get("content", "")

        search_updated = _tool_payload(
            _tool_call(
                base_url,
                session_id,
                "esp_search_yaml",
                {"query": E2E_UPDATED_MARKER, "max_results": 10},
            )
        )
        assert search_updated["success"] is True, search_updated
        assert search_updated["count"] >= 1, search_updated

    def test_device_builder_validate_tool_runs_on_created_device(
        self,
        embedded_server: tuple[str, str | None, str],
    ) -> None:
        base_url, session_id, configuration = embedded_server
        payload = _tool_payload(
            _tool_call(
                base_url,
                session_id,
                "esp_validate_yaml",
                {
                    "configuration": configuration,
                    "message_limit": 200,
                    "debug": True,
                },
                timeout=300,
            )
        )

        assert payload["success"] is True, payload
        assert payload["command"] == "devices/validate"
        terminal = payload.get("terminal_event")
        assert isinstance(terminal, dict), payload
        assert terminal.get("event") == "result", payload
        data = terminal.get("data")
        assert isinstance(data, dict), payload
        assert data.get("success") is True or data.get("code") == 0, payload

    def test_device_builder_log_tool_reports_offline_result(
        self,
        embedded_server: tuple[str, str | None, str],
    ) -> None:
        base_url, session_id, configuration = embedded_server
        payload = _tool_payload(
            _tool_call(
                base_url,
                session_id,
                "esp_device_logs",
                {
                    "configuration": configuration,
                    "port": "OTA",
                    "message_limit": 25,
                    "timeout": 30,
                    "debug": True,
                },
                timeout=90,
            )
        )

        assert payload["success"] is True, payload
        assert payload["command"] == "devices/logs"
        assert payload["closed_by"] in {
            "event_result",
            "message_limit",
            "silence",
            "timeout",
        }, payload

    def test_device_builder_firmware_job_tools_round_trip(
        self,
        embedded_server: tuple[str, str | None, str],
    ) -> None:
        base_url, session_id, configuration = embedded_server
        compile_payload = _tool_payload(
            _tool_call(
                base_url,
                session_id,
                "esp_compile_firmware",
                {
                    "configuration": configuration,
                    "force_local": True,
                    "debug": True,
                },
                timeout=FIRMWARE_JOB_TIMEOUT_S,
            )
        )
        compile_job_id = _job_id_from_payload(compile_payload)
        try:
            jobs = _tool_payload(
                _tool_call(
                    base_url,
                    session_id,
                    "esp_firmware_jobs",
                    {"configuration": configuration, "limit": 10},
                    timeout=FIRMWARE_JOB_TIMEOUT_S,
                )
            )
            assert jobs["success"] is True, jobs
            assert any(
                job.get("job_id") == compile_job_id
                for job in jobs.get("jobs", [])
                if isinstance(job, dict)
            ), jobs

            single = _tool_payload(
                _tool_call(
                    base_url,
                    session_id,
                    "esp_get_firmware_job",
                    {"job_id": compile_job_id},
                    timeout=FIRMWARE_JOB_TIMEOUT_S,
                )
            )
            assert single["success"] is True, single
            assert single["found"] is True, single
            assert single["job"]["job_id"] == compile_job_id

            follow = _tool_payload(
                _tool_call(
                    base_url,
                    session_id,
                    "esp_follow_firmware_job",
                    {
                        "job_id": compile_job_id,
                        "message_limit": 25,
                        "timeout": 60,
                    },
                    timeout=FIRMWARE_JOB_TIMEOUT_S,
                )
            )
            assert follow["success"] is True, follow
            assert follow["job_id"] == compile_job_id
        finally:
            _cancel_firmware_job(base_url, session_id, compile_job_id)

    def test_device_builder_install_tool_queues_or_reports_offline_precondition(
        self,
        embedded_server: tuple[str, str | None, str],
    ) -> None:
        base_url, session_id, configuration = embedded_server
        payload = _tool_payload(
            _tool_call(
                base_url,
                session_id,
                "esp_install_firmware",
                {
                    "configuration": configuration,
                    "port": "OTA",
                    "force_local": True,
                    "debug": True,
                },
                timeout=FIRMWARE_JOB_TIMEOUT_S,
            )
        )

        if payload.get("success"):
            job_id = _job_id_from_payload(payload)
            _cancel_firmware_job(base_url, session_id, job_id)
            return

        assert payload.get("error_code") in {
            "invalid_args",
            "precondition_failed",
            "unavailable",
        }, payload
