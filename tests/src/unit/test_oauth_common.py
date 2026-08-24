"""Tests for OAuth helpers shared by none and ha_auth modes."""

from __future__ import annotations

import hashlib

from custom_components.esphome_mcp.oauth_common import (
    PKCECodeStore,
    _b64url_encode,
    _is_valid_redirect_uri,
)


def test_redirect_uri_floor_accepts_https_and_rfc8252_loopback() -> None:
    assert _is_valid_redirect_uri("https://client.example/callback")
    assert _is_valid_redirect_uri("http://localhost:43123/callback")
    assert _is_valid_redirect_uri("http://127.0.0.2:43123/callback")
    assert _is_valid_redirect_uri("http://[::1]:43123/callback")


def test_redirect_uri_floor_rejects_unsafe_or_malformed_targets() -> None:
    for value in (
        "",
        "http://client.example/callback",
        "https://client.example/callback#fragment",
        "https://client.example:999999/callback",
        "javascript:alert(1)",
    ):
        assert not _is_valid_redirect_uri(value)


def test_pkce_codes_are_bound_and_one_shot() -> None:
    verifier = "v" * 43
    challenge = _b64url_encode(hashlib.sha256(verifier.encode()).digest())
    store = PKCECodeStore()
    code = store.issue_code("https://client.example/callback", challenge)

    assert code is not None
    assert store.consume_code(code, "https://client.example/callback", verifier)
    assert not store.consume_code(code, "https://client.example/callback", verifier)


def test_well_formed_wrong_pkce_verifier_burns_the_code() -> None:
    verifier = "v" * 43
    challenge = _b64url_encode(hashlib.sha256(verifier.encode()).digest())
    store = PKCECodeStore()
    code = store.issue_code("https://client.example/callback", challenge)

    assert code is not None
    assert not store.consume_code(code, "https://client.example/callback", "x" * 43)
    assert not store.consume_code(code, "https://client.example/callback", verifier)
