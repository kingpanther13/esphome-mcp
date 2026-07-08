"""ESPHome host-platform compile/run helpers for E2E tests.

Adapted from ESPHome's own ``tests/integration`` host harness. The helpers
compile a real ``host:`` configuration, run the generated native binary under
a PTY, and connect through the ESPHome native API.
"""

from __future__ import annotations

import asyncio
import os
import pty
import signal
import socket
import sys
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import esphome.config
from aioesphomeapi import APIClient, ReconnectLogic
from esphome.core import CORE
from esphome.platformio.toolchain import get_idedata

LOCALHOST = "127.0.0.1"
HAOS_HOST_GATEWAY = os.environ.get("HAOS_QEMU_HOST_GATEWAY", "10.0.2.2")
DEVICE_NAME = "esp-mcp-host-device"
FRIENDLY_NAME = "ESPHome MCP Host Device"
CONFIGURATION = f"{DEVICE_NAME}.yaml"
LOG_MARKER = "esp-mcp-host-device heartbeat"
SENSOR_NAME = "ESPHome MCP Host Sensor"
API_CONNECT_TIMEOUT_S = 30.0
PORT_WAIT_TIMEOUT_S = 60.0


def platformio_cache_dir() -> Path:
    """Return the shared PlatformIO cache used by host-device E2E tests."""
    cache_dir = Path.home() / ".esphome-mcp-host-e2e" / "platformio"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def platformio_env(cache_dir: Path) -> dict[str, str]:
    """Return ESPHome integration-test style PlatformIO cache environment."""
    env = os.environ.copy()
    env["PLATFORMIO_CORE_DIR"] = str(cache_dir)
    env["PLATFORMIO_CACHE_DIR"] = str(cache_dir / ".cache")
    env["PLATFORMIO_LIBDEPS_DIR"] = str(cache_dir / "libdeps")
    env["ESPHOME_SKIP_CLEAN_BUILD"] = "1"
    return env


@contextmanager
def reserve_port(host: str = LOCALHOST) -> Generator[tuple[int, socket.socket]]:
    """Reserve an unused TCP port until the caller is ready to release it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, 0))
    try:
        yield sock.getsockname()[1], sock
    finally:
        sock.close()


async def wait_for_port(
    port: int,
    *,
    host: str = LOCALHOST,
    timeout: float = PORT_WAIT_TIMEOUT_S,
) -> None:
    """Wait until a TCP port accepts connections."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            _, writer = await asyncio.open_connection(host, port)
        except OSError:
            await asyncio.sleep(0.25)
            continue
        writer.close()
        await writer.wait_closed()
        return
    raise TimeoutError(f"{host}:{port} did not open within {timeout}s")


async def compile_esphome(config_path: Path, work_dir: Path, cache_dir: Path | None = None) -> Path:
    """Compile an ESPHome config and return the generated native ELF path."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "esphome",
        "compile",
        str(config_path),
        cwd=work_dir,
        stdin=asyncio.subprocess.DEVNULL,
        start_new_session=True,
        env=platformio_env(cache_dir or platformio_cache_dir()),
        close_fds=False,
    )
    await proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(
            f"Failed to compile {config_path}; esphome exited {proc.returncode}. "
            "Run pytest with -s to see compiler output."
        )

    loop = asyncio.get_running_loop()

    def _read_binary_path() -> Path:
        CORE.reset()
        CORE.config_path = config_path
        config = esphome.config.read_config({"command": "compile", "config": str(config_path)})
        if config is None:
            raise RuntimeError(f"Failed to re-read compiled config {config_path}")
        return Path(get_idedata(config).firmware_elf_path)

    binary_path = await loop.run_in_executor(None, _read_binary_path)
    if not binary_path.exists():
        raise RuntimeError(f"Compiled binary missing at {binary_path}")
    return binary_path


async def _read_pty_lines(
    stream: asyncio.StreamReader,
    lines: list[str],
    line_callback: Callable[[str], None] | None,
) -> None:
    while line := await stream.readline():
        decoded = (
            line.replace(b"\r", b"")
            .replace(b"\n", b"")
            .decode("utf-8", errors="backslashreplace")
            .rstrip()
        )
        lines.append(decoded)
        if line_callback is not None:
            line_callback(decoded)


@asynccontextmanager
async def run_binary(
    binary_path: Path,
    *,
    line_callback: Callable[[str], None] | None = None,
) -> AsyncGenerator[tuple[asyncio.subprocess.Process, list[str]]]:
    """Run a compiled ESPHome host binary under a PTY and clean it up."""
    controller_fd, device_fd = pty.openpty()
    process = await asyncio.create_subprocess_exec(
        str(binary_path),
        stdout=device_fd,
        stderr=device_fd,
        stdin=asyncio.subprocess.DEVNULL,
        start_new_session=True,
        pass_fds=(device_fd,),
        close_fds=False,
    )
    os.close(device_fd)

    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await loop.connect_read_pipe(
        lambda: protocol,
        os.fdopen(controller_fd, "rb", 0),
    )
    lines: list[str] = []
    read_task = asyncio.create_task(_read_pty_lines(reader, lines, line_callback))

    try:
        await asyncio.sleep(0)
        if process.returncode is not None:
            raise RuntimeError(f"{binary_path} exited immediately: {process.returncode}")
        yield process, lines
    finally:
        read_task.cancel()
        await asyncio.gather(read_task, return_exceptions=True)
        transport.close()
        if process.returncode is None:
            process.send_signal(signal.SIGINT)
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
                    await process.wait()


@asynccontextmanager
async def connected_api(port: int, *, host: str = LOCALHOST) -> AsyncGenerator[APIClient]:
    """Connect to an ESPHome native API endpoint and disconnect on exit."""
    client = APIClient(
        address=host,
        port=port,
        password="",
        noise_psk=None,
        client_info="esphome-mcp-host-e2e",
    )
    connected = asyncio.get_running_loop().create_future()

    async def on_connect() -> None:
        if not connected.done():
            connected.set_result(None)

    async def on_disconnect(expected_disconnect: bool) -> None:
        if not connected.done() and not expected_disconnect:
            connected.set_exception(RuntimeError("Disconnected before connect completed"))

    async def on_connect_error(err: Exception) -> None:
        if not connected.done():
            connected.set_exception(err)

    reconnect = ReconnectLogic(
        client=client,
        on_connect=on_connect,
        on_disconnect=on_disconnect,
        zeroconf_instance=None,
        name=f"{host}:{port}",
        on_connect_error=on_connect_error,
    )
    try:
        await reconnect.start()
        await asyncio.wait_for(connected, timeout=API_CONNECT_TIMEOUT_S)
        yield client
    finally:
        await reconnect.stop()
        await client.disconnect()


def host_yaml(
    *,
    api_port: int,
    ota_port: int,
    name: str = DEVICE_NAME,
    friendly_name: str = FRIENDLY_NAME,
    log_marker: str = LOG_MARKER,
    sensor_name: str = SENSOR_NAME,
) -> str:
    """Return a host-mode ESPHome config with API, OTA, logs, and a sensor."""
    return f"""\
esphome:
  name: {name}
  friendly_name: {friendly_name}
  platformio_options:
    build_flags:
      - "-DDEBUG"
      - "-DESPHOME_DEBUG"

host:

api:
  port: {api_port}

ota:
  - platform: esphome
    port: {ota_port}

logger:
  level: DEBUG

sensor:
  - platform: template
    name: {sensor_name}
    id: host_sensor
    lambda: return 42.0;
    update_interval: 1s

interval:
  - interval: 1s
    then:
      - logger.log: "{log_marker}"
"""
