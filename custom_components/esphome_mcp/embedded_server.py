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
    HA_MCP_SERVER_ENTRY_TYPE,
    OPT_BIND_HOST,
    OPT_SERVER_PORT,
    SERVER_CONFIG_SUBDIR,
)
from .ha_mcp_runtime import (
    HA_MCP_COMPONENT_VERSION,
    HA_MCP_FASTMCP_REQUIREMENT,
    HA_MCP_MASTER_SHA,
    HA_MCP_RUNTIME_CONTRACT_ID,
    HA_MCP_SERVER_REQUIREMENTS,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

_READY_TIMEOUT_SECONDS = 30.0
_READY_POLL_INTERVAL_SECONDS = 0.5
_STOP_JOIN_TIMEOUT_SECONDS = 10.0
_IMPORT_DEADLOCK_RETRY_DELAYS_SECONDS = (0.25, 0.5, 1.0, 2.0)
_MODULE_LOCK_DEADLOCK_TEXT = "deadlock detected by _ModuleLock"
_HA_MCP_DISTRIBUTIONS = ("ha-mcp", "ha-mcp-dev")


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
        self._pip_spec = HA_MCP_FASTMCP_REQUIREMENT
        self._config_dir = hass.config.path(SERVER_CONFIG_SUBDIR)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._thread_exc: BaseException | None = None
        self._fastmcp_version: str | None = None

    @property
    def port(self) -> int:
        """TCP port the server listens on."""
        return self._port

    @property
    def is_running(self) -> bool:
        """Return True while the worker thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def fastmcp_version(self) -> str | None:
        """Return the resolved FastMCP generation used by this manager."""
        return self._fastmcp_version

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

    async def _async_validate_ha_mcp_component(self) -> bool:
        """Validate a configured HA-MCP component and report server ownership."""
        peer_entries = [
            entry
            for entry in self._hass.config_entries.async_entries(HA_MCP_DOMAIN)
            if getattr(entry, "disabled_by", None) is None
        ]
        if not peer_entries:
            return False

        from homeassistant.loader import async_get_integration

        try:
            integration = await async_get_integration(self._hass, HA_MCP_DOMAIN)
        except Exception as err:
            raise EmbeddedServerError(
                f"Could not read the configured HA-MCP component version: {err}",
                kind="package",
            ) from err
        installed_version = getattr(integration, "version", None)
        if installed_version != HA_MCP_COMPONENT_VERSION:
            raise EmbeddedServerError(
                "The configured HA-MCP custom component is version "
                f"{installed_version or 'unknown'}, but ESPHome MCP mirrors "
                f"component {HA_MCP_COMPONENT_VERSION} from HA-MCP master "
                f"{HA_MCP_MASTER_SHA[:12]}. Update both custom components from "
                "their matching dependency PR, then restart Home Assistant.",
                kind="package",
            )
        return any(
            entry.data.get(HA_MCP_ENTRY_TYPE_KEY) == HA_MCP_SERVER_ENTRY_TYPE
            for entry in peer_entries
        )

    async def _async_wait_for_ha_mcp_install(self) -> None:
        """Wait read-only for an enabled HA-MCP server's package bring-up."""
        domain_data = self._hass.data.get(HA_MCP_DOMAIN, {})
        bringup_task = (
            domain_data.get(HA_MCP_BRINGUP_TASK_KEY) if isinstance(domain_data, dict) else None
        )
        if bringup_task is None:
            raise EmbeddedServerError(
                "HA-MCP has an enabled server entry but has not published its "
                "package bring-up task. Let HA-MCP finish setup, then reload "
                "ESPHome MCP.",
                kind="package",
            )
        try:
            await asyncio.shield(bringup_task)
        except asyncio.CancelledError as err:
            if not bringup_task.cancelled():
                raise
            raise EmbeddedServerError(
                "HA-MCP's package bring-up was cancelled. Resolve its repair "
                "issue, then reload ESPHome MCP.",
                kind="package",
            ) from err
        except Exception as err:
            raise EmbeddedServerError(
                f"HA-MCP's package bring-up failed: {err}",
                kind="package",
            ) from err

    async def _async_ensure_package(self) -> None:
        """Reuse or install the exact dependency contract mirrored from HA-MCP."""
        from homeassistant.requirements import (
            RequirementsNotFound,
            async_process_requirements,
        )

        peer_server_enabled = await self._async_validate_ha_mcp_component()
        if peer_server_enabled:
            await self._async_wait_for_ha_mcp_install()
        peer_requirements = await self._hass.async_add_executor_job(_installed_ha_mcp_requirements)
        if peer_server_enabled and not peer_requirements:
            raise EmbeddedServerError(
                "HA-MCP finished package bring-up but no ha-mcp distribution "
                "metadata is installed. Resolve HA-MCP's package repair issue "
                "before reloading ESPHome MCP.",
                kind="package",
            )
        _validate_installed_ha_mcp_contract(peer_requirements)

        installed_version = await self._hass.async_add_executor_job(_installed_fastmcp_version)
        importable = await self._hass.async_add_executor_job(_server_dependencies_importable)
        violations = await self._hass.async_add_executor_job(_unsatisfied_runtime_requirements)
        shared_runtime_loaded = await self._hass.async_add_executor_job(_fastmcp_runtime_loaded)
        if shared_runtime_loaded:
            loaded_fingerprint = await self._hass.async_add_executor_job(
                _loaded_fastmcp_fingerprint
            )
            installed_origin = await self._hass.async_add_executor_job(_installed_fastmcp_origin)
            if not _loaded_fastmcp_matches_install(
                loaded_fingerprint,
                installed_version,
                installed_origin,
            ):
                loaded_version, loaded_origin = loaded_fingerprint or (None, None)
                raise EmbeddedServerError(
                    "The loaded FastMCP "
                    f"{loaded_version or 'version is unknown'} from "
                    f"{loaded_origin or 'an unknown location'} does not match "
                    "installed package metadata (installed FastMCP "
                    f"{installed_version or 'unknown'} at "
                    f"{installed_origin or 'an unknown location'}). Refusing to "
                    "mix process-global runtime generations; restart Home "
                    "Assistant before starting ESPHome MCP.",
                    kind="restart",
                )

        if not violations and importable:
            self._fastmcp_version = installed_version
            self._store_effective_pip_spec()
            return

        if shared_runtime_loaded:
            raise EmbeddedServerError(
                "FastMCP is already loaded by Home Assistant, but the HA-MCP "
                "master dependency contract is not satisfied: "
                f"{'; '.join(violations) if violations else 'runtime modules are missing'}. "
                "Refusing to replace process-global packages; apply the matching "
                "dependency update and restart Home Assistant.",
                kind="restart",
            )

        if peer_server_enabled:
            detail = "; ".join(violations) if violations else "runtime modules are missing"
            raise EmbeddedServerError(
                "HA-MCP owns the enabled shared runtime, but its dependency "
                f"graph is not usable: {detail}. ESPHome MCP will not invoke "
                "pip in the peer-owned path; resolve HA-MCP's repair issue, "
                "then reload ESPHome MCP.",
                kind="package",
            )

        try:
            await async_process_requirements(
                self._hass,
                f"ESPHome MCP server ({HA_MCP_RUNTIME_CONTRACT_ID})",
                list(HA_MCP_SERVER_REQUIREMENTS),
                is_built_in=False,
            )
        except RequirementsNotFound as err:
            raise EmbeddedServerError(
                "Could not install the HA-MCP master dependency contract "
                f"({HA_MCP_MASTER_SHA[:12]}): {err}",
                kind="package",
            ) from err

        importable = await self._hass.async_add_executor_job(_server_dependencies_importable)
        violations = await self._hass.async_add_executor_job(_unsatisfied_runtime_requirements)
        if violations or not importable:
            detail = "; ".join(violations) if violations else "runtime modules are missing"
            raise EmbeddedServerError(
                "Installed the HA-MCP master dependency contract, but it is still "
                f"not usable: {detail}.",
                kind="package",
            )
        installed_version = await self._hass.async_add_executor_job(_installed_fastmcp_version)
        self._fastmcp_version = installed_version
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


def _installed_ha_mcp_requirements() -> dict[str, tuple[str, ...]]:
    """Return direct requirements declared by installed HA-MCP distributions."""
    installed: dict[str, tuple[str, ...]] = {}
    for distribution in _HA_MCP_DISTRIBUTIONS:
        try:
            requirements = metadata.requires(distribution)
        except metadata.PackageNotFoundError:
            continue
        installed[distribution] = tuple(requirements or ())
    return installed


def _normalized_requirement(raw: str) -> tuple[str, tuple[str, ...], str, str, str]:
    """Return a stable comparison key for one PEP 508 requirement."""
    requirement = Requirement(raw)
    return (
        canonicalize_name(requirement.name),
        tuple(sorted(requirement.extras)),
        str(requirement.specifier),
        requirement.url or "",
        str(requirement.marker) if requirement.marker is not None else "",
    )


def _requirement_map(requirements: tuple[str, ...]) -> dict[tuple[object, ...], str]:
    """Map normalized requirements to their readable form."""
    normalized: dict[tuple[object, ...], str] = {}
    for raw in requirements:
        try:
            key = _normalized_requirement(raw)
        except InvalidRequirement as err:
            raise EmbeddedServerError(
                f"Invalid requirement metadata {raw!r}: {err}",
                kind="package",
            ) from err
        if key in normalized:
            raise EmbeddedServerError(
                f"Duplicate requirement metadata for {raw!r}",
                kind="package",
            )
        normalized[key] = str(Requirement(raw))
    return normalized


def _validate_installed_ha_mcp_contract(
    installed: dict[str, tuple[str, ...]],
) -> None:
    """Fail closed when an installed HA-MCP server declares another contract."""
    if len(installed) > 1:
        raise EmbeddedServerError(
            "Both ha-mcp and ha-mcp-dev are installed. They share one import "
            "package, so select one HA-MCP channel before starting ESPHome MCP.",
            kind="package",
        )
    if not installed:
        return

    distribution, requirements = next(iter(installed.items()))
    expected = _requirement_map(HA_MCP_SERVER_REQUIREMENTS)
    actual = _requirement_map(requirements)
    if expected == actual:
        return

    missing = sorted(expected[key] for key in expected.keys() - actual.keys())
    unexpected = sorted(actual[key] for key in actual.keys() - expected.keys())
    differences = []
    if missing:
        differences.append(f"missing {', '.join(missing)}")
    if unexpected:
        differences.append(f"unexpected {', '.join(unexpected)}")
    raise EmbeddedServerError(
        f"Installed {distribution} does not match HA-MCP master "
        f"{HA_MCP_MASTER_SHA[:12]} ({'; '.join(differences)}). Update the "
        "HA-MCP server and both custom components from their matching dependency "
        "updates, then restart Home Assistant.",
        kind="package",
    )


def _unsatisfied_runtime_requirements() -> tuple[str, ...]:
    """Audit the mirrored runtime graph, including requested dependency extras."""
    queue = []
    for raw in HA_MCP_SERVER_REQUIREMENTS:
        requirement = Requirement(raw)
        if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
            queue.append((requirement, "HA-MCP master contract"))
    seen: set[tuple[str, tuple[str, ...], str, str, str]] = set()
    violations: list[str] = []

    while queue:
        requirement, required_by = queue.pop(0)
        key = _normalized_requirement(str(requirement))
        if key in seen:
            continue
        seen.add(key)
        name = canonicalize_name(requirement.name)
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            violations.append(f"{name} is missing (required by {required_by})")
            continue

        installed_version = distribution.version
        try:
            parsed_version = Version(installed_version)
        except InvalidVersion:
            violations.append(
                f"{name} has invalid version {installed_version!r} (required by {required_by})"
            )
            continue
        if requirement.specifier and not requirement.specifier.contains(
            parsed_version, prereleases=True
        ):
            violations.append(
                f"{name} {installed_version} does not satisfy {requirement} "
                f"(required by {required_by})"
            )

        marker_extras = {"", *requirement.extras}
        for child_raw in distribution.requires or ():
            try:
                child = Requirement(child_raw)
            except InvalidRequirement:
                violations.append(
                    f"{name} {installed_version} declares invalid requirement {child_raw!r}"
                )
                continue
            if child.marker is not None and not any(
                child.marker.evaluate({"extra": extra}) for extra in marker_extras
            ):
                continue
            queue.append((child, f"{name} {installed_version}"))

    return tuple(sorted(set(violations)))


def _loaded_fastmcp_fingerprint() -> tuple[str | None, str | None] | None:
    """Return the cached FastMCP generation without importing or reloading it."""
    if not _fastmcp_runtime_loaded():
        return None
    module = sys.modules.get("fastmcp")
    if module is None:
        return None, None
    version = getattr(module, "__version__", None)
    origin = getattr(module, "__file__", None)
    return (
        version if isinstance(version, str) and version else None,
        os.path.realpath(origin) if isinstance(origin, str) and origin else None,
    )


def _installed_fastmcp_origin() -> str | None:
    """Return the installed distribution path that owns fastmcp/__init__.py."""
    for distribution_name in ("fastmcp-slim", "fastmcp"):
        try:
            distribution = metadata.distribution(distribution_name)
        except metadata.PackageNotFoundError:
            continue
        for installed_file in distribution.files or ():
            normalized = str(installed_file).replace("\\", "/")
            if normalized == "fastmcp/__init__.py" or normalized.endswith("/fastmcp/__init__.py"):
                return os.path.realpath(distribution.locate_file(installed_file))
    return None


def _loaded_fastmcp_matches_install(
    loaded: tuple[str | None, str | None] | None,
    installed_version: str | None,
    installed_origin: str | None,
) -> bool:
    """Return whether cached FastMCP code matches current package metadata."""
    if loaded is None or installed_version is None or installed_origin is None:
        return False
    loaded_version, loaded_origin = loaded
    if loaded_version is None or loaded_origin is None:
        return False
    try:
        versions_match = Version(loaded_version) == Version(installed_version)
    except InvalidVersion:
        return False
    return versions_match and os.path.realpath(loaded_origin) == os.path.realpath(installed_origin)


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
