"""Tests for HA-auth client identity translation and refresh envelopes."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ._oauth_stubs import install

install()

from custom_components.esphome_mcp import oauth_ha_auth  # noqa: E402
from custom_components.esphome_mcp.oauth_dcr import mint_client_id  # noqa: E402
from custom_components.esphome_mcp.oauth_ha_auth import (  # noqa: E402
    EnvelopeState,
    core_token_for_revocation,
    origin_client_id,
    redirect_matches,
    resolve_forward_client_id,
    rewrite_token_response_body,
    unwrap_refresh_token,
    wrap_refresh_token,
)

KEY = b"k" * 32
OTHER_KEY = b"x" * 32


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_redirect_matching_is_exact_except_for_loopback_ports() -> None:
    assert redirect_matches(
        ["https://client.example/callback"],
        "https://client.example/callback",
    )
    assert not redirect_matches(
        ["https://client.example/callback"],
        "https://client.example/other",
    )
    assert redirect_matches(
        ["http://localhost/callback"],
        "http://localhost:43123/callback",
    )
    assert origin_client_id("http://localhost:43123/callback") == (
        "http://localhost:43123"
    )


def test_signed_dcr_client_translates_to_the_validated_redirect_origin() -> None:
    client_id = mint_client_id(KEY, ["https://callback.example/oauth"])

    translated = _run(
        resolve_forward_client_id(
            session=None,
            dcr_key=KEY,
            client_id=client_id,
            redirect_uri="https://callback.example/oauth",
        )
    )

    assert translated == "https://callback.example"


def test_unregistered_dcr_redirect_is_left_for_core_to_reject() -> None:
    client_id = mint_client_id(KEY, ["https://callback.example/oauth"])

    translated = _run(
        resolve_forward_client_id(
            session=None,
            dcr_key=KEY,
            client_id=client_id,
            redirect_uri="https://attacker.example/oauth",
        )
    )

    assert translated == client_id


def test_refresh_envelope_is_component_scoped_and_presenter_bound() -> None:
    envelope = wrap_refresh_token(
        KEY,
        "core-refresh-token",
        "https://callback.example",
        "espmcp-dcr-presented",
    )

    assert envelope.startswith("espmcp-rt-")
    assert unwrap_refresh_token(
        KEY, envelope, "espmcp-dcr-presented"
    ) == ("core-refresh-token", "https://callback.example")
    assert (
        unwrap_refresh_token(KEY, envelope, "another-client")
        is EnvelopeState.INVALID
    )
    assert unwrap_refresh_token(OTHER_KEY, envelope, None) is EnvelopeState.INVALID


def test_forwarded_core_response_wraps_its_refresh_token() -> None:
    body = rewrite_token_response_body(
        KEY,
        json.dumps(
            {
                "access_token": "access",
                "refresh_token": "core-refresh",
                "token_type": "Bearer",
            }
        ).encode(),
        "https://callback.example",
        "presented-client",
    )
    wrapped = json.loads(body)["refresh_token"]

    assert unwrap_refresh_token(KEY, wrapped, "presented-client") == (
        "core-refresh",
        "https://callback.example",
    )


def test_revocation_recovers_a_token_after_signing_key_rotation() -> None:
    envelope = wrap_refresh_token(
        KEY,
        "core-refresh-token",
        "https://callback.example",
        "presented-client",
    )

    assert core_token_for_revocation(OTHER_KEY, envelope) == "core-refresh-token"


def test_cimd_document_requires_exact_client_id_and_safe_redirects() -> None:
    client_id = "https://client.example/oauth/metadata.json"
    valid = json.dumps(
        {
            "client_id": client_id,
            "redirect_uris": ["https://callback.example/oauth"],
        }
    ).encode()
    mismatch = json.dumps(
        {
            "client_id": "https://other.example/oauth/metadata.json",
            "redirect_uris": ["https://callback.example/oauth"],
        }
    ).encode()

    assert oauth_ha_auth._parse_cimd(valid, client_id) == [
        "https://callback.example/oauth"
    ]
    assert oauth_ha_auth._parse_cimd(mismatch, client_id) is None
