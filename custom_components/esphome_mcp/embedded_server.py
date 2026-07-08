"""Run the ESPHome MCP FastMCP server in-process inside Home Assistant."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from contextlib import suppress
from typing import TYPE_CHECKING, Literal

from homeassistant.core import HomeAssistant

from .const import (
    DATA_SECRET_PATH,
    DEFAULT_BIND_HOST,
    DEFAULT_SERVER_PORT,
    DOMAIN,
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


class EmbeddedServerError(Exception):
    """Raised when the in-process ESPHome MCP server cannot start."""

    def __init__(
        self, message: str, *, kind: Literal["package", "start"] = "start"
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
            loop.run_until_complete(self._serve())
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

        from .server import EspHomeMCPServer, register_status_routes

        server = EspHomeMCPServer(self._hass)
        register_status_routes(server.mcp, server, self._secret_path)

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
