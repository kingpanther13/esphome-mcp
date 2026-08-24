"""Config-entry wiring for the in-process ESPHome MCP server."""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import suppress
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant

from .const import (
    DATA_BRINGUP_TASK,
    DATA_DCR_SIGNING_KEY,
    DATA_LAST_OPTIONS,
    DATA_SECRET_PATH,
    DATA_WEBHOOK_ID,
    DOMAIN,
    OPT_ENABLE_WEBHOOK,
    OPT_REGENERATE_SECRETS,
    OPT_SECRET_PATH_OVERRIDE,
    OPT_WEBHOOK_AUTH,
    OPT_WEBHOOK_ID_OVERRIDE,
    WEBHOOK_AUTH_HA,
    WEBHOOK_AUTH_NONE,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)


async def async_setup_server_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the server entry and schedule background bring-up."""
    from .embedded_setup import async_bring_up_server

    _ensure_secrets(hass, entry)
    _prebind_oauth_views(hass, entry)

    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data[DATA_LAST_OPTIONS] = dict(entry.options)
    task = entry.async_create_background_task(
        hass, async_bring_up_server(hass, entry), f"{DOMAIN}_bring_up"
    )
    domain_data[DATA_BRINGUP_TASK] = task

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_server_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stop the server and ingress webhook."""
    from .embedded_setup import async_teardown_server

    domain_data = hass.data.get(DOMAIN, {})
    task = domain_data.pop(DATA_BRINGUP_TASK, None)
    if task is not None and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    await async_teardown_server(hass)
    domain_data.pop(DATA_LAST_OPTIONS, None)
    return True


async def async_remove_server_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle entry removal."""
    from .embedded_setup import async_remove_server

    await async_remove_server(hass, entry)


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry only when entry options change."""
    domain_data = hass.data.get(DOMAIN, {})
    if domain_data.get(DATA_LAST_OPTIONS) == dict(entry.options):
        return
    await hass.config_entries.async_reload(entry.entry_id)


def _prebind_oauth_views(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Bind OAuth routes before background server installation can race startup."""
    if not bool(entry.options.get(OPT_ENABLE_WEBHOOK, True)):
        return
    auth_mode = str(entry.options.get(OPT_WEBHOOK_AUTH, WEBHOOK_AUTH_NONE))
    if auth_mode not in (WEBHOOK_AUTH_NONE, WEBHOOK_AUTH_HA):
        return

    from .mcp_webhook import _register_metadata_views
    from .oauth_autoapprove import bind_autoapprove_views
    from .oauth_dcr import bind_dcr_view

    try:
        _register_metadata_views(hass)
        bind_autoapprove_views(hass)
        bind_dcr_view(hass)
    except Exception:
        if auth_mode == WEBHOOK_AUTH_HA:
            raise
        _LOGGER.exception(
            "Failed to prebind none-mode OAuth routes; continuing setup as "
            "a plain secret-URL proxy"
        )


def _ensure_secrets(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Generate and persist stable webhook and direct-access secrets."""
    data = dict(entry.data)
    options = dict(entry.options)
    changed = False

    if options.get(OPT_REGENERATE_SECRETS):
        data[DATA_WEBHOOK_ID] = f"esp_mcp_{secrets.token_hex(16)}"
        data[DATA_SECRET_PATH] = f"/private_{secrets.token_urlsafe(16)}"
        data[DATA_DCR_SIGNING_KEY] = secrets.token_bytes(32).hex()
        options[OPT_REGENERATE_SECRETS] = False
        options[OPT_WEBHOOK_ID_OVERRIDE] = ""
        options[OPT_SECRET_PATH_OVERRIDE] = ""
        hass.config_entries.async_update_entry(entry, data=data, options=options)
        return

    webhook_override = str(options.get(OPT_WEBHOOK_ID_OVERRIDE) or "").strip()
    if webhook_override and data.get(DATA_WEBHOOK_ID) != webhook_override:
        data[DATA_WEBHOOK_ID] = webhook_override
        changed = True

    path_override = str(options.get(OPT_SECRET_PATH_OVERRIDE) or "").strip()
    if path_override:
        if not path_override.startswith("/"):
            path_override = f"/{path_override}"
        if data.get(DATA_SECRET_PATH) != path_override:
            data[DATA_SECRET_PATH] = path_override
            changed = True

    if not data.get(DATA_WEBHOOK_ID):
        data[DATA_WEBHOOK_ID] = f"esp_mcp_{secrets.token_hex(16)}"
        changed = True
    if not data.get(DATA_SECRET_PATH):
        data[DATA_SECRET_PATH] = f"/private_{secrets.token_urlsafe(16)}"
        changed = True
    if not data.get(DATA_DCR_SIGNING_KEY):
        data[DATA_DCR_SIGNING_KEY] = secrets.token_bytes(32).hex()
        changed = True
    if changed:
        hass.config_entries.async_update_entry(entry, data=data)
