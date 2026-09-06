# HA-MCP component watcher

## Goal

Wake ESPHome MCP's existing Renovate automation shortly after every commit to
`homeassistant-ai/ha-mcp` that touches `custom_components/ha_mcp_tools/**`.
This includes consecutive development snapshots such as
`v2.1.4-dev.374`, `v2.1.4-dev.375`, and `v2.1.4-dev.376`; the public component
version does not need to change. Multiple commits between polls are coalesced
into an update to the newest master snapshot.

## Repository boundary

All implementation lives in `kingpanther13/esphome-mcp`. The change does not
modify HA-MCP, its integration mirror, their repository settings, or the
ESPHome MCP runtime component under `custom_components/esphome_mcp/**`.

GitHub Actions cannot subscribe directly to another repository's push or
workflow events. A lightweight ESPHome MCP workflow therefore polls GitHub at
five-minute intervals. It is a GitHub-hosted automation job, not runtime Home
Assistant code. The existing twice-daily Renovate schedule remains the
backstop because GitHub may delay or drop scheduled events.

## Detection contract

The watcher asks GitHub for the newest commit on HA-MCP `master` that touched
`custom_components/ha_mcp_tools`. It compares that commit with the immutable
HA-MCP SHA recorded in ESPHome MCP's generated runtime contract.

When Renovate already has an open
`renovate/ha-mcp-master-runtime-contract-digest` pull request, the watcher also
checks the contract on that branch. A component commit is already covered when
it is an ancestor of either the merged contract snapshot or the open Renovate
branch snapshot. This prevents repeated dispatches while the dependency pull
request is waiting for CI or auto-merge, while still dispatching again when a
newer component commit lands during that wait.

API failures, empty results, invalid SHAs, missing contract constants, and
unknown comparison states fail the watcher visibly. A missing Renovate branch
is normal and falls back to the merged contract.

## Renovate dispatch

When neither tracked snapshot contains the newest component commit, the
watcher dispatches `.github/workflows/renovate.yml` on `master` with an
`ha-mcp` scope and `info` logging. The receiving workflow bypasses the normal
new-branch schedule, as manual dispatches already do, but limits Renovate's
package-file discovery to
`custom_components/esphome_mcp/ha_mcp_runtime/contract.py`. The HA-MCP package
rule then performs the existing atomic contract regeneration and, when the
runtime metadata changes, the ESPHome patch version bump. A SHA-only update
keeps the existing no-release behavior. CI, auto-merge, publication, and user
installation still take time after detection.

The repository-wide `prHourlyLimit` remains at Renovate's default of two. An
HA-MCP-scoped dispatch does not discover or open unrelated dependency pull
requests, disables stale-branch pruning to preserve branches omitted from the
partial scan, and skips the unrelated Dependabot grooming job.

## Credentials and permissions

The watcher uses the workflow's short-lived `GITHUB_TOKEN` with only
`actions: write`, `contents: read`, and `pull-requests: read`. It reads the
public HA-MCP repository and reads/dispatches only within ESPHome MCP. It does
not receive the Renovate GitHub App private key. The Renovate workflow keeps
using its existing repository-scoped app token for branch and pull-request
writes.

## Verification

Unit tests exercise the watcher against a local HTTP server that records real
GET and POST requests. They cover a stale merged snapshot, a current merged
snapshot, an open Renovate branch that already contains the component commit,
and a newer component commit arriving after the open branch. Workflow-shape
tests pin the five-minute cadence, least-privilege permissions, scoped dispatch
input, HA-MCP-only Renovate include path, and the Dependabot-job exclusion. PR CI also runs the watcher with
`--check-only` against GitHub to verify real token access without dispatching.
