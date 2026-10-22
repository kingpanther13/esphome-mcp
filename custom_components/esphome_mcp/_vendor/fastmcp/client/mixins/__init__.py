"""Client mixins for FastMCP."""

from custom_components.esphome_mcp._vendor.fastmcp.client.mixins.prompts import ClientPromptsMixin
from custom_components.esphome_mcp._vendor.fastmcp.client.mixins.resources import ClientResourcesMixin
from custom_components.esphome_mcp._vendor.fastmcp.client.mixins.skills import ClientSkillsMixin
from custom_components.esphome_mcp._vendor.fastmcp.client.mixins.tools import ClientToolsMixin

__all__ = [
    "ClientPromptsMixin",
    "ClientResourcesMixin",
    "ClientSkillsMixin",
    "ClientToolsMixin",
]
