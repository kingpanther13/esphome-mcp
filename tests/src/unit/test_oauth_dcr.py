"""Tests for stateless OAuth dynamic client registration."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from ._oauth_stubs import install

install()

from custom_components.esphome_mcp import oauth_dcr  # noqa: E402
from custom_components.esphome_mcp.const import DATA_WEBHOOK, DOMAIN  # noqa: E402
from custom_components.esphome_mcp.oauth_dcr import (  # noqa: E402
    CFG_DCR_SIGNING_KEY,
    DcrRegisterView,
    client_redirect_uris,
    mint_client_id,
)

KEY = b"k" * 32
OTHER_KEY = b"x" * 32


class _Content:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    async def read(self, size: int) -> bytes:
        chunk, self._raw = self._raw[:size], self._raw[size:]
        return chunk


def _request(body: Any) -> SimpleNamespace:
    return _raw_request(json.dumps(body).encode())


def _raw_request(raw: bytes) -> SimpleNamespace:
    return SimpleNamespace(content=_Content(raw))


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _hass(*, key: bytes | None, ha_auth: bool = False) -> SimpleNamespace:
    cfg: dict[str, Any] = {}
    if key is not None:
        cfg[CFG_DCR_SIGNING_KEY] = key
    if ha_auth:
        cfg["resource_server"] = object()
    return SimpleNamespace(data={DOMAIN: {DATA_WEBHOOK: cfg}})


def test_mint_and_verify_round_trip_is_component_scoped() -> None:
    uris = ["https://client.example/callback"]
    client_id = mint_client_id(KEY, uris)

    assert client_id.startswith("espmcp-dcr-")
    assert client_redirect_uris(KEY, client_id) == uris
    assert client_redirect_uris(OTHER_KEY, client_id) is None


def test_normalized_origin_preserves_explicit_port_zero_and_ipv6() -> None:
    assert oauth_dcr.normalized_origin("https://client.example:0/callback") == (
        "https",
        "client.example",
        0,
    )
    ipv6 = oauth_dcr.normalized_origin("https://[2001:db8::1]:8443/callback")
    assert ipv6 == ("https", "2001:db8::1", 8443)
    assert oauth_dcr.canonical_origin_url(ipv6) == "https://[2001:db8::1]:8443"


def test_register_mints_a_none_mode_public_client() -> None:
    response = _run(
        DcrRegisterView(_hass(key=KEY)).post(
            _request(
                {
                    "redirect_uris": ["https://client.example/callback"],
                    "client_name": "ESPHome MCP test",
                }
            )
        )
    )
    body = json.loads(response.text)

    assert response.status == 201
    assert body["token_endpoint_auth_method"] == "none"
    assert body["grant_types"] == ["authorization_code"]
    assert client_redirect_uris(KEY, body["client_id"]) == ["https://client.example/callback"]


def test_register_ha_auth_advertises_refresh() -> None:
    response = _run(
        DcrRegisterView(_hass(key=KEY, ha_auth=True)).post(
            _request({"redirect_uris": ["http://localhost/callback"]})
        )
    )

    assert response.status == 201
    assert json.loads(response.text)["grant_types"] == [
        "authorization_code",
        "refresh_token",
    ]


def test_register_rejects_unsafe_redirects_and_missing_live_key() -> None:
    missing = _run(
        DcrRegisterView(_hass(key=None)).post(
            _request({"redirect_uris": ["https://client.example/callback"]})
        )
    )
    unsafe = _run(
        DcrRegisterView(_hass(key=KEY)).post(
            _request({"redirect_uris": ["http://client.example/callback"]})
        )
    )

    assert missing.status == 404
    assert unsafe.status == 400
    assert json.loads(unsafe.text)["error"] == "invalid_redirect_uri"


def test_register_body_cap_accepts_exact_boundary_and_rejects_next_byte() -> None:
    raw = json.dumps(
        {"redirect_uris": ["https://client.example/callback"]},
        separators=(",", ":"),
    ).encode()
    exact = raw + b" " * (oauth_dcr.MAX_DCR_BODY_BYTES - len(raw))

    accepted = _run(DcrRegisterView(_hass(key=KEY)).post(_raw_request(exact)))
    oversized = _run(DcrRegisterView(_hass(key=KEY)).post(_raw_request(exact + b" ")))

    assert accepted.status == 201
    assert oversized.status == 400
    assert json.loads(oversized.text)["error"] == "invalid_client_metadata"


def test_register_rejects_redirect_count_and_length_over_caps() -> None:
    too_many = [
        f"https://client.example/callback/{index}"
        for index in range(oauth_dcr.MAX_REDIRECT_URIS + 1)
    ]
    prefix = "https://client.example/"
    too_long = prefix + "x" * (oauth_dcr.MAX_REDIRECT_URI_LEN - len(prefix) + 1)

    count_response = _run(
        DcrRegisterView(_hass(key=KEY)).post(_request({"redirect_uris": too_many}))
    )
    length_response = _run(
        DcrRegisterView(_hass(key=KEY)).post(_request({"redirect_uris": [too_long]}))
    )

    assert count_response.status == 400
    assert json.loads(count_response.text)["error"] == "invalid_redirect_uri"
    assert length_response.status == 400
    assert json.loads(length_response.text)["error"] == "invalid_redirect_uri"
