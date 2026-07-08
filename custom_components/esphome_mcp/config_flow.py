"""Config and options flow for ESPHome MCP."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    BIND_HOST_ALL,
    BIND_HOST_LOOPBACK,
    DATA_SECRET_PATH,
    DATA_WEBHOOK_ID,
    DEFAULT_BIND_HOST,
    DEFAULT_SERVER_PORT,
    DOMAIN,
    OPT_BIND_HOST,
    OPT_ENABLE_WEBHOOK,
    OPT_EXTERNAL_URL,
    OPT_REGENERATE_SECRETS,
    OPT_SECRET_PATH_OVERRIDE,
    OPT_SERVER_PORT,
    OPT_WEBHOOK_AUTH,
    OPT_WEBHOOK_ID_OVERRIDE,
    WEBHOOK_AUTH_HA,
    WEBHOOK_AUTH_NONE,
)

_ENTRY_TITLE = "ESPHome MCP Server"
_UNIQUE_ID = f"{DOMAIN}-server"


class EspHomeMcpConfigFlow(ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Handle the config flow for ESPHome MCP."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return EspHomeMcpOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm and create the single ESPHome MCP server entry."""
        await self.async_set_unique_id(_UNIQUE_ID)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(title=_ENTRY_TITLE, data={}, options={})
        return self.async_show_form(step_id="user")


class EspHomeMcpOptionsFlow(OptionsFlow):
    """Options flow for the in-process ESPHome MCP server."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show or apply server options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=self._normalize(user_input))

        opts = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    OPT_SERVER_PORT,
                    default=opts.get(OPT_SERVER_PORT, DEFAULT_SERVER_PORT),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
                vol.Required(
                    OPT_BIND_HOST,
                    default=opts.get(OPT_BIND_HOST, DEFAULT_BIND_HOST),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(
                                value=BIND_HOST_ALL,
                                label="Local network (default)",
                            ),
                            SelectOptionDict(
                                value=BIND_HOST_LOOPBACK,
                                label="This machine only (loopback)",
                            ),
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    OPT_WEBHOOK_AUTH,
                    default=opts.get(OPT_WEBHOOK_AUTH, WEBHOOK_AUTH_NONE),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[WEBHOOK_AUTH_NONE, WEBHOOK_AUTH_HA],
                        translation_key="server_webhook_auth",
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    OPT_ENABLE_WEBHOOK,
                    default=bool(opts.get(OPT_ENABLE_WEBHOOK, True)),
                ): bool,
                vol.Optional(
                    OPT_EXTERNAL_URL,
                    default=opts.get(OPT_EXTERNAL_URL, ""),
                ): str,
                vol.Optional(
                    OPT_WEBHOOK_ID_OVERRIDE,
                    default=opts.get(OPT_WEBHOOK_ID_OVERRIDE, ""),
                ): str,
                vol.Optional(
                    OPT_SECRET_PATH_OVERRIDE,
                    default=opts.get(OPT_SECRET_PATH_OVERRIDE, ""),
                ): str,
                vol.Optional(OPT_REGENERATE_SECRETS, default=False): bool,
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            description_placeholders={"connect_url": self._connect_url_hint()},
        )

    @staticmethod
    def _normalize(user_input: dict[str, Any]) -> dict[str, Any]:
        """Normalize option strings before storing them."""
        cleaned = dict(user_input)
        for key in (
            OPT_EXTERNAL_URL,
            OPT_WEBHOOK_ID_OVERRIDE,
            OPT_SECRET_PATH_OVERRIDE,
        ):
            cleaned[key] = str(cleaned.get(key, "") or "").strip()
        cleaned[OPT_EXTERNAL_URL] = cleaned[OPT_EXTERNAL_URL].rstrip("/")
        return cleaned

    def _connect_url_hint(self) -> str:
        """Return a human-readable connect-URL hint for the options form."""
        webhook_id = self.config_entry.data.get(DATA_WEBHOOK_ID)
        secret_path = self.config_entry.data.get(DATA_SECRET_PATH)
        if not webhook_id:
            return "The remote connect URL will appear once the server starts."
        port = self.config_entry.options.get(OPT_SERVER_PORT, DEFAULT_SERVER_PORT)
        external = str(self.config_entry.options.get(OPT_EXTERNAL_URL) or "").rstrip("/")
        base = external or "<your-home-assistant-url>"
        hint = f"Remote connect URL: {base}/api/webhook/{webhook_id}"
        if secret_path:
            hint += (
                f"\nLocal/LAN (when bind host is 0.0.0.0): "
                f"http://<home-assistant-ip>:{port}{secret_path}"
            )
        return hint
