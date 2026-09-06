# HA-MCP Component Watcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect every HA-MCP custom-component commit using best-effort five-minute polling and dispatch scoped Renovate when needed, retaining the twice-daily fallback.

**Architecture:** A GitHub-hosted ESPHome MCP workflow runs a small standard-library Python watcher. The watcher compares the newest HA-MCP custom-component commit against both the merged contract SHA and any open Renovate contract branch, dispatching the existing Renovate workflow only when both are stale.

**Tech Stack:** GitHub Actions, Python 3.13 standard library, GitHub REST API, pytest, PyYAML, Renovate 44.51.2.

**Spec:** `docs/superpowers/specs/2026-09-06-ha-mcp-component-watcher-design.md`

## Global Constraints

- Make no changes, pull requests, dispatches, or settings changes in `homeassistant-ai/ha-mcp`, `homeassistant-ai/ha-mcp-integration`, or `ha-mcp-release-bot`.
- Do not modify runtime files under `custom_components/esphome_mcp/**` in this implementation pull request.
- Detect every commit touching `custom_components/ha_mcp_tools/**`, including consecutive `-dev.N` snapshots with an unchanged public component version.
- Keep Renovate's repository-wide `prHourlyLimit` unchanged at its default of two.
- Keep the existing twice-daily Renovate schedule as a fallback.
- Run verification through GitHub CI; do not execute local project tests, imports, Ruff, or formatting commands.

---

### Task 1: Specify watcher behavior and Renovate scope

**Files:**
- Create: `tests/src/unit/test_watch_ha_mcp_component.py`
- Modify: `tests/src/unit/test_renovate_auth.py`

**Interfaces:**
- Consumes: the existing generated `HA_MCP_MASTER_SHA`, the fixed Renovate branch name, and `.github/workflows/renovate.yml`.
- Produces: executable expectations for `watch_ha_mcp_component.main()` and the `workflow_dispatch.inputs.scope` contract.

- [x] **Step 1: Write failing watcher tests**

  Build a local `ThreadingHTTPServer` fixture that returns complete GitHub REST
  shapes and records requests. Call `main()` with literal environment values
  and a temporary contract containing a hand-written 40-character SHA. Assert:

  - a `behind` comparison posts exactly
    `{"ref":"master","inputs":{"logLevel":"info","scope":"ha-mcp"}}` to
    `/actions/workflows/renovate.yml/dispatches`;
  - `ahead` and `identical` mean the merged snapshot contains the component
    commit and produce no POST;
  - an open Renovate branch whose comparison is `ahead` produces no POST; and
  - a `behind` branch comparison dispatches once for a newer component commit.

- [x] **Step 2: Write failing workflow-contract tests**

  Parse both workflows with PyYAML and assert the watcher cadence, permissions,
  concurrency, checkout credential isolation, command, and token environment.
  Assert Renovate accepts `scope` values `all` and `ha-mcp`, maps `ha-mcp` to
  `RENOVATE_INCLUDE_PATHS=["custom_components/esphome_mcp/ha_mcp_runtime/contract.py"]`,
  leaves all other invocations unrestricted, preserves unrelated branches with
  `RENOVATE_PRUNE_STALE_BRANCHES=false`, and skips Dependabot grooming for
  the scoped dispatch.

- [x] **Step 3: Push the test-only commit and verify red CI**

  Commit the tests and design/plan documents, push the branch, and open a draft
  pull request. Wait for the unit-test job to fail because the watcher workflow,
  script, and Renovate scope do not exist. Confirm the failure is caused by
  those missing production artifacts rather than test syntax or fixtures.

### Task 2: Implement the watcher and scoped Renovate dispatch

**Files:**
- Create: `scripts/watch_ha_mcp_component.py`
- Create: `.github/workflows/watch-ha-mcp-component.yml`
- Modify: `.github/workflows/renovate.yml`
- Modify: `.github/workflows/pr.yml`

**Interfaces:**
- Consumes: `GITHUB_API_URL`, `GITHUB_TOKEN`, `GITHUB_REPOSITORY`, a contract path, and GitHub's commits, compare, pulls, contents, and workflow-dispatch endpoints.
- Produces: `main(argv: list[str] | None = None) -> int`; exit zero after either a no-op or one successful dispatch, and exit nonzero with an actionable error on invalid data or API failure.

- [x] **Step 1: Implement the API client and decision logic**

  Use `urllib.request` with 30-second timeouts and GitHub JSON headers. Validate
  every SHA with `[0-9a-f]{40}`. Read `HA_MCP_MASTER_SHA` from the local contract
  with `ast`, fetch the newest HA-MCP commit for
  `custom_components/ha_mcp_tools`, and treat GitHub compare statuses `ahead`
  and `identical` as contained. When merged master is stale, list the one open
  Renovate PR and repeat the comparison using its contract SHA. Dispatch once
  only when neither snapshot contains the newest component commit.

- [x] **Step 2: Add the GitHub-hosted watcher workflow**

  Schedule the workflow at minutes `2,7,12,17,22,27,32,37,42,47,52,57`, add a
  manual trigger, use a non-cancelling watcher concurrency group, grant only
  `actions: write`, `contents: read`, and `pull-requests: read`, check out with
  `persist-credentials: false`, and run the watcher with the built-in token.

- [x] **Step 3: Add the HA-MCP Renovate scope**

  Add a `scope` choice input defaulting to `all`. For `ha-mcp`, set
  `RENOVATE_INCLUDE_PATHS` to a JSON array containing only the contract path;
  for all other events set it to `[]`. Preserve the existing schedule-bypass
  expression, disable stale-branch pruning during the scoped run, and make
  `dependabot-groom` conditional on `scope != 'ha-mcp'`. Add a read-only PR CI
  smoke check using `python scripts/watch_ha_mcp_component.py --check-only`.

- [ ] **Step 4: Push the implementation and verify green CI**

  Commit and push the production changes. Wait for every required GitHub check
  on the exact head SHA, inspect any failing job logs, and fix only verified
  failures. Do not run the project locally.

### Task 3: Review and handoff

**Files:**
- Modify if required by verified feedback: only files already listed in Tasks 1-2.

**Interfaces:**
- Consumes: the exact draft pull-request head, GitHub check results, and existing automated review findings.
- Produces: a draft ESPHome MCP pull request with all required checks green and no unresolved actionable automated-review findings.

- [ ] **Step 1: Audit the final diff boundary**

  Confirm `git diff origin/master...HEAD --name-only` contains no path under
  `custom_components/esphome_mcp/**` and no HA-MCP repository mutation occurred.

- [ ] **Step 2: Address existing automated-review findings**

  Read each finding against the exact head and implementation contract. Apply
  only technically valid fixes, reply with evidence, and resolve addressed
  threads. Do not manually request additional reviews.

- [ ] **Step 3: Reverify and report**

  Re-check every required GitHub status on the final SHA and leave the pull
  request in draft state. Report the pull request, exact checks, five-minute
  polling caveat, and the fact that activation occurs only after merge.
