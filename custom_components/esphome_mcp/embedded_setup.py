"""Bring the in-process ESPHome MCP server up and down."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import (
    BIND_HOST_ALL,
    DATA_MANAGER,
    DATA_SECRET_PATH,
    DATA_WEBHOOK_ID,
    DEFAULT_BIND_HOST,
    DEFAULT_SERVER_PORT,
    DOMAIN,
    ISSUE_PACKAGE_FAILED,
    ISSUE_START_FAILED,
    OPT_BIND_HOST,
    OPT_ENABLE_WEBHOOK,
    OPT_EXTERNAL_URL,
    OPT_SERVER_PORT,
    OPT_WEBHOOK_AUTH,
    WEBHOOK_AUTH_NONE,
)
from .embedded_server import EmbeddedServerError, EmbeddedServerManager
from .mcp_webhook import async_register_webhook, async_unregister_webhook

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)
_NOTIFICATION_ID = "esphome_mcp_server_connect"
_ISSUE_IDS = (ISSUE_PACKAGE_FAILED, ISSUE_START_FAILED)


async def async_bring_up_server(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Start the server and expose it through the HA webhook."""
    _clear_issues(hass)
    manager = EmbeddedServerManager(hass, entry)
    hass.data.setdefault(DOMAIN, {})[DATA_MANAGER] = manager

    try:
        await manager.async_start()

        auth_mode = str(entry.options.get(OPT_WEBHOOK_AUTH, WEBHOOK_AUTH_NONE))
        secret_path = str(entry.data[DATA_SECRET_PATH])
        webhook_enabled = bool(entry.options.get(OPT_ENABLE_WEBHOOK, True))
        if webhook_enabled:
            await async_register_webhook(
                hass,
                entry,
                port=manager.port,
                secret_path=secret_path,
                auth_mode=auth_mode,
            )
        else:
            _LOGGER.info("ESPHome MCP webhook access disabled; direct/panel access only")
        _surface_connect_urls(hass, entry, auth_mode, webhook_enabled=webhook_enabled)
    except asyncio.CancelledError:
        await async_teardown_server(hass)
        raise
    except EmbeddedServerError as err:
        _LOGGER.error("ESPHome MCP server failed to start: %s", err)
        with suppress(Exception):
            await async_teardown_server(hass)
        _create_issue(hass, err.kind, str(err))
    except Exception as err:
        _LOGGER.exception("ESPHome MCP server bring-up failed")
        with suppress(Exception):
            await async_teardown_server(hass)
        _create_issue(hass, "start", str(err))


async def async_teardown_server(hass: HomeAssistant) -> None:
    """Unregister the webhook and stop the server thread."""
    await async_unregister_webhook(hass)
    manager = hass.data.get(DOMAIN, {}).pop(DATA_MANAGER, None)
    if isinstance(manager, EmbeddedServerManager):
        await manager.async_stop()


async def async_remove_server(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clear repair issues on entry removal."""
    _clear_issues(hass)


def _surface_connect_urls(
    hass: HomeAssistant,
    entry: ConfigEntry,
    auth_mode: str,
    *,
    webhook_enabled: bool = True,
) -> None:
    """Log connect URLs and create an admin-safe notification."""
    from homeassistant.helpers.network import NoURLAvailableError, get_url

    webhook_id = entry.data[DATA_WEBHOOK_ID]
    urls: list[str] = []
    external = str(entry.options.get(OPT_EXTERNAL_URL) or "").rstrip("/")
    if not webhook_enabled:
        external = ""
        webhook_id = None
    if external:
        urls.append(f"{external}/api/webhook/{webhook_id}")

    try:
        from homeassistant.components.cloud import CloudNotAvailable, async_remote_ui_url

        try:
            if webhook_id:
                cloud_base = async_remote_ui_url(hass)
                urls.append(f"{cloud_base}/api/webhook/{webhook_id}")
        except CloudNotAvailable:
            pass
    except ImportError:
        pass

    try:
        if webhook_id:
            local_base = get_url(hass, allow_external=False, prefer_external=False)
            urls.append(f"{local_base}/api/webhook/{webhook_id}")
    except NoURLAvailableError:
        pass

    if not urls and webhook_id:
        urls.append(f"/api/webhook/{webhook_id} (prefix with your Home Assistant URL)")

    port = int(entry.options.get(OPT_SERVER_PORT, DEFAULT_SERVER_PORT))
    bind_host = str(entry.options.get(OPT_BIND_HOST, DEFAULT_BIND_HOST))
    auth_note = (
        "Webhook access is disabled (local-only mode)."
        if not webhook_enabled
        else "The webhook URL is the shared secret (no bearer required)."
        if auth_mode == WEBHOOK_AUTH_NONE
        else "Clients authenticate with a Home Assistant administrator account (ha_auth)."
    )

    if bind_host == BIND_HOST_ALL:
        urls.append(
            f"http://<home-assistant-ip>:{port}{entry.data[DATA_SECRET_PATH]} (direct access)"
        )

    url_lines = "\n".join(f"- {url}" for url in urls)
    _LOGGER.info("ESPHome MCP server is running. Connect URL(s):\n%s\n%s", url_lines, auth_note)

    message = (
        "The ESPHome MCP server is running inside Home Assistant.\n\n"
        "Manage it from the [ESPHome MCP panel](/esphome-mcp) in the sidebar.\n\n"
        "The connect URL is shown on the integration Configure screen and in the "
        "Home Assistant log. These surfaces are administrator-only because the "
        "secret URL is a credential in the default mode.\n\n"
        f"{auth_note}\n"
    )
    persistent_notification.async_create(
        hass,
        message,
        title="ESPHome MCP",
        notification_id=_NOTIFICATION_ID,
    )


_ISSUE_BY_KIND = {
    "package": ISSUE_PACKAGE_FAILED,
    "start": ISSUE_START_FAILED,
}


def _create_issue(hass: HomeAssistant, kind: str, detail: str) -> None:
    """File the repair issue matching the failure kind."""
    issue_id = _ISSUE_BY_KIND[kind]
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=issue_id,
        translation_placeholders={"detail": detail},
    )


def _clear_issues(hass: HomeAssistant) -> None:
    """Clear server-bring-up repair issues."""
    for issue_id in _ISSUE_IDS:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
