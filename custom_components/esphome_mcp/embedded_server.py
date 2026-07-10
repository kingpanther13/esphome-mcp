"""Run the ESPHome MCP FastMCP server in-process inside Home Assistant."""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import re
import sys
import threading
import time
from contextlib import suppress
from functools import partial
from importlib import metadata
from typing import TYPE_CHECKING, Literal

from homeassistant.core import HomeAssistant

from .const import (
    DATA_LAST_PIP_SPEC,
    DATA_SECRET_PATH,
    DEFAULT_BIND_HOST,
    DEFAULT_PIP_SPEC,
    DEFAULT_SERVER_PORT,
    OPT_BIND_HOST,
    OPT_SERVER_PORT,
    SERVER_CONFIG_SUBDIR,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

_READY_TIMEOUT_SECONDS = 30.0
_READY_POLL_INTERVAL_SECONDS = 0.5
_STOP_JOIN_TIMEOUT_SECONDS = 10.0
_PIP_INSTALL_TIMEOUT_SECONDS = 300
_IMPORT_DEADLOCK_RETRY_DELAYS_SECONDS = (0.25, 0.5, 1.0, 2.0)
_MODULE_LOCK_DEADLOCK_TEXT = "deadlock detected by _ModuleLock"
_EXACT_FASTMCP_SPEC = re.compile(r"fastmcp==(?P<version>\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?)")


class EmbeddedServerError(Exception):
    """Raised when the in-process ESPHome MCP server cannot start."""

    def __init__(
        self,
        message: str,
        *,
        kind: Literal["package", "restart", "start"] = "start",
    ) -> None:
        """Store the message and failure kind."""
        super().__init__(message)
        self.kind = kind


class EmbeddedServerManager:
    """Manage the in-process ESPHome MCP server for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Bind the manager to Home Assistant and the config entry."""
        self._hass = hass
        self._entry = entry
        self._port = int(entry.options.get(OPT_SERVER_PORT, DEFAULT_SERVER_PORT))
        self._bind_host = str(entry.options.get(OPT_BIND_HOST, DEFAULT_BIND_HOST))
        self._secret_path = str(entry.data.get(DATA_SECRET_PATH, ""))
        self._pip_spec = DEFAULT_PIP_SPEC
        self._config_dir = hass.config.path(SERVER_CONFIG_SUBDIR)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._thread_exc: BaseException | None = None

    @property
    def port(self) -> int:
        """TCP port the server listens on."""
        return self._port

    @property
    def is_running(self) -> bool:
        """Return True while the worker thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    async def async_start(self) -> None:
        """Start the server thread."""
        if not self._secret_path:
            raise EmbeddedServerError(
                "Server secret path missing from the config entry; reload the integration."
            )

        await self._async_ensure_package()
        await self._hass.async_add_executor_job(os.makedirs, self._config_dir, 0o755, True)
        self._thread_exc = None
        self._thread = threading.Thread(
            target=self._thread_main,
            name="esphome-mcp-server",
            daemon=True,
        )
        self._thread.start()
        await self._async_wait_until_ready()

    async def async_stop(self) -> None:
        """Signal the worker thread to shut down and join it."""
        thread = self._thread
        if thread is None:
            return

        loop = self._loop
        stop_event = self._stop_event
        if loop is not None and stop_event is not None and not loop.is_closed():
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(stop_event.set)

        await self._hass.async_add_executor_job(thread.join, _STOP_JOIN_TIMEOUT_SECONDS)
        if thread.is_alive():
            _LOGGER.warning(
                "ESPHome MCP server thread did not stop within %.0fs",
                _STOP_JOIN_TIMEOUT_SECONDS,
            )
        self._thread = None
        self._loop = None
        self._stop_event = None
        self._thread_exc = None

    def _thread_main(self) -> None:
        """Thread entry point."""
        os.environ["ESPHOME_MCP_CONFIG_DIR"] = self._config_dir
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._stop_event = asyncio.Event()
        try:
            _import_server_runtime_with_retry()
            loop.run_until_complete(self._serve())
        except EmbeddedServerError as err:
            self._thread_exc = err
            _LOGGER.error("ESPHome MCP server dependency startup failed: %s", err)
        except ImportError as err:
            self._thread_exc = EmbeddedServerError(
                f"Could not import server dependency: {err}", kind="package"
            )
            _LOGGER.exception("ESPHome MCP dependency import failed")
        except Exception as err:
            self._thread_exc = err
            _LOGGER.exception("ESPHome MCP server thread crashed")
        finally:
            for label, coro_factory in (
                ("asyncgen", loop.shutdown_asyncgens),
                ("executor", loop.shutdown_default_executor),
            ):
                try:
                    loop.run_until_complete(coro_factory())
                except Exception:
                    _LOGGER.warning(
                        "Worker-loop %s shutdown failed during teardown",
                        label,
                        exc_info=True,
                    )
            loop.close()

    async def _serve(self) -> None:
        """Build and run the FastMCP HTTP server until stopped."""
        import uvicorn

        from .server import EspHomeMCPServer

        server = EspHomeMCPServer(self._hass)

        app = server.mcp.http_app(path=self._secret_path, stateless_http=True)
        config = uvicorn.Config(
            app,
            host=self._bind_host,
            port=self._port,
            timeout_graceful_shutdown=2,
            lifespan="on",
            ws="websockets-sansio",
            log_config=None,
        )
        uv_server = uvicorn.Server(config)

        assert self._stop_event is not None
        stop_task = asyncio.create_task(self._stop_event.wait())
        async with server.mcp._lifespan_manager():
            serve_task = asyncio.create_task(uv_server.serve())
            done, _pending = await asyncio.wait(
                {serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if stop_task in done:
                uv_server.should_exit = True
                await serve_task
            else:
                stop_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stop_task
                serve_task.result()

    async def _async_wait_until_ready(self) -> None:
        """Poll loopback until the server accepts connections."""
        deadline = self._hass.loop.time() + _READY_TIMEOUT_SECONDS
        while self._hass.loop.time() < deadline:
            if self._thread_exc is not None:
                if isinstance(self._thread_exc, EmbeddedServerError):
                    raise self._thread_exc
                raise EmbeddedServerError(
                    f"ESPHome MCP server failed to start: {self._thread_exc}"
                ) from self._thread_exc
            if self._thread is not None and not self._thread.is_alive():
                raise EmbeddedServerError("ESPHome MCP server thread exited during startup.")
            if await self._async_probe_port():
                _LOGGER.info(
                    "ESPHome MCP server is listening on %s:%d",
                    self._bind_host,
                    self._port,
                )
                return
            await asyncio.sleep(_READY_POLL_INTERVAL_SECONDS)

        await self.async_stop()
        raise EmbeddedServerError(
            f"ESPHome MCP server did not become reachable on port {self._port} "
            f"within {_READY_TIMEOUT_SECONDS:.0f}s."
        )

    async def _async_probe_port(self) -> bool:
        """Return True if a loopback TCP connection succeeds."""
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self._port),
                timeout=_READY_POLL_INTERVAL_SECONDS,
            )
        except (TimeoutError, OSError):
            return False
        writer.close()
        with suppress(OSError, TimeoutError):
            await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
        return True

    async def _async_ensure_package(self) -> None:
        """Ensure FastMCP server dependencies are importable inside Home Assistant."""
        from homeassistant.requirements import (
            RequirementsNotFound,
            async_process_requirements,
            pip_kwargs,
        )
        from homeassistant.util.package import install_package

        stored_spec = self._entry.data.get(DATA_LAST_PIP_SPEC)
        importable = await self._hass.async_add_executor_job(_server_dependencies_importable)
        installed_version = await self._hass.async_add_executor_job(_installed_fastmcp_version)
        required_version = _pinned_fastmcp_version(self._pip_spec)
        peer_specs = await self._hass.async_add_executor_job(_installed_peer_fastmcp_specs)
        shared_runtime_loaded = await self._hass.async_add_executor_job(_fastmcp_runtime_loaded)
        incompatible_peers = {
            distribution: spec
            for distribution, spec in peer_specs.items()
            if spec != self._pip_spec
        }
        if incompatible_peers:
            peer_summary = ", ".join(
                f"{distribution} requires {spec}"
                for distribution, spec in incompatible_peers.items()
            )
            raise EmbeddedServerError(
                f"{peer_summary}, but ESPHome MCP requires {self._pip_spec}. Refusing "
                "to replace a peer integration's shared FastMCP dependency. Update both "
                "MCP integrations to compatible versions, then restart Home Assistant.",
                kind="restart",
            )
        try:
            if installed_version == required_version and importable:
                await async_process_requirements(
                    self._hass,
                    f"ESPHome MCP server ({self._pip_spec})",
                    [self._pip_spec],
                    is_built_in=False,
                )
            else:
                if shared_runtime_loaded:
                    loaded_version = installed_version or "an unknown version"
                    raise EmbeddedServerError(
                        f"FastMCP {loaded_version} is already loaded by Home Assistant, "
                        f"but ESPHome MCP requires {required_version}. Refusing to replace "
                        "the shared runtime inside a running process. Update both MCP "
                        "integrations to compatible versions, then restart Home Assistant.",
                        kind="restart",
                    )
                kwargs = pip_kwargs(self._hass.config.config_dir)
                kwargs["timeout"] = max(
                    int(kwargs.get("timeout") or 0),
                    _PIP_INSTALL_TIMEOUT_SECONDS,
                )
                installed = await self._hass.async_add_executor_job(
                    partial(install_package, self._pip_spec, upgrade=True, **kwargs)
                )
                if not installed:
                    raise EmbeddedServerError(
                        f"Could not install the server requirement ({self._pip_spec!r}); "
                        "see the Home Assistant log for pip output.",
                        kind="package",
                    )
        except RequirementsNotFound as err:
            raise EmbeddedServerError(
                f"Could not install the server requirement ({self._pip_spec!r}): {err}",
                kind="package",
            ) from err

        if not await self._hass.async_add_executor_job(_server_dependencies_importable):
            raise EmbeddedServerError(
                f"Installed the server requirement ({self._pip_spec!r}) but FastMCP "
                "server dependencies are still not importable.",
                kind="package",
            )
        installed_version = await self._hass.async_add_executor_job(_installed_fastmcp_version)
        if installed_version != required_version:
            raise EmbeddedServerError(
                f"Installed FastMCP version {installed_version or 'unknown'} does not match "
                f"the required version {required_version}.",
                kind="package",
            )

        if stored_spec != self._pip_spec:
            self._hass.config_entries.async_update_entry(
                self._entry,
                data={**self._entry.data, DATA_LAST_PIP_SPEC: self._pip_spec},
            )


def _server_dependencies_importable() -> bool:
    """Return True when runtime packages can resolve without importing them."""
    importlib.invalidate_caches()
    return _module_resolves("fastmcp") and _module_resolves("uvicorn")


def _module_resolves(module_name: str) -> bool:
    """Return True when import machinery can resolve a module without importing it."""
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False


def _installed_fastmcp_version() -> str | None:
    """Return the installed FastMCP distribution version without importing it."""
    importlib.invalidate_caches()
    try:
        return metadata.version("fastmcp")
    except metadata.PackageNotFoundError:
        return None


def _installed_peer_fastmcp_specs() -> dict[str, str]:
    """Return FastMCP requirements declared by installed ha-mcp distributions."""
    peer_specs: dict[str, str] = {}
    for distribution in ("ha-mcp", "ha-mcp-dev"):
        try:
            requirements = metadata.requires(distribution) or []
        except metadata.PackageNotFoundError:
            continue
        fastmcp_requirements = [
            requirement.partition(";")[0].strip()
            for requirement in requirements
            if requirement.lower().startswith("fastmcp")
        ]
        if len(fastmcp_requirements) == 1:
            peer_specs[distribution] = fastmcp_requirements[0]
        elif fastmcp_requirements:
            peer_specs[distribution] = ", ".join(sorted(fastmcp_requirements))
    return peer_specs


def _pinned_fastmcp_version(pip_spec: str) -> str:
    """Extract the version from the code-owned exact FastMCP pin."""
    if (match := _EXACT_FASTMCP_SPEC.fullmatch(pip_spec)) is None:
        raise EmbeddedServerError(
            f"ESPHome MCP runtime requirement must be an exact FastMCP pin: {pip_spec!r}",
            kind="package",
        )
    return match.group("version")


def _fastmcp_runtime_loaded() -> bool:
    """Return whether any shared FastMCP module is loaded or mid-import."""
    return any(name == "fastmcp" or name.startswith("fastmcp.") for name in sys.modules)


def _import_server_runtime_with_retry() -> None:
    """Preload worker imports, retrying only importlib module-lock deadlocks."""
    total_attempts = len(_IMPORT_DEADLOCK_RETRY_DELAYS_SECONDS) + 1
    for attempt in range(1, total_attempts + 1):
        try:
            importlib.import_module("uvicorn")
            importlib.import_module(f"{__package__}.server")
            return
        except RuntimeError as err:
            if not _is_module_lock_deadlock(err):
                raise
            if attempt == total_attempts:
                raise EmbeddedServerError(
                    "FastMCP imports repeatedly collided with another Home Assistant "
                    "integration. Restart Home Assistant to clear the shared import state.",
                    kind="restart",
                ) from err
            delay = _IMPORT_DEADLOCK_RETRY_DELAYS_SECONDS[attempt - 1]
            _LOGGER.warning(
                "FastMCP import deadlock on attempt %d/%d; retrying in %.2fs",
                attempt,
                total_attempts,
                delay,
            )
            time.sleep(delay)


def _is_module_lock_deadlock(err: BaseException) -> bool:
    """Return whether an exception chain contains importlib's module-lock deadlock."""
    current: BaseException | None = err
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, RuntimeError) and _MODULE_LOCK_DEADLOCK_TEXT in str(current):
            return True
        current = current.__cause__ or current.__context__
    return False
