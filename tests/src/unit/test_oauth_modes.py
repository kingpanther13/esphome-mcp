"""Tests for none-mode and HA-auth OAuth endpoint behavior."""

from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp
from multidict import MultiDict

from ._oauth_stubs import install

install()

from custom_components.esphome_mcp.const import (  # noqa: E402
    DATA_WEBHOOK,
    DOMAIN,
    OAUTH_BASE,
)
from custom_components.esphome_mcp.mcp_webhook import (  # noqa: E402
    ResourceServer,
    _async_handle_webhook,
    _AuthorizationServerMetadataView,
    _ProtectedResourceMetadataView,
)
from custom_components.esphome_mcp.oauth_autoapprove import (  # noqa: E402
    CFG_AUTOAPPROVE_PROVIDER,
    AutoApproveAuthorizeView,
    AutoApproveProvider,
    AutoApproveRevokeView,
    AutoApproveTokenView,
)
from custom_components.esphome_mcp.oauth_common import _b64url_encode  # noqa: E402
from custom_components.esphome_mcp.oauth_dcr import CFG_DCR_SIGNING_KEY  # noqa: E402

KEY = b"k" * 32


class _Request:
    def __init__(
        self,
        *,
        query: dict[str, str] | MultiDict[str] | None = None,
        form: dict[str, str] | None = None,
        authorization: str | None = None,
        body: bytes = b"",
        method: str = "POST",
    ) -> None:
        self.query = MultiDict(query or {})
        self._form = MultiDict(form or {})
        self.headers = {"Host": "ha.example"}
        if authorization is not None:
            self.headers["Authorization"] = authorization
        self.scheme = "https"
        self._body = body
        self.method = method

    async def post(self) -> MultiDict[str]:
        return self._form

    async def read(self) -> bytes:
        return self._body


class _FailingRequestContext:
    async def __aenter__(self) -> None:
        raise aiohttp.ClientConnectionError("upstream unavailable")

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _FailingSession:
    def request(self, **_kwargs: Any) -> _FailingRequestContext:
        return _FailingRequestContext()


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _none_hass() -> SimpleNamespace:
    cfg = {
        "webhook_id": "secret-webhook",
        "resource_server": None,
        CFG_AUTOAPPROVE_PROVIDER: AutoApproveProvider(),
        CFG_DCR_SIGNING_KEY: KEY,
    }
    return SimpleNamespace(data={DOMAIN: {DATA_WEBHOOK: cfg}})


def _ha_auth_hass() -> SimpleNamespace:
    hass = SimpleNamespace(data={}, auth=SimpleNamespace())
    cfg = {
        "webhook_id": "public-webhook",
        "resource_server": ResourceServer(hass, "public-webhook"),
        CFG_AUTOAPPROVE_PROVIDER: None,
        CFG_DCR_SIGNING_KEY: KEY,
        "cimd_session": None,
    }
    hass.data = {DOMAIN: {DATA_WEBHOOK: cfg}}
    return hass


def test_none_mode_discovery_advertises_scoped_public_pkce_endpoints() -> None:
    hass = _none_hass()
    response = _run(_AuthorizationServerMetadataView(hass).get(_Request()))
    body = json.loads(response.text)

    assert response.status == 200
    assert body["issuer"] == f"https://ha.example{OAUTH_BASE}"
    assert body["authorization_endpoint"].endswith(f"{OAUTH_BASE}/authorize")
    assert body["token_endpoint"].endswith(f"{OAUTH_BASE}/token")
    assert body["registration_endpoint"].endswith(f"{OAUTH_BASE}/register")
    assert body["grant_types_supported"] == ["authorization_code"]
    assert body["token_endpoint_auth_methods_supported"] == ["none"]


def test_none_mode_fixed_metadata_path_does_not_leak_the_secret_webhook_id() -> None:
    response = _run(_ProtectedResourceMetadataView(_none_hass()).get(_Request()))

    assert response.status == 404
    assert "secret-webhook" not in response.text


