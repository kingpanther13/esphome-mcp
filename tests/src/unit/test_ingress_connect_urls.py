"""Unit tests for admin-only MCP connect URL surfacing."""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
COMPONENT = ROOT / "custom_components" / "esphome_mcp"


def _install_package_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install custom_components package stubs that point at this checkout."""
    custom_components_mod = ModuleType("custom_components")
    custom_components_mod.__path__ = [str(ROOT / "custom_components")]
    package_mod = ModuleType("custom_components.esphome_mcp")
    package_mod.__path__ = [str(COMPONENT)]

    monkeypatch.setitem(sys.modules, "custom_components", custom_components_mod)
    monkeypatch.setitem(sys.modules, "custom_components.esphome_mcp", package_mod)


def _install_embedded_setup_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cloud_url: str | None = "https://example.ui.nabu.casa",
    local_url: str | None = "http://homeassistant.local:8123",
    notification_calls: list[dict[str, Any]] | None = None,
) -> None:
    """Install enough Home Assistant modules to import embedded_setup.py."""
    _install_package_stubs(monkeypatch)

    ha_mod = ModuleType("homeassistant")
    ha_mod.__path__ = []
    components_mod = ModuleType("homeassistant.components")
    components_mod.__path__ = []
    persistent_mod = ModuleType("homeassistant.components.persistent_notification")

    def async_create(
        hass: Any,
        message: str,
        *,
        title: str,
        notification_id: str,
    ) -> None:
        if notification_calls is not None:
            notification_calls.append(
                {
                    "action": "create",
                    "hass": hass,
                    "message": message,
                    "title": title,
                    "notification_id": notification_id,
                }
            )

    def async_dismiss(hass: Any, notification_id: str) -> None:
        if notification_calls is not None:
            notification_calls.append(
                {
                    "action": "dismiss",
                    "hass": hass,
                    "notification_id": notification_id,
                }
            )

    persistent_mod.async_create = async_create
    persistent_mod.async_dismiss = async_dismiss
    components_mod.persistent_notification = persistent_mod

    cloud_mod = ModuleType("homeassistant.components.cloud")

    class CloudNotAvailable(Exception):
        """Raised when the cloud URL is unavailable."""

    def async_remote_ui_url(_hass: Any) -> str:
        if cloud_url is None:
            raise CloudNotAvailable
        return cloud_url

    cloud_mod.CloudNotAvailable = CloudNotAvailable
    cloud_mod.async_remote_ui_url = async_remote_ui_url

    config_entries_mod = ModuleType("homeassistant.config_entries")
    config_entries_mod.ConfigEntry = object
    core_mod = ModuleType("homeassistant.core")
    core_mod.HomeAssistant = object

    helpers_mod = ModuleType("homeassistant.helpers")
    helpers_mod.__path__ = []
    issue_mod = ModuleType("homeassistant.helpers.issue_registry")
    issue_mod.IssueSeverity = SimpleNamespace(ERROR="error")
    issue_mod.async_create_issue = lambda *_args, **_kwargs: None
    issue_mod.async_delete_issue = lambda *_args, **_kwargs: None
    helpers_mod.issue_registry = issue_mod

    network_mod = ModuleType("homeassistant.helpers.network")

    class NoURLAvailableError(Exception):
        """Raised when Home Assistant cannot resolve a URL."""

    def get_url(
        _hass: Any,
        *,
        allow_external: bool,
        prefer_external: bool,
    ) -> str:
        assert allow_external is False
        assert prefer_external is False
        if local_url is None:
            raise NoURLAvailableError
        return local_url

    network_mod.NoURLAvailableError = NoURLAvailableError
    network_mod.get_url = get_url

    embedded_server_mod = ModuleType("custom_components.esphome_mcp.embedded_server")
    embedded_server_mod.EmbeddedServerError = type(
        "EmbeddedServerError",
        (Exception,),
        {},
    )

    class EmbeddedServerManager:
        async def async_stop(self) -> None:
            return None

    embedded_server_mod.EmbeddedServerManager = EmbeddedServerManager
    webhook_mod = ModuleType("custom_components.esphome_mcp.mcp_webhook")

    async def async_register_webhook(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def async_unregister_webhook(*_args: Any, **_kwargs: Any) -> None:
        return None

    webhook_mod.async_register_webhook = async_register_webhook
    webhook_mod.async_unregister_webhook = async_unregister_webhook

    for module_name in (
        "custom_components.esphome_mcp.embedded_setup",
        "custom_components.esphome_mcp.const",
    ):
        monkeypatch.delitem(sys.modules, module_name, raising=False)

    for name, module in {
        "homeassistant": ha_mod,
        "homeassistant.components": components_mod,
        "homeassistant.components.persistent_notification": persistent_mod,
        "homeassistant.components.cloud": cloud_mod,
        "homeassistant.config_entries": config_entries_mod,
        "homeassistant.core": core_mod,
        "homeassistant.helpers": helpers_mod,
        "homeassistant.helpers.issue_registry": issue_mod,
        "homeassistant.helpers.network": network_mod,
        "custom_components.esphome_mcp.embedded_server": embedded_server_mod,
        "custom_components.esphome_mcp.mcp_webhook": webhook_mod,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)


def _load_embedded_setup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cloud_url: str | None = "https://example.ui.nabu.casa",
    local_url: str | None = "http://homeassistant.local:8123",
    notification_calls: list[dict[str, Any]] | None = None,
) -> ModuleType:
    _install_embedded_setup_stubs(
        monkeypatch,
        cloud_url=cloud_url,
        local_url=local_url,
        notification_calls=notification_calls,
    )
    return importlib.import_module("custom_components.esphome_mcp.embedded_setup")


def test_build_connect_urls_includes_nabu_casa_remote_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resolver surfaces the full Nabu Casa webhook URL."""
    module = _load_embedded_setup(monkeypatch)
    entry = SimpleNamespace(
        data={
            module.DATA_WEBHOOK_ID: "abc123",
            module.DATA_SECRET_PATH: "/private_abc",
        },
        options={
            module.OPT_BIND_HOST: module.BIND_HOST_ALL,
            module.OPT_SERVER_PORT: 9590,
        },
    )

    urls = module.build_connect_urls(SimpleNamespace(), entry)

    assert urls[0] == "https://example.ui.nabu.casa/api/webhook/abc123"
    assert "http://homeassistant.local:8123/api/webhook/abc123" in urls
    assert "http://homeassistant.local:9590/private_abc (direct access)" in urls
    assert all("<your-home-assistant-url>" not in url for url in urls)


