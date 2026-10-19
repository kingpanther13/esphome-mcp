"""LocalProvider for locally-defined MCP components.

This module provides the `LocalProvider` class that manages tools, resources,
templates, and prompts registered via decorators or direct methods.
"""

from custom_components.esphome_mcp._vendor.fastmcp.server.providers.local_provider.local_provider import (
    LocalProvider,
)

__all__ = ["LocalProvider"]
