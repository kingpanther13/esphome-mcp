# HA-MCP master runtime contract

## Goal

Keep ESPHome MCP's in-process dependency graph in exact lockstep with both
surfaces developed in HA-MCP: the Python server and the Home Assistant custom
component. HA-MCP does not need to change or participate at runtime.

## Source of truth

One immutable commit from `homeassistant-ai/ha-mcp` master owns the contract.
The generated `ha_mcp_runtime/contract.py` records:

- the 40-character master commit SHA;
- the HA-MCP server project version and complete direct dependency list;
- the HA-MCP component manifest version and requirements;
- the matching `COMPONENT_VERSION` constant; and
- the exact FastMCP requirement used for runtime reporting.

The package contains metadata only. It does not copy or import HA-MCP's server,
tools, authentication code, or vendored websockets tree.

## Runtime behavior

ESPHome MCP always targets the generated server dependency tuple, whether or
not HA-MCP is installed.

1. If an enabled HA-MCP component entry exists, its loaded manifest version must
   match the generated component version.
2. If that component has an enabled server entry, ESPHome waits read-only for
   HA-MCP's published bring-up task and never invokes pip in that path.
3. If `ha-mcp` or `ha-mcp-dev` distribution metadata exists, its complete
   direct requirement set must match the generated server requirements.
4. The installed dependency graph is audited recursively, including dependency
   extras such as `httpx[socks]`.
5. A complete importable graph is reused without invoking the requirements
   manager.
6. A missing graph is installed through Home Assistant's public,
   process-locked `async_process_requirements` API.
7. An HA-MCP metadata mismatch fails only ESPHome MCP and never invokes pip.
8. A mismatch after FastMCP has entered `sys.modules` requests a Home Assistant
   restart and never replaces live process-global packages.

The peer wait is observation only: ESPHome never writes HA-MCP data, cancels its
task, acquires a lease in HA-MCP, or changes HA-MCP code.

The existing loaded-module version and origin fingerprint remains mandatory, so
new distribution metadata cannot be mixed with cached code from an older
generation.

## Automation ownership

Dependabot continues to own GitHub Actions and declared Python development/test
dependencies. Renovate alone owns the HA-MCP master SHA through its `git-refs`
datasource. Renovate admits new branches during the 5 AM and 5 PM Eastern
hours and on manual dispatch. Every push to master still wakes Renovate to
rebase existing branches; Dependabot keeps its weekly Thursday schedule.

When master advances, Renovate updates the SHA and runs
`scripts/sync_ha_mcp_runtime_contract.py --contract-ref` before committing.
That regenerates the server and component metadata atomically in one PR, then
`scripts/bump_component_version.py origin/master` patch-bumps all three
ESPHome release-version sources. The bump is idempotent against the base
branch, so Renovate retries do not create additional releases. The matching
package rule also adds the required user-facing release-note section to the
generated PR body. The old direct FastMCP canary manager is removed, so FastMCP
cannot update outside the HA-MCP snapshot.

PR and release CI run three independent gates:

- static runtime mutation/install sandbox checks;
- a remote regeneration check against the pinned HA-MCP commit; and
- unit plus HAOS embedded E2E assertions of the resolved FastMCP version and
  reported HA-MCP server/component snapshot.

Renovate authenticates as a dedicated, repository-scoped GitHub App. Its
short-lived installation token allows pull-request updates to start CI
normally. The workflow's built-in `GITHUB_TOKEN` remains read-only; using it
for Renovate writes would put downstream runs into GitHub's approval-required
recursion-protection state. Dependabot keeps its separate GitHub App identity
and never receives the Renovate credentials.

## HA-MCP component fixes

The post-release HA-MCP component work was reviewed by concern. Its generic
dependency-diagnostics idea applies here and is represented by the recursive
installed-graph audit. OAuth, YAML/file tools, and HA-MCP websocket API changes
are domain-specific and are not copied into ESPHome MCP.