def test_build_connect_urls_prefers_configured_external_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured reverse-proxy URL leads before the auto-detected cloud URL."""
    module = _load_embedded_setup(monkeypatch)
    entry = SimpleNamespace(
        data={
            module.DATA_WEBHOOK_ID: "abc123",
            module.DATA_SECRET_PATH: "/private_abc",
        },
        options={
            module.OPT_EXTERNAL_URL: "https://mcp.example.test/",
            module.OPT_BIND_HOST: "127.0.0.1",
            module.OPT_SERVER_PORT: 9590,
        },
    )

    urls = module.build_connect_urls(SimpleNamespace(), entry)

    assert urls[:2] == [
        "https://mcp.example.test/api/webhook/abc123",
        "https://example.ui.nabu.casa/api/webhook/abc123",
    ]
    assert not any(url.endswith("//api/webhook/abc123") for url in urls)


def test_build_connect_urls_disabled_webhook_only_surfaces_direct_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local-only mode does not invent a remote webhook URL."""
    module = _load_embedded_setup(monkeypatch)
    entry = SimpleNamespace(
        data={
            module.DATA_WEBHOOK_ID: "abc123",
            module.DATA_SECRET_PATH: "/private_abc",
        },
        options={
            module.OPT_BIND_HOST: module.BIND_HOST_ALL,
            module.OPT_SERVER_PORT: 9590,
        },
    )

    urls = module.build_connect_urls(SimpleNamespace(), entry, webhook_enabled=False)

    assert urls == ["http://homeassistant.local:9590/private_abc (direct access)"]


def test_build_connect_urls_brackets_ipv6_direct_access_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct-access URLs preserve IPv6 brackets from Home Assistant's local URL."""
    module = _load_embedded_setup(monkeypatch, local_url="http://[fd00::1]:8123")
    entry = SimpleNamespace(
        data={
            module.DATA_WEBHOOK_ID: "abc123",
            module.DATA_SECRET_PATH: "/private_abc",
        },
        options={
            module.OPT_BIND_HOST: module.BIND_HOST_ALL,
            module.OPT_SERVER_PORT: 9590,
        },
    )

    urls = module.build_connect_urls(SimpleNamespace(), entry)

    assert "http://[fd00::1]:9590/private_abc (direct access)" in urls
    assert "http://fd00::1:9590/private_abc (direct access)" not in urls


