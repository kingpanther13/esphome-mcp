# Peer-Owned FastMCP Runtime Design

## Goal

Let ESPHome MCP coexist with HA-MCP without duplicating HA-MCP's exact FastMCP
pin or replacing a live shared runtime. Keep a bounded standalone path for Home
Assistant installations that do not have HA-MCP.

## Context

ESPHome MCP and HA-MCP both run FastMCP in the Home Assistant Core process.
Python's import cache is process-global, so two in-process integrations cannot
load independent versions of the `fastmcp` package safely. The current ESPHome
MCP implementation mirrors HA-MCP's entire direct dependency list and requires
the same exact FastMCP version. That turns every HA-MCP FastMCP patch release
into a lockstep ESPHome MCP release and caused PR #33 to fail while HA-MCP was
still pinned one patch behind.

Vendoring FastMCP does not solve the isolation problem. FastMCP 3.4.7 contains
about 1,315 absolute `fastmcp` imports across 214 Python files, so relocating it
under a private namespace would require a source-rewriting fork. Its external
dependencies, including the MCP SDK, Pydantic, Starlette, Uvicorn, Authlib, and
Cryptography, would remain shared with Home Assistant. Vendoring the complete
dependency closure would duplicate native packages and transfer security and
compatibility ownership to this integration. A subprocess would isolate the
graph, but ESPHome MCP currently passes the live `HomeAssistant` object into its
server and would need a new authenticated RPC boundary. Those approaches are
outside this change.

## Decision

Use one process-wide FastMCP runtime with explicit ownership:

1. An enabled HA-MCP server entry owns initialization. ESPHome MCP waits for
   HA-MCP's background `bringup_task` without cancelling it, verifies HA-MCP's
   manager is running, and then adopts the installed runtime.
2. An installed `ha-mcp` or `ha-mcp-dev` distribution also owns its declared
   FastMCP requirement even when its server entry is inactive. ESPHome MCP does
   not invoke pip over an installed peer's dependency graph.
3. With no peer distribution and no enabled HA-MCP server entry, ESPHome MCP
   owns a standalone `fastmcp>=3.4.5,<4` requirement.
4. Any already-installed FastMCP version that satisfies the effective
   requirement and resolves with Uvicorn is reused without an installer call.
5. Any cached FastMCP root module must expose the same version and file origin
   as the installed `fastmcp-slim` distribution. A mismatch requires a Home
   Assistant restart before ESPHome MCP starts, preventing mixed generations.
6. Only a cold or unloaded standalone runtime may be installed or repaired,
   through Home Assistant's public `async_process_requirements` API.

The local compatibility range deliberately admits HA-MCP's previous 3.4.6 pin,
its current 3.4.7 pin, and later compatible FastMCP 3.x releases. A future
FastMCP 4 adoption must be an explicit ESPHome MCP compatibility change.

## Runtime Flow

`EmbeddedServerManager._async_ensure_package` performs these steps before the
ESPHome MCP worker thread starts:

1. Query `hass.config_entries.async_entries("ha_mcp_tools")` for enabled entries
   whose `entry_type` is `server`.
2. If such an entry exists, wait briefly for its `bringup_task` registration,
   shield-await the task, and require its manager to report `is_running`.
3. Read distribution metadata for `ha-mcp` and `ha-mcp-dev`. More than one peer
   distribution is an ambiguous owner and fails closed. A peer distribution
   that does not declare FastMCP also fails closed.
4. Read the installed FastMCP version and package origin without importing it,
   check that the server modules resolve, and fingerprint any cached root
   module through its existing `__version__` and `__file__` attributes.
5. If cached code cannot be proven to match the installed version and origin,
   require a Home Assistant restart before doing any further runtime work.
6. For a peer-owned runtime, require the installed version to satisfy both the
   peer requirement and ESPHome MCP's supported range. Record the peer's
   effective FastMCP requirement and skip the Home Assistant installer.
7. For a standalone runtime, reuse a compatible installed version. If no
   compatible runtime is loaded, ask Home Assistant to process only the bounded
   standalone FastMCP requirement, then recheck imports and version constraints.
8. If an incompatible or incomplete FastMCP runtime is already loaded, refuse
   in-process replacement and surface the existing restart repair issue.

Waiting for HA-MCP uses `asyncio.shield`: unloading ESPHome MCP may cancel its
own bring-up task but must never cancel HA-MCP's task. If HA-MCP finishes without
a running manager, ESPHome MCP reports a package error instead of falling back
to a competing install.

## Static Safety Contract

The runtime dependency sandbox continues to forbid `sys.modules` mutation and
`importlib.reload`, and continues to require deadlock-safe worker preloading.
Its install contract changes from the mirrored shared-dependency tuple to the
single standalone requirement tuple.

CI fetches HA-MCP's current `master` `pyproject.toml`, extracts its direct
FastMCP requirement, and verifies an exact upstream pin falls inside ESPHome
MCP's supported range. Other HA-MCP direct dependencies are intentionally not
mirrored or compared because the peer-owned path never installs them.

Renovate no longer edits the production FastMCP requirement. Instead it tracks
an exact pin in `tests/fastmcp_canary.txt`. Each FastMCP release opens a canary
PR that installs that release, smoke-tests the API used by ESPHome MCP, and
triggers the peer-free HAOS E2E. The production range remains a compatibility
policy while dependency releases still receive an automatic CI event.

## Failure Handling

- Enabled HA-MCP entry but no registered task: package error after a bounded
  wait, with no pip mutation.
- HA-MCP task cancelled, fails, or finishes without a running manager: package
  error, with no fallback install.
- Both `ha-mcp` and `ha-mcp-dev` installed: package error identifying ambiguous
  ownership.
- Multiple active FastMCP declarations from one peer: package error identifying
  ambiguous ownership.
- Peer requirement missing or incompatible with ESPHome MCP's supported range:
  restart/compatibility error instructing the operator to update the
  integrations.
- Installed version violates the peer requirement: restart/compatibility error.
- Cached FastMCP version or origin differs from installed metadata: restart
  error before the ESPHome worker starts.
- Loaded standalone version is outside the supported range or has incomplete
  imports: restart error and no mutation.
- Cold standalone install fails or produces an incompatible version: package
  error.

## Testing

Unit tests cover peer task waiting, cancellation shielding, peer adoption with
3.4.6 and 3.4.7, inactive installed-peer adoption, ambiguous peers, incompatible
peer and loaded standalone versions, compatible standalone reuse, cold
standalone installation, cached-versus-installed generation drift, package
provenance, duplicate peer declarations, and post-install validation. Sandbox
tests cover the bounded constant, installer argument, and HA-MCP 3.4.6/3.4.7
compatibility. Renovate's canary PR adds an exact-release FastMCP smoke test.

GitHub CI remains the integration authority: Ruff, unit tests, metadata checks,
the fetched HA-MCP compatibility gate, ESPHome host-device E2E, and HAOS embedded
E2E must pass before merge.

## Repository Actions Policy

The repository's fork-PR approval policy is set to
`first_time_contributors_new_to_github`. This permits established external
accounts, including `github-actions[bot]`, to start workflows automatically
while retaining manual approval for accounts that are new to GitHub.
