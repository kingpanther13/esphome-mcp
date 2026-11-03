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
- Access to the installed Device Builder's native MCP tools and schemas.
- Supervisor-backed ESPHome add-on lifecycle and config management.
- Webhook access with either a secret URL or Home Assistant administrator sign-in.

## Requirements

- Home Assistant `2026.10.4` or newer.
- Home Assistant Marketplace on `2026.11` or newer, or HACS on `2026.10`,
  if installing through the community integration store.
- Home Assistant OS or Supervised Home Assistant for ESPHome add-on and Device
  Builder tools. These tools require Supervisor.
- The official ESPHome Device Builder add-on installed and running for the
  Device Builder tool set.
- Network/package-install access on first setup so Home Assistant can install
  any missing packages listed in the component manifest.

On older supported Core versions that do not provide a compatible `httpx2`,
setup also installs that dependency through Home Assistant's requirement manager.
Core's compatible version is reused when present.

The Home Assistant registry tools can still report ESPHome integration devices
and entities anywhere this custom component can run, but the add-on and Device
Builder tools need Supervisor.

## Marketplace / HACS Installation

On Home Assistant `2026.11` and newer, use the built-in **Marketplace** and
complete its first-run warning and any requested GitHub connection in the UI.
On `2026.10`, install and use **HACS**. The repository and component format is
the same for both stores.

1. In Marketplace or HACS, open **Custom repositories**.
2. Add this repository URL:

   ```text
   https://github.com/kingpanther13/esphome-mcp
   ```

3. Choose category **Integration**.
4. Install the latest published **ESPHome MCP** release.
5. Restart Home Assistant.
6. Go to **Settings** > **Devices & services** > **Add integration**.
7. Search for **ESPHome MCP** and create the integration entry.

This repository is release-backed for Marketplace and HACS installs. The release workflow
publishes the component manifest version as a GitHub Release tag such as
`v1.0.0`, which is the version the store displays. Do not install a
seven-character commit version such as `99cdab0`. If the store has cached an old
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
For MCP clients that probe OAuth during setup, ESPHome MCP also publishes its
own component-scoped discovery, dynamic registration, and invisible PKCE
authorize/token endpoints. That compatibility flow requires no Home Assistant
login; its token is cosmetic because the secret URL remains the credential.

The options flow can switch webhook access to Home Assistant `ha_auth`. Clients
that support MCP OAuth/protected-resource discovery can then sign in with a Home
Assistant administrator account. The scoped endpoints follow HA-MCP's current
DCR/CIMD translation, refresh-token envelope, and revocation behavior while
Home Assistant core remains the authorization authority.

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

Use `esp_native_tools` to discover the running Device Builder's native tools,
then `esp_native_call` to use them through the same authenticated ESPHome MCP
connection. No additional server URL or login is needed: requests pass through
Supervisor ingress to the add-on's `/api/mcp` endpoint.

| Tool | Purpose |
| --- | --- |
| `esp_native_tools` | Return the installed native catalog and input schemas; pass `tool` to select one schema, and `slug` to select an add-on. |
| `esp_native_call` | Call a discovered `name` with its `arguments`, preserving the native MCP reply in `result`. |

The native API is the preferred path for component/board discovery, structured
automation editing, and config edits with conflict detection. For example:

```text
esp_native_tools(tool="get_config")
esp_native_call(name="get_config", arguments={"configuration": "kitchen.yaml"})
esp_native_tools(tool="update_config")
esp_native_call(name="update_config", arguments={
  "configuration": "kitchen.yaml",
  "content": "<revised YAML>",
  "expected": "<exact text returned by get_config>"
})
```