def test_none_mode_authorize_and_token_complete_without_login() -> None:
    hass = _none_hass()
    verifier = "v" * 43
    challenge = _b64url_encode(hashlib.sha256(verifier.encode()).digest())
    redirect_uri = "https://client.example/callback"
    authorize = _run(
        AutoApproveAuthorizeView(hass).get(
            _Request(
                query={
                    "response_type": "code",
                    "client_id": "client",
                    "redirect_uri": redirect_uri,
                    "state": "state-1",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            )
        )
    )
    redirected = urlparse(authorize.headers["Location"])
    code = parse_qs(redirected.query)["code"][0]

    token = _run(
        AutoApproveTokenView(hass).post(
            _Request(
                form={
                    "grant_type": "authorization_code",
                    "client_id": "client",
                    "redirect_uri": redirect_uri,
                    "code": code,
                    "code_verifier": verifier,
                }
            )
        )
    )
    body = json.loads(token.text)

    assert authorize.status == 302
    assert parse_qs(redirected.query)["state"] == ["state-1"]
    assert parse_qs(redirected.query)["iss"] == [f"https://ha.example{OAUTH_BASE}"]
    assert token.status == 200
    assert body["token_type"] == "Bearer"
    assert body["access_token"]
    assert token.headers["Cache-Control"] == "no-store"


def test_ha_auth_discovery_advertises_scoped_refresh_and_revocation() -> None:
    hass = _ha_auth_hass()
    response = _run(_AuthorizationServerMetadataView(hass).get(_Request()))
    body = json.loads(response.text)

    assert body["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert body["authorization_endpoint"].endswith(f"{OAUTH_BASE}/authorize")
    assert body["token_endpoint"].endswith(f"{OAUTH_BASE}/token")
    assert body["registration_endpoint"].endswith(f"{OAUTH_BASE}/register")
    assert body["revocation_endpoint"].endswith(f"{OAUTH_BASE}/revoke")
    assert body["client_id_metadata_document_supported"] is True


def test_ha_auth_authorize_and_untranslated_token_use_relative_core_hops() -> None:
    hass = _ha_auth_hass()
    client_id = "https://client.example/oauth/metadata.json"
    redirect_uri = "https://client.example/callback"
    authorize = _run(
        AutoApproveAuthorizeView(hass).get(
            _Request(
                query={
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "code_challenge": "x" * 43,
                    "code_challenge_method": "S256",
                }
            )
        )
    )
    token = _run(
        AutoApproveTokenView(hass).post(
            _Request(
                form={
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "code": "core-code",
                    "code_verifier": "v" * 43,
                }
            )
        )
    )

    assert authorize.status == 302
    assert authorize.headers["Location"].startswith("/auth/authorize?")
    assert token.status == 307
    assert token.headers["Location"] == "/auth/token"
    assert token.headers["Cache-Control"] == "no-store"


def test_scoped_revoke_is_inactive_in_none_mode() -> None:
    response = _run(AutoApproveRevokeView(_none_hass()).post(_Request(form={})))

    assert response.status == 404


def test_resource_server_requires_an_active_human_admin() -> None:
    hass = _ha_auth_hass()
    provider = hass.data[DOMAIN][DATA_WEBHOOK]["resource_server"]

    def result(*, is_admin: bool, is_active: bool = True) -> Any:
        user = SimpleNamespace(
            is_admin=is_admin,
            is_active=is_active,
            system_generated=False,
        )
        return SimpleNamespace(user=user)

    hass.auth.async_validate_access_token = lambda _token: result(is_admin=True)
    assert _run(provider.validate_request(_Request(authorization="Bearer valid")))

    hass.auth.async_validate_access_token = lambda _token: result(is_admin=False)
    assert not _run(provider.validate_request(_Request(authorization="Bearer valid")))


def test_webhook_unavailability_responses_preserve_live_e2e_retry_prefix() -> None:
    not_ready = _run(_async_handle_webhook(SimpleNamespace(data={}), "webhook", _Request()))
    unavailable_hass = SimpleNamespace(
        data={
            DOMAIN: {
                DATA_WEBHOOK: {
                    "target_url": "http://127.0.0.1:9590/private",
                    "session": _FailingSession(),
                    "resource_server": None,
                }
            }
        }
    )
    upstream_down = _run(_async_handle_webhook(unavailable_hass, "webhook", _Request()))

    assert not_ready.status == 503
    assert not_ready.text.startswith("ESPHome MCP server")
    assert upstream_down.status == 502
    assert upstream_down.text.startswith("ESPHome MCP server")
