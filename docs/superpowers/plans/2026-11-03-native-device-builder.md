# Native Device Builder MCP integration

Addresses #196. Keep the authenticated ESPHome MCP connection and its HA-specific
tools, and use the installed Device Builder's native MCP API through Supervisor
ingress. All test execution and builds run on repository branches in Actions.

## Design

- `esp_native_tools` discovers the running server's tools and exact input schemas;
  an optional name selects one schema. No copied upstream catalog to maintain.
- `esp_native_call` invokes a discovered tool once, preserves its MCP result and
  errors, and advertises conservative write/destructive annotations. No write
  retries or WebSocket fallback after a native call.
- `esp_get_yaml` delegates compatible device YAML reads to native `get_config`,
  retaining its existing response shape. Only a missing endpoint/tool permits
  fallback to WebSocket. Preserve all other existing tools: their filters,
  streaming, wipe controls, firmware flags, and file creation differ upstream.
- Negotiate supported protocol versions, bound time and response size, discover
  per request so add-on upgrades/restarts cannot leave stale capability caches.
- Use the existing HAOS image/runtime for an opt-in live development mode in
  Component E2E. Publish temporary tunnel URLs only encrypted to a caller key.
  Dispatch directly on this owned repo; never touch the personal HA instance.

## Tasks

1. Add protocol/routing/error and tool-surface regression tests. Run them on a
   remote branch and confirm missing-native-support failures before implementing.
2. Implement native discovery/calls and compatible YAML read delegation. Document
   the preferred native editing flow, compatibility, and retained differences;
   bump the additive release to 2.2.0. Run branch PR Validation.
3. Add opt-in HAOS dev mode and native end-to-end scenarios. Launch it on this
   repo, exercise the actual tools through ingress, verify writes/readback and
   stale-write rejection using throwaway configs; clean fixtures and stop it.
4. Review the complete diff, open the PR with the existing template and Closes
   #196, inspect all checks and CodeRabbit threads, and fix substantive findings
   until current-head CI and review are settled. Do not merge.

## Review focus

- Missing endpoint versus authorization, timeout, invalid protocol, or tool error:
  only explicit capability absence allows a legacy read fallback.
- Native errors must remain failures; never repeat an ambiguous write.
- Preserve nested schemas, tool results, pagination, and native stale-write guards.
- Existing HA context, files, streams, and firmware flags remain available.
- No unencrypted dev URLs, credentials, or fixture secrets in logs/artifacts.

## Evidence

Pending remote validation and live HAOS run.