Tool availability and required arguments come from the installed add-on, rather
than a copied catalog. Current native capabilities include `search_components`,
`get_component`, `search_boards`, `list_automations`, `get_available_automations`,
`get_automation_docs`, `upsert_automation`, and `delete_automation`, alongside
config, secrets, validation, and firmware tools. Always discover the schema first.
See the [official API contract](https://github.com/esphome/device-builder/blob/1.25.0/docs/API.md#mcp-endpoint-post-apimcp).

Native tool errors return `success: false` and retain `result.isError` and
`result.content`; JSON-RPC errors retain `native_error`. Writes are never retried
or replayed through WebSocket. If a call times out or loses its connection, read
back the state before retrying: the write may have completed. Because
`esp_native_call` can invoke writes and firmware installs, it is advertised as
potentially destructive even when the selected native tool only reads.

Older add-ons without `/api/mcp` return an actionable capability error from the
native tools. Existing tools remain available. `esp_get_yaml` uses native
`get_config` for bare `.yaml` filenames when available and preserves its existing
`content` response; it falls back only when the endpoint/tool is absent. Other
filename forms retain the WebSocket route. Authentication errors, malformed
replies, and timeouts never cause a fallback.

The following tools retain behavior that differs from the native API:

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

Except for the compatible native reads described above, these tools target
ESPHome Device Builder's current multiplexed `/ws` API,
including `devices/list`, `yaml/search`, `devices/get_config`,
`devices/update_config`, `devices/validate`, `devices/logs`,
`devices/stop_stream`, `firmware/compile`, `firmware/install`,
`firmware/get_jobs`, `firmware/get_job`, and `firmware/follow_job`.

They preserve dashboard filters/importable devices, cross-file YAML search,
`allow_wipe` editing, streaming validation/log output, explicit compile/install
flags (`force_local`, `bootloader`), and firmware-job listing/following. The
existing `esp_update_yaml` retains its legacy wipe guards; use native
`update_config` when you need the `expected` stale-write check. HA registry
context, Supervisor management, and supporting-file creation remain local tools.

For repository-hosted live testing, see [the HAOS dev environment](docs/dev-ha-env.md).

### Configuration Folders and Supporting Files

| Tool | Purpose |
| --- | --- |
| `esp_create_directory` | Create an ESPHome folder, including missing parent folders by default. Existing folders succeed. |
| `esp_create_file` | Create a UTF-8 supporting file, including missing parent folders by default. Existing files are never overwritten. |

Both tools take `path` relative to Home Assistant's `esphome` configuration
folder (normally `/config/esphome`) and an optional `parents` boolean, defaulting
to `true`. `esp_create_file` also requires `content`.
For example, create `packages/common`, then create
`packages/common/base.yaml` with the desired YAML content. Supporting files can
be referenced from device YAML using ESPHome's normal `!include` or package
syntax; these tools do not register nested files as dashboard devices.

These operations run locally inside Home Assistant Core and require that its
existing `esphome` folder is the configuration folder shared with Device
Builder. They do not use the add-on ingress API, accept an add-on slug, or
support a separate remote Device Builder filesystem. Verify that the add-on
uses this shared folder before trying them. A missing root folder returns an
error rather than creating a potentially unrelated configuration tree.
Absolute paths, traversal, hidden path components, and symlinks in the relative
path or at the root are refused. File creation bypasses Device Builder's YAML
validation guards; top-level `.yaml` and `.yml` device files are refused, and
`esp_update_yaml` remains the supported way to edit existing device
configurations.

## Safety Notes

- `esp_update_yaml`, firmware actions, and add-on lifecycle/config actions can
  change running ESPHome systems.
- The default webhook URL is a shared secret. Treat it like a password.
- `ha_auth` mode requires a Home Assistant administrator account because the MCP
  server can perform privileged Home Assistant and Supervisor operations.
- Add-on and Device Builder tools require Home Assistant Supervisor; they return
  structured errors when Supervisor or the ESPHome add-on is not available.
- ESPHome MCP ships its own private copy of FastMCP and the MCP SDK under
  `custom_components/esphome_mcp/_vendor`, so it never installs public FastMCP
  or MCP packages over Home Assistant's copies and never depends on HA-MCP.
  Both integrations can be installed and updated independently. The manifest
  declares only the vendored code's remaining dependencies, as minimum
  versions, through Home Assistant's normal requirements step.
  `scripts/vendor_fastmcp.py` regenerates the vendored tree and those manifest
  requirements from the pins in `_vendor/requirements.txt`; Renovate bumps the
  pins weekly and CI rejects any drift. Dependency markers use the supported
  Home Assistant runtime (Linux, Python 3.14), independent of the vendoring
  runner's Python version. Update `_TARGET_PYTHON_VERSION` in the script when
  that runtime changes; unsupported marker expressions fail the sync.

## Testing

The repository includes unit and end-to-end coverage for the custom component
and tool surface:

- Ruff lint and format checks, vendored-runtime regeneration checks, and
  Renovate config validation.
- Unit tests for metadata, tool registration, Supervisor routing, ingress-session
  routing, Device Builder WebSocket framing, stream cancellation, import-deadlock
  recovery, and wrapper behavior.
- ESPHome host-device E2E tests using ESPHome's host platform.
- HAOS embedded E2E tests that boot a HAOS image, install ESPHome Device Builder,
  bake this custom component into Home Assistant beside a released HA-MCP
  component, and drive both MCP webhooks.

## Prior Art

This project intentionally builds on ha-mcp's Home Assistant custom-component
ingress/auth approach and compares protocol behavior against existing ESPHome MCP
implementations by loryanstrant, jeeftor, bberrevoets, and jrigling.

The distinguishing piece here is the custom-component-only Home Assistant
deployment path: Home Assistant webhook auth, Nabu Casa-compatible ingress, and
Supervisor-backed ESPHome add-on routing.
