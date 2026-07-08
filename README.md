# ESPHome MCP

ESPHome MCP is a Home Assistant custom component that runs a small FastMCP server inside Home Assistant and exposes it through the same webhook ingress pattern used by ha-mcp's custom component server path.

Initial defaults:

- Custom component only: `custom_components/esphome_mcp`
- MCP server port: `9590`
- Tool prefix: `esp_`
- Webhook auth modes: secret webhook URL or Home Assistant `ha_auth`
- Initial tools:
  `esp_overview`, `esp_list_devices`, `esp_list_entities`,
  `esp_dashboard_devices`, `esp_search_yaml`, `esp_get_yaml`,
  `esp_update_yaml`, `esp_validate_yaml`, `esp_device_logs`,
  `esp_compile_firmware`, `esp_install_firmware`, `esp_firmware_jobs`,
  `esp_get_firmware_job`, `esp_follow_firmware_job`, `esp_manage_addon`

The webhook URL works through Nabu Casa Remote UI because it is registered as a Home Assistant webhook at `/api/webhook/<secret>`.

`esp_manage_addon` is the ESPHome-focused starting point copied from ha-mcp's
add-on management tool. It supports Supervisor lifecycle/config actions for the
ESPHome add-on and proxies ESPHome dashboard HTTP/WebSocket calls through the
same Supervisor ingress-session path HA Core uses for Home Assistant add-on
ingress.

The preferred workflow tools target ESPHome Device Builder's current multiplexed
`/ws` API (`devices/list`, `yaml/search`, `devices/get_config`,
`devices/update_config`, `devices/validate`, `devices/logs`,
`firmware/compile`, `firmware/install`, `firmware/get_jobs`,
`firmware/get_job`, and `firmware/follow_job`). The Home Assistant context tools
`esp_list_devices` and `esp_list_entities` support search/filter parameters for
the HA ESPHome integration registry view.

## Testing

Unit tests cover Supervisor action routing, add-on ingress-session routing,
current Device Builder WebSocket command framing, stream cancellation, and tool
wrapper command selection. The component E2E workflow builds or restores a HAOS
qcow2, installs ESPHome Device Builder, bakes this custom component into
Home Assistant, boots QEMU/KVM, and drives the MCP webhook.

## Prior Art

This project intentionally builds on ha-mcp's Home Assistant custom component
ingress/auth approach and compares protocol behavior against the existing
ESPHome MCP implementations by loryanstrant, jeeftor, bberrevoets, and
jrigling. The distinguishing piece here is the custom-component-only Home
Assistant deployment path: Home Assistant webhook auth, Nabu Casa-compatible
ingress, and Supervisor-backed ESPHome add-on routing.

## HACS

Add this repository as a custom HACS integration repository, then install **ESPHome MCP** and restart Home Assistant.
