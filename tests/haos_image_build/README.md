# HAOS Image Build

This directory contains the ESPHome MCP HAOS E2E image builder. It follows the
same QEMU/libguestfs/Supervisor-WebSocket pattern used by ha-mcp's embedded HAOS
lane, trimmed to this component:

- boot a pinned HAOS qcow2,
- onboard Home Assistant,
- install and start the official ESPHome Device Builder add-on,
- bootstrap the complete HACS release through the supported Get HACS add-on,
- shut down HAOS,
- inject `custom_components/esphome_mcp` and an enabled config entry into
  `/supervisor/homeassistant`.

The workflow caches the resulting uncompressed qcow2 by hashing this directory
and `custom_components/esphome_mcp`.
