"""OAuth2 Authentication implementation for httpx2.

Implements authorization code flow with PKCE and automatic token refresh.
"""

from custom_components.esphome_mcp._vendor.mcp.client.auth.exceptions import OAuthFlowError, OAuthRegistrationError, OAuthTokenError
from custom_components.esphome_mcp._vendor.mcp.client.auth.oauth2 import (
    OAuthClientProvider,
    PKCEParameters,
    TokenStorage,
)
from custom_components.esphome_mcp._vendor.mcp.shared.auth import AuthorizationCodeResult

__all__ = [
    "AuthorizationCodeResult",
    "OAuthClientProvider",
    "OAuthFlowError",
    "OAuthRegistrationError",
    "OAuthTokenError",
    "PKCEParameters",
    "TokenStorage",
]
