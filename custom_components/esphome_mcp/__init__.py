"""ESPHome MCP custom component."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .embedded_entry import (
    async_remove_server_entry,
    async_setup_server_entry,
    async_unload_server_entry,
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the ESPHome MCP server entry."""
    return await async_setup_server_entry(hass, entry)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the ESPHome MCP server entry."""
    return await async_unload_server_entry(hass, entry)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle removal of the ESPHome MCP server entry."""
    await async_remove_server_entry(hass, entry)