def test_build_connect_urls_no_resolved_base_never_uses_old_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The no-cloud/no-local fallback avoids the bad literal base placeholder."""
    module = _load_embedded_setup(monkeypatch, cloud_url=None, local_url=None)
    entry = SimpleNamespace(
        data={
            module.DATA_WEBHOOK_ID: "abc123",
            module.DATA_SECRET_PATH: "/private_abc",
        },
        options={
            module.OPT_BIND_HOST: "127.0.0.1",
            module.OPT_SERVER_PORT: 9590,
        },
    )

    urls = module.build_connect_urls(SimpleNamespace(), entry)

    assert urls == ["/api/webhook/abc123  (prefix with your Home Assistant URL)"]
    assert all("<your-home-assistant-url>" not in url for url in urls)


def test_surface_connect_urls_notification_omits_secret_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The non-admin-visible persistent notification does not carry credentials."""
    notification_calls: list[dict[str, Any]] = []
    module = _load_embedded_setup(monkeypatch, notification_calls=notification_calls)
    hass = SimpleNamespace()
    entry = SimpleNamespace(
        data={
            module.DATA_WEBHOOK_ID: "abc123",
            module.DATA_SECRET_PATH: "/private_abc",
        },
        options={
            module.OPT_BIND_HOST: module.BIND_HOST_ALL,
            module.OPT_SERVER_PORT: 9590,
        },
    )

    module._surface_connect_urls(hass, entry, module.WEBHOOK_AUTH_NONE)

    create_calls = [call for call in notification_calls if call["action"] == "create"]
    assert len(create_calls) == 1
    message = create_calls[0]["message"]
    assert "/api/webhook/abc123" not in message
    assert "/private_abc" not in message
    assert "Configure screen" in message
    assert create_calls[0]["notification_id"] == module._NOTIFICATION_ID


def test_teardown_dismisses_running_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unload/removal clears the running notification instead of leaving stale status."""
    notification_calls: list[dict[str, Any]] = []
    module = _load_embedded_setup(monkeypatch, notification_calls=notification_calls)
    hass = SimpleNamespace(data={module.DOMAIN: {}})

    asyncio.run(module.async_teardown_server(hass))
    asyncio.run(module.async_remove_server(hass, SimpleNamespace()))

    dismiss_calls = [call for call in notification_calls if call["action"] == "dismiss"]
    assert dismiss_calls == [
        {"action": "dismiss", "hass": hass, "notification_id": module._NOTIFICATION_ID},
        {"action": "dismiss", "hass": hass, "notification_id": module._NOTIFICATION_ID},
    ]


def _install_config_flow_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    resolved_urls: list[str],
) -> dict[str, Any]:
    """Install enough Home Assistant modules to import config_flow.py."""
    _install_package_stubs(monkeypatch)
    captured: dict[str, Any] = {}

    ha_mod = ModuleType("homeassistant")
    ha_mod.__path__ = []
    vol_mod = ModuleType("voluptuous")
    vol_mod.Schema = lambda schema: schema
    vol_mod.Required = lambda key, **_kwargs: key
    vol_mod.Optional = lambda key, **_kwargs: key
    vol_mod.All = lambda *validators: validators
    vol_mod.Coerce = lambda value_type: value_type
    vol_mod.Range = lambda **_kwargs: object()
    config_entries_mod = ModuleType("homeassistant.config_entries")

    class ConfigFlow:
        def __init_subclass__(cls, **_kwargs: Any) -> None:
            return None

    class OptionsFlow:
        pass

    config_entries_mod.ConfigEntry = object
    config_entries_mod.ConfigFlow = ConfigFlow
    config_entries_mod.ConfigFlowResult = dict[str, Any]
    config_entries_mod.OptionsFlow = OptionsFlow

    core_mod = ModuleType("homeassistant.core")
    core_mod.callback = lambda func: func

    helpers_mod = ModuleType("homeassistant.helpers")
    helpers_mod.__path__ = []
    selector_mod = ModuleType("homeassistant.helpers.selector")

    class SelectOptionDict(dict):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(kwargs)

    class SelectSelectorConfig:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class SelectSelector:
        def __init__(self, config: SelectSelectorConfig) -> None:
            self.config = config

    selector_mod.SelectOptionDict = SelectOptionDict
    selector_mod.SelectSelector = SelectSelector
    selector_mod.SelectSelectorConfig = SelectSelectorConfig
    selector_mod.SelectSelectorMode = SimpleNamespace(DROPDOWN="dropdown")
    helpers_mod.selector = selector_mod

    embedded_setup_mod = ModuleType("custom_components.esphome_mcp.embedded_setup")

    def build_connect_urls(
        hass: Any,
        entry: Any,
        *,
        webhook_enabled: bool = True,
    ) -> list[str]:
        captured["hass"] = hass
        captured["entry"] = entry
        captured["webhook_enabled"] = webhook_enabled
        return resolved_urls

    embedded_setup_mod.build_connect_urls = build_connect_urls

    for module_name in (
        "custom_components.esphome_mcp.config_flow",
        "custom_components.esphome_mcp.const",
        "custom_components.esphome_mcp.embedded_setup",
    ):
        monkeypatch.delitem(sys.modules, module_name, raising=False)

    for name, module in {
        "voluptuous": vol_mod,
        "homeassistant": ha_mod,
        "homeassistant.config_entries": config_entries_mod,
        "homeassistant.core": core_mod,
        "homeassistant.helpers": helpers_mod,
        "homeassistant.helpers.selector": selector_mod,
        "custom_components.esphome_mcp.embedded_setup": embedded_setup_mod,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    return captured


def test_options_hint_uses_resolved_connect_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Configure screen is fed by the resolver, not a placeholder base URL."""
    captured = _install_config_flow_stubs(
        monkeypatch,
        resolved_urls=["https://example.ui.nabu.casa/api/webhook/abc123"],
    )
    module = importlib.import_module("custom_components.esphome_mcp.config_flow")
    flow = module.EspHomeMcpOptionsFlow()
    flow.hass = object()
    flow.config_entry = SimpleNamespace(
        data={
            module.DATA_WEBHOOK_ID: "abc123",
            module.DATA_SECRET_PATH: "/private_abc",
        },
        options={module.OPT_ENABLE_WEBHOOK: True},
    )

    hint = flow._connect_url_hint()

    assert hint == "Connect URL(s):\n- https://example.ui.nabu.casa/api/webhook/abc123"
    assert "<your-home-assistant-url>" not in hint
    assert captured == {
        "hass": flow.hass,
        "entry": flow.config_entry,
        "webhook_enabled": True,
    }


