"""FastMCP CLI package."""

try:
    from .cli import app
except ImportError as exc:
    from custom_components.esphome_mcp._vendor.fastmcp import _install_hints

    raise ImportError(_install_hints.CLI_SUPPORT) from exc
