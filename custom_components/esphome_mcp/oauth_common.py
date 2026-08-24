"""Shared OAuth protocol primitives for ESPHome MCP.

The component deliberately supports only the secret-URL ``none`` mode and
Home Assistant-backed ``ha_auth`` mode.  This module carries the small,
mode-neutral subset of HA-MCP's legacy provider that those two modes share:
redirect validation, base64url helpers, PKCE code lifecycle, issuer binding,
and safe anonymous form parsing.  Keeping those primitives here avoids
shipping a dormant legacy authorization server merely as a utility module.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import logging
import re
import secrets
import time
from typing import Any, TypedDict
from urllib.parse import urlparse

from aiohttp import web
from multidict import MultiDictProxy

_LOGGER = logging.getLogger(__name__)

ACCESS_TOKEN_TTL = 60 * 60
AUTH_CODE_TTL = 5 * 60

# RFC 6749 section 5.1: token responses must never be cached.
_TOKEN_RESPONSE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}

_LOOPBACK_HOSTNAMES = frozenset({"localhost"})

# RFC 7636 section 4.1.
PKCE_VERIFIER_MIN = 43
PKCE_VERIFIER_MAX = 128
_PKCE_VERIFIER_RE = re.compile(r"[A-Za-z0-9._~-]+")
_PKCE_CHALLENGE_RE = re.compile(r"[A-Za-z0-9_-]{43}")

MAX_PENDING_CODES = 1000


def _issuer_for(request: web.Request) -> str:
    """Return the issuer advertised for this request's public origin."""
    from .mcp_webhook import issuer_for_request

    return issuer_for_request(request)


def _b64url_encode(raw: bytes) -> str:
    """Encode bytes as unpadded base64url."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    """Decode padded or unpadded base64url."""
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


def _is_loopback_host(hostname: str) -> bool:
    """Return whether a host is an RFC 8252 loopback callback host."""
    if hostname in _LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


# RFC 3986 section 3.2 authority characters.  Rejecting everything else also
# keeps urlparse/yarl disagreements from escaping as anonymous-view 500s.
_AUTHORITY_CHARS_RE = re.compile(r"[A-Za-z0-9._~%!$&'()*+,;=:@\[\]-]*")


def _is_valid_redirect_uri(redirect_uri: str) -> bool:
    """Validate an HTTPS redirect or an RFC 8252 HTTP loopback redirect."""
    if not redirect_uri:
        return False
    try:
        parsed = urlparse(redirect_uri)
        _ = parsed.port
    except ValueError:
        return False
    if not parsed.hostname or not _AUTHORITY_CHARS_RE.fullmatch(parsed.netloc):
        return False
    if parsed.scheme == "http":
        if not _is_loopback_host(parsed.hostname):
            return False
    elif parsed.scheme != "https":
        return False
    return not parsed.fragment


class _PendingCode(TypedDict):
    """Stored PKCE authorization-code state."""

    redirect_uri: str
    code_challenge: str
    expires: float


class PKCECodeStore:
    """Short-lived, bounded, one-shot PKCE S256 authorization-code store."""

    def __init__(self) -> None:
        self._codes: dict[str, _PendingCode] = {}

    def issue_code(self, redirect_uri: str, code_challenge: str) -> str | None:
        """Issue a code, or return None when the pending-code cap is reached."""
        now = time.time()
        self._codes = {code: entry for code, entry in self._codes.items() if entry["expires"] > now}
        if len(self._codes) >= MAX_PENDING_CODES:
            _LOGGER.warning(
                "ESPHome MCP OAuth: pending-code store at cap (%d); refusing issuance",
                MAX_PENDING_CODES,
            )
            return None
        code = secrets.token_urlsafe(32)
        self._codes[code] = {
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "expires": now + AUTH_CODE_TTL,
        }
        return code

    def consume_code(self, code: str, redirect_uri: str, code_verifier: str) -> bool:
        """Consume a code once and verify its redirect URI and S256 challenge."""
        if not (PKCE_VERIFIER_MIN <= len(code_verifier) <= PKCE_VERIFIER_MAX):
            return False
        if not _PKCE_VERIFIER_RE.fullmatch(code_verifier):
            return False
        entry = self._codes.pop(code, None)
        if entry is None or entry["expires"] < time.time():
            return False
        if entry["redirect_uri"] != redirect_uri:
            return False
        derived = _b64url_encode(hashlib.sha256(code_verifier.encode()).digest())
        return hmac.compare_digest(derived.encode("ascii"), entry["code_challenge"].encode("ascii"))


async def read_form(request: web.Request) -> MultiDictProxy[Any] | None:
    """Parse a form body, returning None for invalid data or charset names."""
    try:
        return await request.post()
    except (ValueError, LookupError):
        return None
