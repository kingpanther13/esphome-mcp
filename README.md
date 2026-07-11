# ESPHome MCP

ESPHome MCP is a Home Assistant custom component that exposes ESPHome and the
official ESPHome Device Builder add-on as MCP tools.

It runs an in-process FastMCP server inside Home Assistant, listens on port
`9590` by default, and exposes the MCP endpoint through a Home Assistant webhook
so remote MCP clients can connect through the same Home Assistant URL you already
use, including Nabu Casa Remote UI.

## What You Get

- A custom-component-only install path for Home Assistant.
- MCP tool names with the `esp_` prefix.
- Searchable Home Assistant ESPHome registry context.
- Device Builder device, YAML, log, validation, and firmware tools.
- Supervisor-backed ESPHome add-on lifecycle and config management.
- Webhook access with either a secret URL or Home Assistant administrator sign-in.

## Requirements

- Home Assistant `2025.9.1` or newer.
- HACS, if installing through the Home Assistant Community Store.
- Home Assistant OS or Supervised Home Assistant for ESPHome add-on and Device
  Builder tools. These tools require Supervisor.
- The official ESPHome Device Builder add-on installed and running for the
  Device Builder tool set.
- Network/package-install access on first start so Home Assistant can install
  the embedded MCP server runtime dependency.

The Home Assistant registry tools can still report ESPHome integration devices
and entities anywhere this custom component can run, but the add-on and Device
Builder tools need Supervisor.

## HACS Installation

1. In HACS, open **Custom repositories**.
2. Add this repository URL:

   ```text
   https://github.com/kingpanther13/esphome-mcp
   ```

3. Choose category **Integration**.
4. Install the latest published **ESPHome MCP** release.
5. Restart Home Assistant.
6. Go to **Settings** > **Devices & services** > **Add integration**.
7. Search for **ESPHome MCP** and create the integration entry.

This repository is release-backed for HACS installs. The release workflow
publishes the component manifest version as a GitHub Release tag such as
`v0.1.4`, which is the version HACS displays. Do not install a
seven-character commit version such as `99cdab0`. If HACS has cached an old
commit-only entry, refresh the custom repository before installing.

After setup, Home Assistant starts the embedded MCP server and registers the
webhook route. The integration Configure screen, Home Assistant notification,
and Home Assistant log show the connection details. Manage all server and
connection settings through **Settings** > **Devices & services** > **ESPHome MCP**
> **Configure**.

## Connecting An MCP Client

Use the Home Assistant webhook URL as the MCP server URL:

```text
https://<your-home-assistant-url>/api/webhook/<webhook-secret>
```

The default webhook mode treats that URL as the credential. Keep it private.

The options flow can switch webhook access to Home Assistant `ha_auth`. Clients
that support MCP OAuth/protected-resource discovery can then sign in with a Home
Assistant administrator account.

Direct LAN access is also available when the server is bound to `0.0.0.0`:

```text
http://<home-assistant-ip>:9590/<private-path>
```

Direct port access uses the private path as its credential. Set network access
to loopback if you only want webhook access.

Webhook access is the recommended path for remote clients because it works
through Home Assistant's normal external URL and Nabu Casa Remote UI.

## Tools

### Home Assistant ESPHome Context

| Tool | Purpose |
| --- | --- |
| `esp_overview` | Return ESPHome integration, device, and entity counts. |
| `esp_list_devices` | Search ESPHome devices known to Home Assistant by query, area, config entry state, and limit. |
| `esp_list_entities` | Search ESPHome entities by query, domain, device, state, disabled status, and limit. |

### ESPHome Add-on Management

| Tool | Purpose |
| --- | --- |
| `esp_manage_addon` | Manage the ESPHome add-on through Supervisor, update add-on options, or call add-on HTTP/WebSocket endpoints. |

`esp_manage_addon` supports Supervisor lifecycle actions such as `install`,
`update`, `rebuild`, `start`, `stop`, `restart`, and `uninstall`. It also
supports add-on config updates for `options`, `network`, `boot`, `auto_update`,
and `watchdog`.

For add-on API calls, the component routes through Home Assistant Core's
`/api/hassio_ingress/...` proxy with a fresh Supervisor `ingress_session` cookie.
That matches ESPHome Device Builder's current trusted-ingress behavior. Direct
container-port routing is available only when explicitly requested with `port`.

### Device Builder

| Tool | Purpose |
| --- | --- |
| `esp_dashboard_devices` | List and search configured and importable ESPHome Device Builder devices. |
| `esp_search_yaml` | Search raw ESPHome YAML across Device Builder configurations. |
| `esp_get_yaml` | Read one ESPHome YAML configuration. |
| `esp_update_yaml` | Write one ESPHome YAML configuration through Device Builder. |
| `esp_validate_yaml` | Run Device Builder validation for one configuration. |
| `esp_device_logs` | Collect a bounded batch of Device Builder device logs. |
| `esp_compile_firmware` | Queue a firmware compile job. |
| `esp_install_firmware` | Queue a firmware install job. |
| `esp_firmware_jobs` | List firmware jobs with optional status and configuration filters. |
| `esp_get_firmware_job` | Return one firmware job by ID. |
| `esp_follow_firmware_job` | Follow one firmware job stream and return collected output. |

These tools target ESPHome Device Builder's current multiplexed `/ws` API,
including `devices/list`, `yaml/search`, `devices/get_config`,
`devices/update_config`, `devices/validate`, `devices/logs`,
`devices/stop_stream`, `firmware/compile`, `firmware/install`,
`firmware/get_jobs`, `firmware/get_job`, and `firmware/follow_job`.

## Safety Notes

- `esp_update_yaml`, firmware actions, and add-on lifecycle/config actions can
  change running ESPHome systems.
- The default webhook URL is a shared secret. Treat it like a password.
- `ha_auth` mode requires a Home Assistant administrator account because the MCP
  server can perform privileged Home Assistant and Supervisor operations.
- Add-on and Device Builder tools require Home Assistant Supervisor; they return
  structured errors when Supervisor or the ESPHome add-on is not available.
- ESPHome MCP and ha-mcp share FastMCP inside the Home Assistant Core process.
  Keep both integrations current and restart Home Assistant after either one
  changes that shared runtime.

## Testing

The repository includes unit and end-to-end coverage for the custom component
and tool surface:

- Ruff lint and format checks plus an AST-based shared-dependency sandbox.
- Unit tests for metadata, tool registration, Supervisor routing, ingress-session
  routing, Device Builder WebSocket framing, stream cancellation, import-deadlock
  recovery, and wrapper behavior.
- ESPHome host-device E2E tests using ESPHome's host platform.
- HAOS embedded E2E tests that boot a HAOS image, install ESPHome Device Builder,
  bake this custom component into Home Assistant, and drive the MCP webhook.

## Prior Art

This project intentionally builds on ha-mcp's Home Assistant custom-component
ingress/auth approach and compares protocol behavior against existing ESPHome MCP
implementations by loryanstrant, jeeftor, bberrevoets, and jrigling.

The distinguishing piece here is the custom-component-only Home Assistant
deployment path: Home Assistant webhook auth, Nabu Casa-compatible ingress, and
Supervisor-backed ESPHome add-on routing.
