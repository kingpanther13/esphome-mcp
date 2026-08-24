"""Run the ESPHome MCP FastMCP server in-process inside Home Assistant."""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import sys
import threading
import time
from contextlib import suppress
from importlib import metadata
from typing import TYPE_CHECKING, Literal

from homeassistant.core import HomeAssistant
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from .const import (
    DATA_LAST_PIP_SPEC,
    DATA_SECRET_PATH,
    DEFAULT_BIND_HOST,
    DEFAULT_SERVER_PORT,
    HA_MCP_BRINGUP_TASK_KEY,
    HA_MCP_DOMAIN,
    HA_MCP_ENTRY_TYPE_KEY,
    HA_MCP_MANAGER_KEY,
    HA_MCP_SERVER_ENTRY_TYPE,
    OPT_BIND_HOST,
    OPT_SERVER_PORT,
    SERVER_CONFIG_SUBDIR,
    STANDALONE_FASTMCP_SPEC,
    STANDALONE_RUNTIME_REQUIREMENTS,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

_READY_TIMEOUT_SECONDS = 30.0
_READY_POLL_INTERVAL_SECONDS = 0.5
_STOP_JOIN_TIMEOUT_SECONDS = 10.0
_IMPORT_DEADLOCK_RETRY_DELAYS_SECONDS = (0.25, 0.5, 1.0, 2.0)
_MODULE_LOCK_DEADLOCK_TEXT = "deadlock detected by _ModuleLock"
_PEER_TASK_REGISTRATION_TIMEOUT_SECONDS = 30.0
_PEER_TASK_POLL_INTERVAL_SECONDS = 0.1


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
        self._pip_spec = STANDALONE_FASTMCP_SPEC
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
            # MCP uses stateless streamable HTTP. Disabling Uvicorn's WebSocket
            # protocol keeps the embedded server from importing HA-owned
            # websockets solely while loading its HTTP listener configuration.
            ws="none",
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

    async def _async_wait_for_ha_mcp_owner(self) -> bool:
        """Wait for an enabled HA-MCP server entry to initialize its runtime."""
        peer_entries = [
            entry
            for entry in self._hass.config_entries.async_entries(HA_MCP_DOMAIN)
            if entry.data.get(HA_MCP_ENTRY_TYPE_KEY) == HA_MCP_SERVER_ENTRY_TYPE
            and getattr(entry, "disabled_by", None) is None
        ]
        if not peer_entries:
            return False

        loop = asyncio.get_running_loop()
        deadline = loop.time() + _PEER_TASK_REGISTRATION_TIMEOUT_SECONDS
        peer_task = None
        while peer_task is None:
            peer_data = self._hass.data.get(HA_MCP_DOMAIN, {})
            if isinstance(peer_data, dict):
                peer_task = peer_data.get(HA_MCP_BRINGUP_TASK_KEY)
            if peer_task is not None:
                break
            if loop.time() >= deadline:
                raise EmbeddedServerError(
                    "HA-MCP has an enabled server entry but did not register its "
                    "runtime bring-up task. Reload both MCP integrations.",
                    kind="package",
                )
            await asyncio.sleep(_PEER_TASK_POLL_INTERVAL_SECONDS)

        try:
            await asyncio.shield(peer_task)
        except asyncio.CancelledError as err:
            if not peer_task.cancelled():
                raise
            raise EmbeddedServerError(
                "HA-MCP's runtime bring-up task was cancelled. Reload HA-MCP "
                "before starting ESPHome MCP.",
                kind="package",
            ) from err
        except Exception as err:
            raise EmbeddedServerError(
                f"HA-MCP's runtime bring-up task failed: {err}",
                kind="package",
            ) from err

        peer_data = self._hass.data.get(HA_MCP_DOMAIN, {})
        peer_manager = peer_data.get(HA_MCP_MANAGER_KEY) if isinstance(peer_data, dict) else None
        if not bool(getattr(peer_manager, "is_running", False)):
            raise EmbeddedServerError(
                "HA-MCP finished bring-up but did not start its runtime. Resolve "
                "HA-MCP's repair issue before starting ESPHome MCP.",
                kind="package",
            )
        return True

    async def _async_ensure_package(self) -> None:
        """Adopt a peer FastMCP runtime or prepare a bounded standalone one."""
        from homeassistant.requirements import (
            RequirementsNotFound,
            async_process_requirements,
        )

        peer_entry_owner = await self._async_wait_for_ha_mcp_owner()
        peer_specs = await self._hass.async_add_executor_job(_installed_peer_fastmcp_specs)
        peer_owner = _select_peer_fastmcp_spec(peer_specs)
        installed_version = await self._hass.async_add_executor_job(_installed_fastmcp_version)
        importable = await self._hass.async_add_executor_job(_server_dependencies_importable)
        shared_runtime_loaded = await self._hass.async_add_executor_job(_fastmcp_runtime_loaded)

        if peer_entry_owner and peer_owner is None:
            raise EmbeddedServerError(
                "HA-MCP started its server entry but no ha-mcp distribution owns "
                "the FastMCP runtime. Resolve HA-MCP's package repair issue.",
                kind="package",
            )

        if peer_owner is not None:
            distribution, peer_spec = peer_owner
            if installed_version is None or not importable:
                raise EmbeddedServerError(
                    f"{distribution} owns {peer_spec}, but its FastMCP runtime is "
                    "not installed and importable. Resolve HA-MCP's package repair "
                    "issue before starting ESPHome MCP.",
                    kind="package",
                )
            if not _version_satisfies_requirement(installed_version, STANDALONE_FASTMCP_SPEC):
                raise EmbeddedServerError(
                    f"{distribution} installed FastMCP {installed_version}, which is "
                    "outside ESPHome MCP's supported range "
                    f"{STANDALONE_FASTMCP_SPEC}. Update both MCP integrations, then "
                    "restart Home Assistant.",
                    kind="restart",
                )
            if not _version_satisfies_requirement(installed_version, peer_spec):
                raise EmbeddedServerError(
                    f"Installed FastMCP {installed_version} does not satisfy "
                    f"{distribution} requirement {peer_spec}. Resolve the torn "
                    "HA-MCP install, then restart Home Assistant.",
                    kind="restart",
                )
            self._pip_spec = peer_spec
            self._store_effective_pip_spec()
            return

        self._pip_spec = STANDALONE_FASTMCP_SPEC
        if importable and _version_satisfies_requirement(
            installed_version, STANDALONE_FASTMCP_SPEC
        ):
            self._store_effective_pip_spec()
            return

        if shared_runtime_loaded:
            loaded_version = installed_version or "an unknown version"
            raise EmbeddedServerError(
                f"FastMCP {loaded_version} is already loaded by Home Assistant, "
                f"but ESPHome MCP requires {STANDALONE_FASTMCP_SPEC}. Refusing to "
                "replace the shared runtime inside a running process. Update the "
                "MCP integrations, then restart Home Assistant.",
                kind="restart",
            )

        try:
            await async_process_requirements(
                self._hass,
                f"ESPHome MCP server ({self._pip_spec})",
                list(STANDALONE_RUNTIME_REQUIREMENTS),
                is_built_in=False,
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
        if not _version_satisfies_requirement(installed_version, self._pip_spec):
            raise EmbeddedServerError(
                f"Installed FastMCP version {installed_version or 'unknown'} does "
                f"not satisfy {self._pip_spec}.",
                kind="package",
            )
        self._store_effective_pip_spec()

    def _store_effective_pip_spec(self) -> None:
        """Persist the requirement that owns the runtime used by this entry."""
        if self._entry.data.get(DATA_LAST_PIP_SPEC) == self._pip_spec:
            return
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


def _declared_fastmcp_spec(requirements: list[str]) -> str | None:
    """Return one active, marker-free FastMCP requirement."""
    for requirement in requirements:
        try:
            parsed = Requirement(requirement)
        except InvalidRequirement:
            continue
        if parsed.marker is not None and not parsed.marker.evaluate({"extra": ""}):
            continue
        if canonicalize_name(parsed.name) == "fastmcp":
            return requirement.partition(";")[0].strip()
    return None


def _installed_peer_fastmcp_specs() -> dict[str, str | None]:
    """Return FastMCP requirements declared by installed HA-MCP distributions."""
    peer_specs: dict[str, str | None] = {}
    for distribution in ("ha-mcp", "ha-mcp-dev"):
        try:
            requirements = metadata.requires(distribution) or []
        except metadata.PackageNotFoundError:
            continue
        peer_specs[distribution] = _declared_fastmcp_spec(requirements)
    return peer_specs


def _select_peer_fastmcp_spec(
    peer_specs: dict[str, str | None],
) -> tuple[str, str] | None:
    """Return the sole installed peer's FastMCP requirement."""
    if len(peer_specs) > 1:
        raise EmbeddedServerError(
            "Both ha-mcp and ha-mcp-dev are installed, so FastMCP runtime "
            "ownership is ambiguous. Reinstall the selected HA-MCP channel.",
            kind="package",
        )
    if not peer_specs:
        return None
    distribution, spec = next(iter(peer_specs.items()))
    if spec is None:
        raise EmbeddedServerError(
            f"Installed {distribution} does not declare a FastMCP requirement. "
            "Update or reinstall HA-MCP before starting ESPHome MCP.",
            kind="package",
        )
    return distribution, spec


def _version_satisfies_requirement(version: str | None, requirement: str) -> bool:
    """Return whether a version satisfies one bounded FastMCP requirement."""
    if version is None:
        return False
    try:
        parsed_requirement = Requirement(requirement)
        parsed_version = Version(version)
    except (InvalidRequirement, InvalidVersion):
        return False
    return (
        canonicalize_name(parsed_requirement.name) == "fastmcp"
        and parsed_requirement.url is None
        and parsed_requirement.marker is None
        and bool(parsed_requirement.specifier)
        and parsed_requirement.specifier.contains(parsed_version, prereleases=True)
    )


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
