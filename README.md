# ESPHome MCP

ESPHome MCP is a Home Assistant custom component that runs a small FastMCP server inside Home Assistant and exposes it through the same webhook ingress pattern used by ha-mcp's custom component server path.

Initial defaults:

- Custom component only: `custom_components/esphome_mcp`
- MCP server port: `9590`
- Tool prefix: `esp_`
- Webhook auth modes: secret webhook URL or Home Assistant `ha_auth`
- Initial tools: `esp_overview`, `esp_list_devices`, `esp_list_entities`

The webhook URL works through Nabu Casa Remote UI because it is registered as a Home Assistant webhook at `/api/webhook/<secret>`.

## HACS

Add this repository as a custom HACS integration repository, then install **ESPHome MCP** and restart Home Assistant.
