"""Minimal HAOS-QEMU runtime helpers for the embedded component E2E lane.

This is intentionally small but follows ha-mcp's HAOS harness shape: boot a
cached HAOS qcow2 under QEMU/KVM, wait for Home Assistant to answer, and let
tests drive the MCP webhook through Home Assistant.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

LOG = logging.getLogger(__name__)

BASE_HA_HOST_PORT = 18123
BASE_SSH_HOST_PORT = 12222
OVMF_CODE_PATH = os.environ.get("HAOS_BUILD_OVMF", "/usr/share/OVMF/OVMF_CODE.fd")
HAOS_IMAGE_ENV = "HAOS_TEST_IMAGE_PATH"

ESPHOME_MCP_SERVER_ENTRY_ID = "e2e_test_esphome_mcp_server_entry"
ESPHOME_MCP_SERVER_WEBHOOK_ID = "esp_mcp_e2e_haos"
ESPHOME_MCP_SERVER_SECRET_PATH = "/private_e2e_esphome_mcp_haos"
ESPHOME_MCP_SERVER_PORT = 9590
ESPHOME_FIXTURE_DEVICE_ID = "ee2e0000000000000000000000000001"
ESPHOME_FIXTURE_ENTITY_ID = "sensor.kitchen_esphome_temperature"
ONBOARDING_USERNAME = os.environ.get("HAOS_BUILD_USERNAME", "mcp")
ONBOARDING_PASSWORD = os.environ.get("HAOS_BUILD_PASSWORD", "mcp")


def ha_host_port() -> int:
    """Return the forwarded HA HTTP port."""
    return int(os.environ.get("HAOS_TEST_HA_PORT", str(BASE_HA_HOST_PORT)))


def ssh_host_port() -> int:
    """Return the forwarded HAOS SSH port."""
    return int(os.environ.get("HAOS_TEST_SSH_PORT", str(BASE_SSH_HOST_PORT)))


def wait_port(host: str, port: int, *, timeout: float) -> None:
    """Wait until a TCP port accepts connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(2.0)
            try:
                sock.connect((host, port))
                return
            except OSError:
                time.sleep(2.0)
    raise TimeoutError(f"{host}:{port} did not open within {timeout}s")


def wait_http_ok(url: str, *, timeout: float) -> None:
    """Wait until an HTTP endpoint returns 200."""
    deadline = time.monotonic() + timeout
    last_err: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5.0) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as err:
            last_err = err
        time.sleep(3.0)
    raise TimeoutError(f"{url} did not become ready within {timeout}s (last: {last_err})")