def test_options_hint_static_fallback_avoids_bad_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even without hass on the flow object, the old placeholder is not rendered."""
    _install_config_flow_stubs(monkeypatch, resolved_urls=[])
    module = importlib.import_module("custom_components.esphome_mcp.config_flow")
    flow = module.EspHomeMcpOptionsFlow()
    flow.config_entry = SimpleNamespace(
        data={
            module.DATA_WEBHOOK_ID: "abc123",
            module.DATA_SECRET_PATH: "/private_abc",
        },
        options={},
    )

    hint = flow._connect_url_hint()

    assert "Remote connect URL: /api/webhook/abc123" in hint
    assert "Home Assistant URL unavailable" in hint
    assert "<your-home-assistant-url>" not in hint


def test_options_hint_propagates_disabled_webhook_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The options hint asks the resolver for local-only URLs when webhook is disabled."""
    captured = _install_config_flow_stubs(monkeypatch, resolved_urls=[])
    module = importlib.import_module("custom_components.esphome_mcp.config_flow")
    flow = module.EspHomeMcpOptionsFlow()
    flow.hass = object()
    flow.config_entry = SimpleNamespace(
        data={
            module.DATA_WEBHOOK_ID: "abc123",
            module.DATA_SECRET_PATH: "/private_abc",
        },
        options={
            module.OPT_ENABLE_WEBHOOK: False,
            module.OPT_SERVER_PORT: 9590,
        },
    )

    hint = flow._connect_url_hint()

    assert "Remote access via webhook is disabled" in hint
    assert "http://127.0.0.1:9590/private_abc" in hint
    assert "/api/webhook/abc123" not in hint
    assert captured == {
        "hass": flow.hass,
        "entry": flow.config_entry,
        "webhook_enabled": False,
    }


def test_options_normalize_drops_stale_fastmcp_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale clients cannot persist the removed FastMCP package override."""
    _install_config_flow_stubs(monkeypatch, resolved_urls=[])
    module = importlib.import_module("custom_components.esphome_mcp.config_flow")

    normalized = module.EspHomeMcpOptionsFlow._normalize(
        {
            module.OPT_SERVER_PORT: 9590,
            module.OPT_BIND_HOST: module.BIND_HOST_LOOPBACK,
            module.OPT_WEBHOOK_AUTH: module.WEBHOOK_AUTH_NONE,
            module.OPT_ENABLE_WEBHOOK: True,
            module.OPT_EXTERNAL_URL: "https://example.com/",
            module.OPT_WEBHOOK_ID_OVERRIDE: "",
            module.OPT_SECRET_PATH_OVERRIDE: "",
            "pip_spec": "fastmcp==0.0.1",
        }
    )

    assert normalized[module.OPT_EXTERNAL_URL] == "https://example.com"
    assert "pip_spec" not in normalized
