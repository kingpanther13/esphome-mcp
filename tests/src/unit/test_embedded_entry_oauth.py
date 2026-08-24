"""Tests for stable OAuth state and startup-time route binding."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from ._oauth_stubs import install

install()

from custom_components.esphome_mcp import embedded_entry  # noqa: E402
from custom_components.esphome_mcp.const import (  # noqa: E402
    DATA_DCR_SIGNING_KEY,
    DATA_SECRET_PATH,
    DATA_WEBHOOK_ID,
    OAUTH_BASE,
    OPT_ENABLE_WEBHOOK,
    OPT_WEBHOOK_AUTH,
    WEBHOOK_AUTH_NONE,
)


class _ConfigEntries:
    def async_update_entry(
        self,
        entry: Any,
        *,
        data: dict[str, Any],
        options: dict[str, Any] | None = None,
    ) -> None:
        entry.data = data
        if options is not None:
            entry.options = options


def _hass() -> SimpleNamespace:
    registered: list[Any] = []
    return SimpleNamespace(
        data={},
        config_entries=_ConfigEntries(),
        http=SimpleNamespace(register_view=registered.append),
        registered=registered,
    )


def test_ensure_secrets_mints_one_stable_dcr_signing_key() -> None:
    hass = _hass()
    entry = SimpleNamespace(data={}, options={})

    embedded_entry._ensure_secrets(hass, entry)
    first_key = entry.data[DATA_DCR_SIGNING_KEY]
    embedded_entry._ensure_secrets(hass, entry)

    assert len(first_key) == 64
    assert entry.data[DATA_DCR_SIGNING_KEY] == first_key
    assert entry.data[DATA_WEBHOOK_ID].startswith("esp_mcp_")
    assert entry.data[DATA_SECRET_PATH].startswith("/private_")


def test_prebind_registers_complete_nonlegacy_oauth_surface_once() -> None:
    hass = _hass()
    entry = SimpleNamespace(
        data={},
        options={
            OPT_ENABLE_WEBHOOK: True,
            OPT_WEBHOOK_AUTH: WEBHOOK_AUTH_NONE,
        },
    )

    embedded_entry._prebind_oauth_views(hass, entry)
    first_count = len(hass.registered)
    embedded_entry._prebind_oauth_views(hass, entry)
    urls = {view.url for view in hass.registered}

    assert first_count == 11
    assert len(hass.registered) == first_count
    assert f"{OAUTH_BASE}/authorization-server" in urls
    assert f"{OAUTH_BASE}/authorize" in urls
    assert f"{OAUTH_BASE}/token" in urls
    assert f"{OAUTH_BASE}/revoke" in urls
    assert f"{OAUTH_BASE}/register" in urls


def test_prebind_skips_local_only_mode() -> None:
    hass = _hass()
    entry = SimpleNamespace(
        data={},
        options={
            OPT_ENABLE_WEBHOOK: False,
            OPT_WEBHOOK_AUTH: WEBHOOK_AUTH_NONE,
        },
    )

    embedded_entry._prebind_oauth_views(hass, entry)

    assert hass.registered == []
