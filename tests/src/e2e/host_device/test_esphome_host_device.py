"""E2E coverage against a real ESPHome host-platform device.

The host compile/run harness is adapted from ESPHome's own
``tests/integration`` fixtures and ``test_host_ota.py``. This test keeps the
full HAOS lane responsible for Home Assistant custom-component ingress, then
proves the device-facing helper path against a real compiled ESPHome binary
with native API logs and OTA.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from aioesphomeapi import LogLevel
from aiohttp import WSMsgType, web
from esphome import espota2

from custom_components.esphome_mcp import addon_tools

from ..utilities.esphome_host import (
    CONFIGURATION,
    DEVICE_NAME,
    LOCALHOST,
    LOG_MARKER,
    compile_esphome,
    connected_api,
    host_yaml,
    reserve_port,
    run_binary,
    wait_for_port,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.slow,
    pytest.mark.host_device,
    pytest.mark.timeout(900),
]


class _HostDeviceBuilderServer:
    """Tiny current-WS-compatible Device Builder stand-in backed by a real device."""

    def __init__(
        self,
        *,
        binary_path: Path,
        api_port: int,
        ota_port: int,
    ) -> None:
        self.binary_path = binary_path
        self.api_port = api_port
        self.ota_port = ota_port
        self.jobs: dict[str, dict[str, Any]] = {}
        self._runner: web.AppRunner | None = None
        self.port = 0

    async def __aenter__(self) -> _HostDeviceBuilderServer:
        app = web.Application()
        app.router.add_get("/ws", self._ws)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, LOCALHOST, 0)
        await site.start()
        server = site._server
        assert server is not None
        sock = next(iter(server.sockets))
        self.port = int(sock.getsockname()[1])
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(autoclose=False)
        await ws.prepare(request)
        await ws.send_json(
            {
                "server_version": "host-device-e2e",
                "requires_auth": False,
                "ha_ingress": True,
            }
        )
        async for msg in ws:
            if msg.type is not WSMsgType.TEXT:
                continue
            frame = json.loads(msg.data)
            command = frame["command"]
            message_id = frame["message_id"]
            args = frame.get("args") or {}
            if command == "devices/list":
                await self._send_devices(ws, message_id)
            elif command == "devices/logs":
                await self._send_logs(ws, message_id)
            elif command == "firmware/install":
                await self._install(ws, message_id, args)
            elif command == "firmware/follow_job":
                await self._follow_job(ws, message_id, str(args["job_id"]))
            elif command == "devices/stop_stream":
                await ws.send_json({"message_id": message_id, "result": {"cancelled": False}})
        return ws

    async def _send_devices(self, ws: web.WebSocketResponse, message_id: str) -> None:
        await ws.send_json(
            {
                "message_id": message_id,
                "result": {
                    "configured": [
                        {
                            "name": DEVICE_NAME,
                            "friendly_name": "ESPHome MCP Host Device",
                            "configuration": CONFIGURATION,
                            "state": "online",
                        }
                    ],
                    "importable": [],
                },
            }
        )

    async def _send_logs(self, ws: web.WebSocketResponse, message_id: str) -> None:
        queue: asyncio.Queue[str] = asyncio.Queue()

        async with connected_api(self.api_port) as client:
            unsubscribe = client.subscribe_logs(
                lambda msg: queue.put_nowait(
                    msg.message.decode("utf-8", errors="replace").rstrip()
                ),
                log_level=LogLevel.LOG_LEVEL_DEBUG,
            )
            try:
                deadline = asyncio.get_running_loop().time() + 20.0
                while asyncio.get_running_loop().time() < deadline:
                    remaining = deadline - asyncio.get_running_loop().time()
                    try:
                        line = await asyncio.wait_for(queue.get(), timeout=remaining)
                    except TimeoutError:
                        break
                    await ws.send_json(
                        {
                            "message_id": message_id,
                            "event": "output",
                            "data": line,
                        }
                    )
                    if LOG_MARKER in line:
                        await ws.send_json(
                            {
                                "message_id": message_id,
                                "event": "result",
                                "data": {"success": True, "code": 0},
                            }
                        )
                        return
                await ws.send_json(
                    {
                        "message_id": message_id,
                        "event": "result",
                        "data": {"success": False, "code": 1},
                    }
                )
            finally:
                if callable(unsubscribe):
                    unsubscribe()

    async def _install(
        self,
        ws: web.WebSocketResponse,
        message_id: str,
        args: dict[str, Any],
    ) -> None:
        assert args["configuration"] == CONFIGURATION
        assert args["port"] in {"OTA", LOCALHOST}
        job_id = "host-ota-install-1"
        loop = asyncio.get_running_loop()
        rc, output = await loop.run_in_executor(
            None,
            espota2.run_ota,
            LOCALHOST,
            self.ota_port,
            None,
            self.binary_path,
        )
        job = {
            "job_id": job_id,
            "configuration": CONFIGURATION,
            "status": "completed" if rc == 0 else "failed",
            "exit_code": rc,
            "output": [f"espota2 rc={rc}", *(str(output).splitlines() if output else [])],
        }
        self.jobs[job_id] = job
        await ws.send_json({"message_id": message_id, "result": job})

    async def _follow_job(
        self,
        ws: web.WebSocketResponse,
        message_id: str,
        job_id: str,
    ) -> None:
        job = self.jobs[job_id]
        for line in job["output"]:
            await ws.send_json(
                {
                    "message_id": message_id,
                    "event": "output",
                    "data": line,
                }
            )
        await ws.send_json({"message_id": message_id, "event": "result", "data": job})


def _patch_device_builder_route(
    monkeypatch: pytest.MonkeyPatch,
    server: _HostDeviceBuilderServer,
) -> None:
    async def fake_resolve(_hass: Any, _slug: str | None) -> dict[str, Any]:
        return {
            "success": True,
            "addon": {
                "slug": "host_device_builder",
                "name": "ESPHome Device Builder",
                "state": "started",
                "ingress": True,
                "ingress_entry": "/",
            },
        }

    async def fake_route(
        _hass: Any,
        _addon: dict[str, Any],
        normalized_path: str,
        *,
        port: int | None,
        websocket: bool,
    ) -> tuple[str, dict[str, str]]:
        assert port is None
        assert websocket is True
        return f"ws://{LOCALHOST}:{server.port}/{normalized_path}", {}

    monkeypatch.setattr(addon_tools, "_resolve_esphome_addon", fake_resolve)
    monkeypatch.setattr(addon_tools, "_route_for_addon", fake_route)


@pytest.mark.asyncio
async def test_mcp_device_builder_helpers_reach_real_esphome_host_device(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP Device Builder helpers can list, log, and OTA a real ESPHome host device."""
    with (
        reserve_port() as (api_port, api_socket),
        reserve_port() as (ota_port, ota_socket),
    ):
        config_path = tmp_path / CONFIGURATION
        config_path.write_text(host_yaml(api_port=api_port, ota_port=ota_port), encoding="utf-8")
        binary_path = await compile_esphome(config_path, tmp_path)
        api_socket.close()
        ota_socket.close()

        async with run_binary(binary_path) as (process, _lines):
            await wait_for_port(api_port)
            assert process.returncode is None
            async with connected_api(api_port) as client:
                info = await client.device_info()
            assert info.name == DEVICE_NAME

            async with _HostDeviceBuilderServer(
                binary_path=binary_path,
                api_port=api_port,
                ota_port=ota_port,
            ) as server:
                _patch_device_builder_route(monkeypatch, server)
                hass = object()

                devices = await addon_tools.list_device_builder_devices(
                    hass,
                    slug=None,
                    query="host device",
                    state="online",
                    include_importable=True,
                    limit=10,
                    timeout=30,
                    debug=True,
                )
                assert devices["success"] is True, devices
                assert devices["configured"][0]["configuration"] == CONFIGURATION

                logs = await addon_tools.run_device_builder_stream(
                    hass,
                    slug=None,
                    command="devices/logs",
                    args={
                        "configuration": CONFIGURATION,
                        "port": "OTA",
                        "no_states": True,
                    },
                    timeout=30,
                    debug=True,
                    message_limit=10,
                )
                assert logs["success"] is True, logs
                assert any(LOG_MARKER in str(line) for line in logs["output"]), logs

                install = await addon_tools.queue_device_builder_firmware_job(
                    hass,
                    slug=None,
                    command="firmware/install",
                    args={
                        "configuration": CONFIGURATION,
                        "port": LOCALHOST,
                        "force_local": True,
                        "bootloader": False,
                    },
                    timeout=60,
                    debug=True,
                )
                assert install["success"] is True, install
                job = install["job"]
                assert job["status"] == "completed", install
                assert job["exit_code"] == 0, install

                follow = await addon_tools.follow_device_builder_firmware_job(
                    hass,
                    slug=None,
                    job_id=job["job_id"],
                    message_limit=20,
                    timeout=30,
                    debug=True,
                )
                assert follow["success"] is True, follow
                assert follow["exit_code"] == 0, follow
                assert any("espota2 rc=0" in str(line) for line in follow["output"]), follow

                await wait_for_port(api_port)
                assert process.returncode is None
