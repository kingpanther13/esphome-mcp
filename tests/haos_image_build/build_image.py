#!/usr/bin/env python3
"""Build the HAOS qcow2 used by the ESPHome MCP embedded E2E lane.

The harness is adapted from ha-mcp's HAOS embedded lane. This project only
needs the in-component proof: a real HAOS VM with the ESPHome MCP custom
component baked into `/config/custom_components` and the official ESPHome
Device Builder add-on installed and running.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOG = logging.getLogger("haos_image_build")

# renovate: datasource=github-releases depName=home-assistant/operating-system
HAOS_VERSION = "18.0"
HAOS_QCOW2_URL = (
    f"https://github.com/home-assistant/operating-system/releases/download/"
    f"{HAOS_VERSION}/haos_ova-{HAOS_VERSION}.qcow2.xz"
)

ONBOARDING_USER = os.environ.get("HAOS_BUILD_USERNAME", "mcp")
ONBOARDING_PASSWORD = os.environ.get("HAOS_BUILD_PASSWORD", "mcp")
ONBOARDING_NAME = "ESPHome MCP CI"

HA_HOST_PORT = int(os.environ.get("HAOS_BUILD_HA_PORT", "18123"))
SSH_HOST_PORT = int(os.environ.get("HAOS_BUILD_SSH_PORT", "12222"))
OVMF_CODE_PATH = os.environ.get("HAOS_BUILD_OVMF", "/usr/share/OVMF/OVMF_CODE.fd")

ESPHOME_MCP_DOMAIN = "esphome_mcp"
ESPHOME_MCP_UNIQUE_ID = "esphome_mcp-server"
ESPHOME_MCP_ENTRY_ID = "e2e_test_esphome_mcp_server_entry"
ESPHOME_MCP_WEBHOOK_ID = "esp_mcp_e2e_haos"
ESPHOME_MCP_SECRET_PATH = "/private_e2e_esphome_mcp_haos"
ESPHOME_MCP_PORT = 9590
ESPHOME_FIXTURE_DEVICE_ID = "ee2e0000000000000000000000000001"
ESPHOME_FIXTURE_ENTITY_REGISTRY_ID = "ee2e0000000000000000000000000002"
ESPHOME_FIXTURE_ENTITY_ID = "sensor.kitchen_esphome_temperature"
ESPHOME_FIXTURE_NODE_ID = "kitchen-node"


@dataclass(frozen=True)
class Addon:
    """An add-on to install from the Supervisor store."""

    repo: str | None
    name: str
    start: bool = True


ESPHOME_DEVICE_BUILDER_ADDON = Addon(
    repo="https://github.com/esphome/home-assistant-addon",
    name="ESPHome Device Builder",
)


def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    LOG.debug("$ %s", " ".join(cmd))
    return subprocess.run(cmd, check=True, text=True, **kwargs)


def _http(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: dict[str, Any] | None = None,
    form: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """JSON/form HTTP helper for Home Assistant setup calls."""
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
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as err:
        err_body = ""
        try:
            err_body = err.read().decode()
        except (OSError, UnicodeDecodeError):
            pass
        LOG.error("%s %s -> HTTP %d: %s", method, url, err.code, err_body[:500])
        raise
    return json.loads(raw) if raw else {}


def _wait_port(port: int, host: str = "127.0.0.1", timeout: float = 180.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(2.0)
            try:
                sock.connect((host, port))
                return
            except OSError:
                time.sleep(2.0)
    raise TimeoutError(f"{host}:{port} did not open within {timeout}s")


def _wait_http_ok(url: str, timeout: float = 300.0) -> None:
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


def fetch_haos_qcow2(work_dir: Path) -> Path:
    """Download and decompress the pinned HAOS qcow2."""
    archive = work_dir / f"haos_ova-{HAOS_VERSION}.qcow2.xz"
    qcow2 = work_dir / "haos-test-image.qcow2"
    if qcow2.exists():
        LOG.info("Reusing existing qcow2 at %s", qcow2)
        return qcow2
    LOG.info("Downloading HAOS %s", HAOS_VERSION)
    _run(["curl", "-sfL", "-o", str(archive), HAOS_QCOW2_URL])
    LOG.info("Decompressing %s", archive.name)
    _run(["xz", "-dk", "--force", str(archive)])
    archive.with_suffix("").rename(qcow2)
    archive.unlink(missing_ok=True)
    _run(["qemu-img", "resize", str(qcow2), "32G"])
    return qcow2


def start_qemu(qcow2: Path, work_dir: Path) -> subprocess.Popen[bytes]:
    """Boot HAOS in QEMU with KVM and NAT networking."""
    serial_log = work_dir / "haos-serial.log"
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
        f"if=virtio,file={qcow2},format=qcow2",
        "-netdev",
        f"user,id=net0,hostfwd=tcp:127.0.0.1:{HA_HOST_PORT}-:8123,"
        f"hostfwd=tcp:127.0.0.1:{SSH_HOST_PORT}-:22",
        "-device",
        "virtio-net-pci,netdev=net0",
        "-display",
        "none",
        "-serial",
        f"file:{serial_log}",
    ]
    LOG.info("Booting HAOS (serial log: %s)", serial_log)
    return subprocess.Popen(cmd)


def stop_qemu(proc: subprocess.Popen[bytes], ws: HAWebSocket | None) -> None:
    """Try graceful HAOS shutdown, then terminate QEMU if needed."""
    if ws is not None:
        try:
            ws.supervisor_api("/host/shutdown", method="post", timeout=10.0)
        except Exception as err:
            LOG.warning("Supervisor shutdown call failed: %r; sending SIGTERM", err)
            proc.terminate()
    else:
        proc.terminate()
    try:
        proc.wait(timeout=120)
    except subprocess.TimeoutExpired:
        LOG.warning("QEMU did not exit cleanly; killing")
        proc.kill()
        proc.wait()


def onboard(base_url: str) -> str:
    """Create the first user and return an access token."""
    LOG.info("Onboarding first user")
    resp = _http(
        "POST",
        f"{base_url}/api/onboarding/users",
        body={
            "client_id": base_url,
            "name": ONBOARDING_NAME,
            "username": ONBOARDING_USER,
            "password": ONBOARDING_PASSWORD,
            "language": "en",
        },
    )
    token_resp = _http(
        "POST",
        f"{base_url}/auth/token",
        form={
            "client_id": base_url,
            "grant_type": "authorization_code",
            "code": resp["auth_code"],
        },
    )
    return token_resp["access_token"]


class HAWebSocket:
    """Minimal HA WebSocket client for Supervisor API commands."""

    def __init__(self, base_url: str, token: str) -> None:
        self._ws_url = (
            base_url.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"
        )
        self._token = token
        self._ws = None
        self._next_id = 0

    def __enter__(self) -> HAWebSocket:
        from websockets.sync.client import connect

        self._ws = connect(self._ws_url, open_timeout=30, close_timeout=10)
        auth_req = json.loads(self._ws.recv())
        if auth_req.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected WS handshake message: {auth_req}")
        self._ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        auth_resp = json.loads(self._ws.recv())
        if auth_resp.get("type") != "auth_ok":
            raise RuntimeError(f"WS auth rejected: {auth_resp}")
        LOG.info("WS connected to %s (ha_version=%s)", self._ws_url, auth_resp.get("ha_version"))
        self._wait_supervisor_api_ready()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except (OSError, RuntimeError) as err:
                LOG.debug("WS close error: %r", err)

    def reconnect(self) -> None:
        """Reconnect after HA Core or Supervisor restarts."""
        if self._ws is not None:
            try:
                self._ws.close()
            except (OSError, RuntimeError):
                pass
        self._ws = None
        self._next_id = 0
        self.__enter__()

    def _wait_supervisor_api_ready(self, timeout: float = 60.0) -> None:
        start = time.monotonic()
        delay = 1.0
        while True:
            try:
                self.supervisor_api("/supervisor/info", method="get", timeout=10.0)
                return
            except WSCommandError as err:
                if err.code != "unknown_command":
                    raise
                if time.monotonic() - start >= timeout:
                    raise RuntimeError(
                        f"hassio supervisor/api WS handler did not register within {timeout:.0f}s"
                    ) from err
                time.sleep(delay)
                delay = min(delay * 1.5, 5.0)

    def supervisor_api(
        self,
        endpoint: str,
        *,
        method: str = "get",
        data: dict[str, Any] | None = None,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        """Issue one Home Assistant ``supervisor/api`` WebSocket command."""
        assert self._ws is not None
        self._next_id += 1
        msg_id = self._next_id
        msg: dict[str, Any] = {
            "id": msg_id,
            "type": "supervisor/api",
            "endpoint": endpoint,
            "method": method,
            "timeout": timeout,
        }
        if data is not None:
            msg["data"] = data
        self._ws.send(json.dumps(msg))
        while True:
            raw = self._ws.recv()
            if not isinstance(raw, str):
                raw = raw.decode()
            resp = json.loads(raw)
            if resp.get("id") != msg_id:
                continue
            if not resp.get("success", True):
                err = resp.get("error") or {}
                code = err.get("code") if isinstance(err, dict) else None
                raise WSCommandError(
                    f"supervisor/api {method} {endpoint} failed: {err}",
                    code=code,
                )
            return resp.get("result", {}) or {}


class WSCommandError(RuntimeError):
    """Supervisor/Core WS failure with a structured error code."""

    def __init__(self, message: str, *, code: str | None) -> None:
        super().__init__(message)
        self.code = code


try:
    from websockets.exceptions import WebSocketException as _WebSocketException

    _SUPERVISOR_TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
        WSCommandError,
        OSError,
        TimeoutError,
        _WebSocketException,
    )
except ImportError:
    _SUPERVISOR_TRANSIENT_ERRORS = (WSCommandError, OSError, TimeoutError)


def _wait_supervisor_ready(ws: HAWebSocket, *, update_timeout: float = 600.0) -> None:
    """Wait until Supervisor responds and finishes its first self-update."""
    info = ws.supervisor_api("/supervisor/info", method="get", timeout=30.0)
    LOG.info(
        "Supervisor ready: version=%s latest=%s arch=%s",
        info.get("version"),
        info.get("version_latest"),
        info.get("arch"),
    )
    if not info.get("update_available") and info.get("version_latest"):
        return

    deadline = time.monotonic() + update_timeout
    last_version = info.get("version")
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        time.sleep(10.0)
        try:
            info = ws.supervisor_api("/supervisor/info", method="get", timeout=30.0)
        except _SUPERVISOR_TRANSIENT_ERRORS as err:
            last_error = err
            LOG.debug("Transient error polling /supervisor/info: %r", err)
            try:
                ws.reconnect()
            except _SUPERVISOR_TRANSIENT_ERRORS + (RuntimeError,) as reconnect_err:
                LOG.warning("Reconnect during Supervisor wait failed: %r", reconnect_err)
            continue
        version = info.get("version")
        if version != last_version:
            LOG.info("Supervisor version changed: %s -> %s", last_version, version)
            last_version = version
        if not info.get("update_available") and info.get("version_latest"):
            LOG.info("Supervisor self-update complete: version=%s", version)
            return
    suffix = f"; last error: {last_error!r}" if last_error else ""
    raise TimeoutError(
        f"Supervisor did not finish self-updating within {update_timeout:.0f}s{suffix}"
    )


def _add_repository(ws: HAWebSocket, repo_url: str) -> None:
    LOG.info("Adding add-on repository %s", repo_url)
    try:
        ws.supervisor_api(
            "/store/repositories",
            method="post",
            data={"repository": repo_url},
            timeout=120.0,
        )
    except RuntimeError as err:
        if "already in the store" in str(err):
            LOG.info("Repository %s already registered", repo_url)
            return
        raise


def _reload_store(ws: HAWebSocket) -> None:
    ws.supervisor_api("/store/reload", method="post", timeout=120.0)


def _discover_slug(ws: HAWebSocket, addon: Addon) -> str:
    """Resolve an add-on slug by current store metadata."""
    resp = ws.supervisor_api("/store", method="get")
    store_addons = resp.get("addons", [])
    exact = [entry for entry in store_addons if entry.get("name") == addon.name]
    candidates = exact or [
        entry for entry in store_addons if addon.name.lower() in str(entry.get("name", "")).lower()
    ]
    if not candidates:
        sample = [
            {"name": entry.get("name"), "slug": entry.get("slug"), "repo": entry.get("repository")}
            for entry in store_addons[:25]
        ]
        raise RuntimeError(f"Add-on {addon.name!r} not found after store refresh. Sample: {sample}")
    if len(candidates) == 1:
        return str(candidates[0]["slug"])
    for candidate in candidates:
        if addon.repo is not None and candidate.get("repository") != "core":
            return str(candidate["slug"])
    return str(candidates[0]["slug"])


def _addon_info_or_none(ws: HAWebSocket, slug: str) -> dict[str, Any] | None:
    try:
        return ws.supervisor_api(f"/addons/{slug}/info", method="get", timeout=30.0)
    except RuntimeError as err:
        if "not found" in str(err).lower() or "does not exist" in str(err).lower():
            return None
        raise


def _addon_is_installed(info: dict[str, Any] | None) -> bool:
    """Return true when Supervisor info represents an installed add-on."""
    if not info:
        return False
    state = str(info.get("state") or "").lower()
    if state in {"", "unknown"}:
        return False
    if info.get("installed") is False:
        return False
    return True


def _install_addon_with_retry(
    ws: HAWebSocket,
    slug: str,
    *,
    timeout: float,
    attempts: int = 2,
) -> None:
    """Install an add-on, retrying once across transient Supervisor/store failures."""
    for attempt in range(1, attempts + 1):
        try:
            ws.supervisor_api(
                f"/store/addons/{slug}/install",
                method="post",
                timeout=timeout,
            )
            return
        except _SUPERVISOR_TRANSIENT_ERRORS as err:
            if "already installed" in str(err).lower():
                LOG.info("Add-on %s already installed", slug)
                return
            if attempt >= attempts:
                raise
            LOG.warning(
                "Add-on %s install attempt %d/%d failed (%r); retrying",
                slug,
                attempt,
                attempts,
                err,
            )
            time.sleep(20.0)
            try:
                ws.reconnect()
                _reload_store(ws)
            except _SUPERVISOR_TRANSIENT_ERRORS + (RuntimeError,) as prep_err:
                LOG.warning("Reconnect/store reload before retry failed: %r", prep_err)


def install_esphome_device_builder(ws: HAWebSocket) -> str:
    """Install and start the official ESPHome Device Builder add-on."""
    _wait_supervisor_ready(ws)
    addon = ESPHOME_DEVICE_BUILDER_ADDON
    if addon.repo:
        _add_repository(ws, addon.repo)
        _reload_store(ws)
    slug = _discover_slug(ws, addon)
    LOG.info("Installing %s (slug=%s)", addon.name, slug)
    info = _addon_info_or_none(ws, slug)
    if not _addon_is_installed(info):
        _install_addon_with_retry(ws, slug, timeout=900.0)
        info = ws.supervisor_api(f"/addons/{slug}/info", method="get", timeout=60.0)
    ws.supervisor_api(
        f"/addons/{slug}/options",
        method="post",
        data={"boot": "auto"},
        timeout=60.0,
    )
    state = info.get("state")
    if addon.start and state != "started":
        LOG.info("Starting %s (state=%s)", slug, state)
        ws.supervisor_api(f"/addons/{slug}/start", method="post", timeout=180.0)
    return slug


def _check_core_auth(base_url: str, token: str) -> None:
    cfg = _http("GET", f"{base_url}/api/config", token=token, timeout=10.0)
    LOG.info("AUTH OK: /api/config version=%s state=%s", cfg.get("version"), cfg.get("state"))


def _load_storage_list(path: Path, *, expected_key: str, list_key: str) -> dict[str, Any]:
    data = json.loads(path.read_text())
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("data"), dict)
        or not isinstance(data["data"].get(list_key), list)
    ):
        raise RuntimeError(f"{path} has unexpected shape; expected dict with data.{list_key} list")
    if data.get("key") != expected_key:
        raise RuntimeError(
            f"{path} has unexpected key {data.get('key')!r}; expected {expected_key!r}"
        )
    return data


def _load_storage_entries(path: Path) -> dict[str, Any]:
    return _load_storage_list(path, expected_key="core.config_entries", list_key="entries")


def _inject_esphome_mcp_entry(config_dir: Path) -> None:
    ce_path = config_dir / ".storage" / "core.config_entries"
    ce_data = _load_storage_entries(ce_path)
    entries = ce_data["data"]["entries"]
    if not any(entry.get("entry_id") == ESPHOME_MCP_ENTRY_ID for entry in entries):
        entries.append(
            {
                "created_at": "2026-07-08T00:00:00+00:00",
                "data": {
                    "webhook_id": ESPHOME_MCP_WEBHOOK_ID,
                    "secret_path": ESPHOME_MCP_SECRET_PATH,
                },
                "disabled_by": "user",
                "discovery_keys": {},
                "domain": ESPHOME_MCP_DOMAIN,
                "entry_id": ESPHOME_MCP_ENTRY_ID,
                "minor_version": 1,
                "modified_at": "2026-07-08T00:00:00+00:00",
                "options": {
                    "server_port": ESPHOME_MCP_PORT,
                    "bind_host": "127.0.0.1",
                    "webhook_auth": "none",
                    "enable_webhook": True,
                },
                "pref_disable_new_entities": False,
                "pref_disable_polling": False,
                "source": "import",
                "subentries": [],
                "title": "ESPHome MCP Server",
                "unique_id": ESPHOME_MCP_UNIQUE_ID,
                "version": 1,
            }
        )
    ce_path.write_text(json.dumps(ce_data, indent=2))
    LOG.info("Injected disabled ESPHome MCP config entry (%s)", ESPHOME_MCP_ENTRY_ID)


def _inject_esphome_registry_fixtures(config_dir: Path) -> None:
    """Seed one ESPHome registry device/entity for HA search-tool E2E coverage."""
    storage_dir = config_dir / ".storage"
    device_path = storage_dir / "core.device_registry"
    device_data = _load_storage_list(
        device_path,
        expected_key="core.device_registry",
        list_key="devices",
    )
    devices = device_data["data"]["devices"]
    if not any(device.get("id") == ESPHOME_FIXTURE_DEVICE_ID for device in devices):
        devices.append(
            {
                "area_id": "kitchen",
                "config_entries": [],
                "config_entries_subentries": {},
                "configuration_url": None,
                "connections": [],
                "created_at": "2026-07-08T00:00:00+00:00",
                "disabled_by": None,
                "entry_type": None,
                "hw_version": "esp32",
                "id": ESPHOME_FIXTURE_DEVICE_ID,
                "identifiers": [["esphome", ESPHOME_FIXTURE_NODE_ID]],
                "labels": ["e2e"],
                "manufacturer": "ESPHome",
                "model": "ESP32 DevKit",
                "model_id": None,
                "modified_at": "2026-07-08T00:00:00+00:00",
                "name_by_user": "Kitchen ESPHome",
                "name": "Kitchen ESPHome",
                "primary_config_entry": None,
                "serial_number": None,
                "sw_version": "2026.7.0",
                "via_device_id": None,
            }
        )
    device_path.write_text(json.dumps(device_data, indent=2))

    entity_path = storage_dir / "core.entity_registry"
    entity_data = _load_storage_list(
        entity_path,
        expected_key="core.entity_registry",
        list_key="entities",
    )
    entities = entity_data["data"]["entities"]
    if not any(entity.get("entity_id") == ESPHOME_FIXTURE_ENTITY_ID for entity in entities):
        entities.append(
            {
                "aliases": [],
                "area_id": None,
                "categories": {},
                "capabilities": {"state_class": "measurement"},
                "config_entry_id": None,
                "config_subentry_id": None,
                "created_at": "2026-07-08T00:00:00+00:00",
                "device_class": None,
                "device_id": ESPHOME_FIXTURE_DEVICE_ID,
                "disabled_by": None,
                "entity_category": None,
                "entity_id": ESPHOME_FIXTURE_ENTITY_ID,
                "hidden_by": None,
                "icon": None,
                "id": ESPHOME_FIXTURE_ENTITY_REGISTRY_ID,
                "has_entity_name": True,
                "labels": ["e2e"],
                "modified_at": "2026-07-08T00:00:00+00:00",
                "name": "Kitchen ESPHome Temperature",
                "options": {"conversation": {"should_expose": False}},
                "original_device_class": "temperature",
                "original_icon": None,
                "original_name": "Kitchen ESPHome Temperature",
                "platform": "esphome",
                "suggested_object_id": "kitchen_esphome_temperature",
                "supported_features": 0,
                "translation_key": None,
                "unique_id": f"{ESPHOME_FIXTURE_NODE_ID}-temperature",
                "previous_unique_id": None,
                "unit_of_measurement": "C",
            }
        )
    entity_path.write_text(json.dumps(entity_data, indent=2))
    LOG.info(
        "Injected ESPHome registry fixture (%s, %s)",
        ESPHOME_FIXTURE_DEVICE_ID,
        ESPHOME_FIXTURE_ENTITY_ID,
    )


def bake_component_into_config(qcow2: Path) -> None:
    """Copy live HA config out, inject this component, and tar it back in."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    component_src = repo_root / "custom_components" / ESPHOME_MCP_DOMAIN
    if not component_src.exists():
        raise RuntimeError(f"Custom component source missing: {component_src}")

    workdir = Path(tempfile.mkdtemp(prefix="esphome-mcp-haos-bake-"))
    try:
        LOG.info("Copying /supervisor/homeassistant out of qcow2")
        _run(
            [
                "guestfish",
                "--ro",
                "-a",
                str(qcow2),
                "run",
                ":",
                "mount",
                "/dev/sda8",
                "/",
                ":",
                "copy-out",
                "/supervisor/homeassistant",
                str(workdir),
            ]
        )
        config_dir = workdir / "homeassistant"
        if not config_dir.exists():
            raise RuntimeError(f"guestfish copy-out did not create {config_dir}")

        cc_dir = config_dir / "custom_components"
        cc_dir.mkdir(exist_ok=True)
        dest = cc_dir / ESPHOME_MCP_DOMAIN
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(component_src, dest)
        LOG.info("Staged custom component %s", ESPHOME_MCP_DOMAIN)

        _inject_esphome_mcp_entry(config_dir)
        _inject_esphome_registry_fixtures(config_dir)

        db_src = config_dir / "home-assistant_v2.db"
        if db_src.exists():
            import sqlite3

            vacuumed = workdir / "home-assistant_v2.db"
            con = sqlite3.connect(str(db_src))
            try:
                con.execute(f"VACUUM INTO '{vacuumed}'")
            finally:
                con.close()
            shutil.move(str(vacuumed), str(db_src))

        seed_tar = workdir / "config.tar"
        _run(
            [
                "tar",
                "--numeric-owner",
                "--owner=0",
                "--group=0",
                "-C",
                str(workdir),
                "-cf",
                str(seed_tar),
                "homeassistant",
            ]
        )

        probe = subprocess.run(
            ["guestfish", "--ro", "-a", str(qcow2), "run", ":", "list-filesystems"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        LOG.info("guestfish filesystems on qcow2:\n%s", probe.stdout)
        if probe.returncode != 0:
            raise RuntimeError(
                f"guestfish list-filesystems failed (rc={probe.returncode}): {probe.stderr}"
            )

        _run(
            [
                "guestfish",
                "--rw",
                "-a",
                str(qcow2),
                "run",
                ":",
                "mount",
                "/dev/sda8",
                "/",
                ":",
                "tar-in",
                str(seed_tar),
                "/supervisor",
            ]
        )
        LOG.info("Component bake complete")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def build(work_dir: Path, output: Path) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    qcow2 = fetch_haos_qcow2(work_dir)
    qemu = start_qemu(qcow2, work_dir)
    base_url = f"http://127.0.0.1:{HA_HOST_PORT}"
    try:
        _wait_port(HA_HOST_PORT, timeout=180)
        _wait_http_ok(f"{base_url}/manifest.json", timeout=600)
        token = onboard(base_url)
        _check_core_auth(base_url, token)
        with HAWebSocket(base_url, token) as ws:
            install_esphome_device_builder(ws)
            stop_qemu(qemu, ws)
    except Exception:
        LOG.exception("Image build failed; leaving qcow2 in %s for inspection", qcow2)
        if qemu.poll() is None:
            try:
                qemu.terminate()
                qemu.wait(timeout=60)
            except (ProcessLookupError, subprocess.TimeoutExpired) as err:
                LOG.warning("QEMU teardown after build failure: %r", err)
        raise

    bake_component_into_config(qcow2)
    output.parent.mkdir(parents=True, exist_ok=True)
    LOG.info("Copying qcow2 to %s", output)
    _run(["cp", "--reflink=auto", str(qcow2), str(output)])
    LOG.info("Wrote %s (%.1f MB)", output, output.stat().st_size / 1024 / 1024)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path(os.environ.get("HAOS_BUILD_WORK_DIR", "/tmp/haos-build")),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("haos-test-image.qcow2"),
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not Path("/dev/kvm").exists():
        LOG.error("/dev/kvm not available; HAOS build requires KVM acceleration")
        return 2
    build(args.work_dir, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
