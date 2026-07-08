"""Minimal HAOS-QEMU runtime helpers for the embedded component E2E lane.

This is intentionally small but follows ha-mcp's HAOS harness shape: boot a
cached HAOS qcow2 under QEMU/KVM, wait for Home Assistant to answer, and let
tests drive the MCP webhook through Home Assistant.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import time
import urllib.error
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