def _http_json(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: dict[str, object] | None = None,
    form: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> dict[str, object]:
    """Make a JSON/form request to HA and return a JSON object."""
    data: bytes | None
    headers: dict[str, str] = {}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    else:
        data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
    loaded = json.loads(raw) if raw else {}
    assert isinstance(loaded, dict), loaded
    return loaded


def login_for_token(
    base_url: str,
    username: str = ONBOARDING_USERNAME,
    password: str = ONBOARDING_PASSWORD,
) -> str:
    """Drive HA's login flow against the pre-onboarded HAOS image."""
    flow = _http_json(
        "POST",
        f"{base_url}/auth/login_flow",
        body={
            "client_id": base_url,
            "handler": ["homeassistant", None],
            "redirect_uri": base_url,
        },
    )
    flow_id = str(flow["flow_id"])
    submit = _http_json(
        "POST",
        f"{base_url}/auth/login_flow/{flow_id}",
        body={"client_id": base_url, "username": username, "password": password},
    )
    if submit.get("type") != "create_entry":
        raise RuntimeError(f"login_flow rejected HAOS test credentials: {submit!r}")
    token_resp = _http_json(
        "POST",
        f"{base_url}/auth/token",
        form={
            "client_id": base_url,
            "grant_type": "authorization_code",
            "code": str(submit["result"]),
        },
    )
    return str(token_resp["access_token"])


def enable_config_entry(
    base_url: str,
    token: str,
    entry_id: str = ESPHOME_MCP_SERVER_ENTRY_ID,
    *,
    timeout: float = 60.0,
) -> None:
    """Enable a baked-disabled config entry via HA's WebSocket API."""
    import websockets.sync.client

    ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"
    deadline = time.monotonic() + timeout
    with websockets.sync.client.connect(ws_url, max_size=None, open_timeout=30) as ws:
        first = json.loads(ws.recv(timeout=30))
        if first.get("type") != "auth_required":
            raise RuntimeError(f"WS handshake expected auth_required, got {first!r}")
        ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth_resp = json.loads(ws.recv(timeout=30))
        if auth_resp.get("type") != "auth_ok":
            raise RuntimeError(f"WS auth rejected: {auth_resp!r}")

        msg_id = 1
        ws.send(
            json.dumps(
                {
                    "id": msg_id,
                    "type": "config_entries/disable",
                    "entry_id": entry_id,
                    "disabled_by": None,
                }
            )
        )
        while time.monotonic() < deadline:
            remaining = max(deadline - time.monotonic(), 1.0)
            raw = ws.recv(timeout=remaining)
            if not isinstance(raw, str):
                raw = raw.decode()
            resp = json.loads(raw)
            if resp.get("id") != msg_id:
                continue
            if not resp.get("success", False):
                raise RuntimeError(f"config_entries/disable for {entry_id!r} failed: {resp!r}")
            return
    raise TimeoutError(f"config_entries/disable for {entry_id!r} got no response")


def collect_runtime_logs(base_url: str, token: str, dest: Path | None = None) -> None:
    """Fetch HA Core/Supervisor runtime logs before the HAOS VM shuts down."""
    log_dir = dest or Path("/tmp/haos-diagnostics")
    log_dir.mkdir(parents=True, exist_ok=True)
    endpoints = {
        "ha-core-runtime.log": f"{base_url}/api/hassio/core/logs?lines=20000",
        "supervisor-runtime.log": f"{base_url}/api/hassio/supervisor/logs?lines=20000",
    }
    for filename, url in endpoints.items():
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read()
        except (urllib.error.URLError, OSError) as err:
            body = f"failed to fetch {url}: {type(err).__name__}: {err}\n".encode()
        (log_dir / filename).write_bytes(body)


@contextmanager
def boot_haos_qemu(image_path: Path, serial_log: Path | None = None) -> Iterator[str]:
    """Boot a HAOS qcow2 under QEMU/KVM and yield the HA base URL."""
    if not Path("/dev/kvm").exists():
        raise RuntimeError("/dev/kvm not available; HAOS tests require KVM acceleration")

    serial = serial_log or Path("/tmp/haos-e2e-serial.log")
    ha_port = ha_host_port()
    cmd = [
        "qemu-system-x86_64",
        "-machine",
        "q35,accel=kvm",
        "-cpu",
        "host",
        "-smp",
        "2",
        "-m",
        "4096",
        "-drive",
        f"if=pflash,format=raw,readonly=on,file={OVMF_CODE_PATH}",
        "-drive",
        f"if=virtio,file={image_path},format=qcow2",
        "-netdev",
        f"user,id=net0,hostfwd=tcp:127.0.0.1:{ha_port}-:8123,"
        f"hostfwd=tcp:127.0.0.1:{ssh_host_port()}-:22",
        "-device",
        "virtio-net-pci,netdev=net0",
        "-display",
        "none",
        "-serial",
        f"file:{serial}",
    ]
    LOG.info("Booting HAOS from %s (serial log: %s)", image_path, serial)
    proc = subprocess.Popen(cmd)
    base_url = f"http://127.0.0.1:{ha_port}"
    try:
        wait_port("127.0.0.1", ha_port, timeout=180)
        wait_http_ok(f"{base_url}/manifest.json", timeout=600)
        LOG.info("HAOS frontend ready at %s", base_url)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            LOG.warning("QEMU did not exit within 60s; sending SIGKILL")
            proc.kill()
            proc.wait()
