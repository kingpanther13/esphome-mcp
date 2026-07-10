"""Config-entry wiring for the in-process ESPHome MCP server."""

from __future__ import annotations

import asyncio
import secrets
from contextlib import suppress
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant

from .const import (
    DATA_BRINGUP_TASK,
    DATA_LAST_OPTIONS,
    DATA_SECRET_PATH,
    DATA_WEBHOOK_ID,
    DOMAIN,
    OPT_REGENERATE_SECRETS,
    OPT_SECRET_PATH_OVERRIDE,
    OPT_WEBHOOK_ID_OVERRIDE,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry


async def async_setup_server_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the server entry and schedule background bring-up."""
    from .embedded_setup import async_bring_up_server

    _ensure_secrets(hass, entry)

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


def _ensure_secrets(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Generate and persist stable webhook and direct-access secrets."""
    data = dict(entry.data)
    options = dict(entry.options)
    changed = False

    if options.get(OPT_REGENERATE_SECRETS):
        data[DATA_WEBHOOK_ID] = f"esp_mcp_{secrets.token_hex(16)}"
        data[DATA_SECRET_PATH] = f"/private_{secrets.token_urlsafe(16)}"
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
    if changed:
        hass.config_entries.async_update_entry(entry, data=data)
