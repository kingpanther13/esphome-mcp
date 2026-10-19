"""MCP Client module."""

from custom_components.esphome_mcp._vendor.mcp.client._input_required import InputRequiredRoundsExceededError
from custom_components.esphome_mcp._vendor.mcp.client._transport import Transport
from custom_components.esphome_mcp._vendor.mcp.client.caching import (
    CacheConfig,
    CacheEntry,
    CacheKey,
    CacheMode,
    InMemoryResponseCacheStore,
    ResponseCacheStore,
)
from custom_components.esphome_mcp._vendor.mcp.client.client import Client
from custom_components.esphome_mcp._vendor.mcp.client.context import ClientRequestContext
from custom_components.esphome_mcp._vendor.mcp.client.extension import (
    ClaimContext,
    ClientExtension,
    NotificationBinding,
    ResultClaim,
    UnexpectedClaimedResult,
    advertise,
)
from custom_components.esphome_mcp._vendor.mcp.client.session import ClientSession, IncomingMessage

__all__ = [
    "CacheConfig",
    "CacheEntry",
    "CacheKey",
    "CacheMode",
    "ClaimContext",
    "Client",
    "ClientExtension",
    "ClientRequestContext",
    "ClientSession",
    "IncomingMessage",
    "InMemoryResponseCacheStore",
    "InputRequiredRoundsExceededError",
    "NotificationBinding",
    "ResponseCacheStore",
    "ResultClaim",
    "Transport",
    "UnexpectedClaimedResult",
    "advertise",
]
