# Peer-Owned FastMCP Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ESPHome MCP adopt HA-MCP's FastMCP dependency when present and use a bounded FastMCP 3.x standalone runtime otherwise.

**Architecture:** The component detects an enabled HA-MCP server entry, waits for its background bring-up, and treats installed `ha-mcp` distribution metadata as authoritative. Only installations without a peer may invoke Home Assistant's requirement manager, and only with `fastmcp>=3.4.5,<4`; loaded incompatible runtimes always fail closed.

**Tech Stack:** Python 3.13, Home Assistant config-entry APIs, `importlib.metadata`, `packaging`, pytest, Ruff, GitHub Actions, HAOS E2E.

**Spec:** `docs/superpowers/specs/2026-08-24-peer-owned-fastmcp-runtime-design.md`

## Global Constraints

- The standalone requirement is exactly `fastmcp>=3.4.5,<4`.
- ESPHome MCP never installs or mirrors HA-MCP's other direct dependencies.
- An installed `ha-mcp` or `ha-mcp-dev` distribution is an authoritative owner.
- An enabled HA-MCP server entry must complete bring-up before ESPHome MCP adopts the runtime.
- Waiting for HA-MCP must use `asyncio.shield` so ESPHome MCP cancellation cannot cancel HA-MCP.
- Runtime code must not mutate `sys.modules` or call `importlib.reload`.
- Runtime installation must use Home Assistant's public `async_process_requirements` API.
- `websockets` remains absent from ESPHome MCP's runtime requirement list.
- GitHub CI and HAOS E2E are the authoritative integration validation.

---

### Task 1: Specify runtime ownership behavior with failing tests

**Files:**
- Modify: `tests/src/unit/test_embedded_server_dependencies.py`

**Interfaces:**
- Consumes: Existing `EmbeddedServerManager._async_ensure_package()` test harness.
- Produces: Behavioral requirements for `_async_wait_for_ha_mcp_owner()`, `_installed_peer_fastmcp_specs()`, `_version_satisfies_requirement()`, and standalone installation.

- [ ] **Step 1: Extend the Home Assistant fake with peer entries and domain data**

Add `async_entries(domain)` to `_FakeConfigEntries`, allow `_FakeHass` to receive entries, and give it a mutable `data` mapping. Peer fixtures use complete entry data:

```python
SimpleNamespace(
    data={"entry_type": "server"},
    disabled_by=None,
)
```

- [ ] **Step 2: Add peer-owner tests**

Add tests that prove:

```python
# HA-MCP 3.4.6 and 3.4.7 both satisfy ESPHome MCP's supported range.
assert module._version_satisfies_requirement("3.4.6", module.STANDALONE_FASTMCP_SPEC)
assert module._version_satisfies_requirement("3.4.7", module.STANDALONE_FASTMCP_SPEC)

# A running enabled peer records its exact requirement and never invokes pip.
assert process_calls == []
assert hass.config_entries.updated == {
    module.DATA_LAST_PIP_SPEC: "fastmcp==3.4.7"
}
```

Cover an in-flight peer task, a completed running peer, a failed peer bring-up,
an inactive installed peer, both peer distributions installed, a peer without a
FastMCP declaration, a peer pin outside the supported range, and an installed
version that violates the peer pin.

- [ ] **Step 3: Add standalone-owner tests**

Prove a compatible installed FastMCP is reused without an installer call, a cold
runtime invokes the requirement manager with only
`["fastmcp>=3.4.5,<4"]`, a loaded incompatible runtime is never replaced, and
post-install validation rejects a resolver result outside the range.

- [ ] **Step 4: Run the focused test and verify RED**

Run:

```bash
pytest -q tests/src/unit/test_embedded_server_dependencies.py
```

Expected: failures because the range constant and peer-owner functions do not
exist and the current exact-pin path still invokes pip.

- [ ] **Step 5: Commit the failing behavioral specification**

```bash
git add tests/src/unit/test_embedded_server_dependencies.py
git commit -m "test: specify peer-owned FastMCP runtime"
```

### Task 2: Implement peer and standalone ownership

**Files:**
- Modify: `custom_components/esphome_mcp/const.py`
- Modify: `custom_components/esphome_mcp/embedded_server.py`

**Interfaces:**
- Consumes: Task 1 behavioral tests.
- Produces:
  - `STANDALONE_FASTMCP_SPEC: str`
  - `STANDALONE_RUNTIME_REQUIREMENTS: tuple[str, ...]`
  - `EmbeddedServerManager._async_wait_for_ha_mcp_owner() -> bool`
  - `_installed_peer_fastmcp_specs() -> dict[str, str | None]`
  - `_version_satisfies_requirement(version: str | None, requirement: str) -> bool`

- [ ] **Step 1: Replace mirrored constants with ownership constants**

Define:

```python
HA_MCP_COMPAT_REF = "master"
STANDALONE_FASTMCP_SPEC = "fastmcp>=3.4.5,<4"
STANDALONE_RUNTIME_REQUIREMENTS = (STANDALONE_FASTMCP_SPEC,)
HA_MCP_DOMAIN = "ha_mcp_tools"
HA_MCP_ENTRY_TYPE_KEY = "entry_type"
HA_MCP_SERVER_ENTRY_TYPE = "server"
HA_MCP_BRINGUP_TASK_KEY = "bringup_task"
HA_MCP_MANAGER_KEY = "manager"
```

Remove `DEFAULT_PIP_SPEC`, `HA_OWNED_RUNTIME_REQUIREMENTS`, and
`SHARED_RUNTIME_REQUIREMENTS`. Initialize the manager's effective `_pip_spec`
with `STANDALONE_FASTMCP_SPEC`.

- [ ] **Step 2: Implement HA-MCP entry synchronization**

Add `_async_wait_for_ha_mcp_owner()` that returns `False` when no enabled server
entry exists. Otherwise it polls for HA-MCP's task for a bounded interval,
shield-awaits it, and returns `True` only when the peer manager has
`is_running is True`. Convert peer cancellation/failure/missing-manager outcomes
to `EmbeddedServerError(kind="package")` without invoking pip.

- [ ] **Step 3: Implement peer metadata selection**

For each of `ha-mcp` and `ha-mcp-dev`, read `metadata.requires()`. Preserve an
installed distribution even when its dependency list is empty by mapping it to
`None`. Parse active base requirements with `packaging.Requirement` and extract
only canonical `fastmcp`. Reject multiple installed peers as ambiguous.

- [ ] **Step 4: Implement requirement satisfaction**

Use `Requirement(requirement).specifier.contains(Version(version), prereleases=True)`.
Invalid requirements, invalid versions, URL requirements, wrong distribution
names, and empty specifiers return `False`.

- [ ] **Step 5: Replace the exact-pin ensure path**

The peer branch validates the installed version against the peer requirement and
the standalone supported range, verifies imports, records the peer requirement,
and returns without `async_process_requirements`.

The standalone branch reuses a compatible importable installed runtime. If
repair is needed and FastMCP is loaded, raise a restart error. Otherwise call:

```python
await async_process_requirements(
    self._hass,
    f"ESPHome MCP server ({STANDALONE_FASTMCP_SPEC})",
    list(STANDALONE_RUNTIME_REQUIREMENTS),
    is_built_in=False,
)
```

Then recheck importability and range satisfaction before recording the
standalone spec.

- [ ] **Step 6: Run the focused test and verify GREEN**

Run:

```bash
pytest -q tests/src/unit/test_embedded_server_dependencies.py
```

Expected: all tests pass.

- [ ] **Step 7: Commit runtime ownership**

```bash
git add custom_components/esphome_mcp/const.py custom_components/esphome_mcp/embedded_server.py
git commit -m "fix: adopt peer-owned FastMCP runtime"
```

### Task 3: Replace exact parity CI with compatibility CI

**Files:**
- Modify: `tests/src/unit/test_runtime_dependency_sandbox.py`
- Modify: `scripts/check_runtime_dependency_sandbox.py`

**Interfaces:**
- Consumes: `STANDALONE_FASTMCP_SPEC`, `STANDALONE_RUNTIME_REQUIREMENTS`, and `HA_MCP_COMPAT_REF`.
- Produces: `validate_ha_mcp_fastmcp_compatibility(ha_mcp_pyproject, const_path=CONST_PATH) -> list[str]`.

- [ ] **Step 1: Rewrite sandbox tests first**

Remove tests that require full dependency parity. Add tests proving the
repository constants pass, an exact pin is rejected as a standalone policy, an
unbounded FastMCP requirement is rejected, Uvicorn/websockets additions are
rejected, and HA-MCP pins 3.4.6 and 3.4.7 pass while 3.3.9 and 4.0.0 fail.

- [ ] **Step 2: Run the sandbox tests and verify RED**

Run:

```bash
pytest -q tests/src/unit/test_runtime_dependency_sandbox.py
```

Expected: failures because the old checker requires exact full parity and the
old tuple name.

- [ ] **Step 3: Implement the narrowed sandbox contract**

Validate a static one-item `STANDALONE_RUNTIME_REQUIREMENTS` tuple containing
the bounded FastMCP range. Update the installer AST rule to require exactly that
tuple. Replace full dependency parity with extraction of HA-MCP's FastMCP
requirement and validation that its exact pin falls within the local lower and
upper bounds. Preserve all module-cache and worker-import protections.

- [ ] **Step 4: Run the sandbox tests and script**

Run:

```bash
pytest -q tests/src/unit/test_runtime_dependency_sandbox.py
python scripts/check_runtime_dependency_sandbox.py
```

Expected: all tests pass and the script prints `Runtime dependency sandbox passed.`

- [ ] **Step 5: Commit the CI contract**

```bash
git add scripts/check_runtime_dependency_sandbox.py tests/src/unit/test_runtime_dependency_sandbox.py
git commit -m "ci: validate FastMCP compatibility range"
```

### Task 4: Remove pin automation and release the behavior change

**Files:**
- Modify: `renovate.json`
- Modify: `README.md`
- Modify: `pyproject.toml`
- Modify: `custom_components/esphome_mcp/manifest.json`
- Modify: `custom_components/esphome_mcp/const.py`

**Interfaces:**
- Consumes: The peer-owned runtime behavior from Tasks 2 and 3.
- Produces: Release version `0.1.9` and user-facing ownership documentation.

- [ ] **Step 1: Remove FastMCP Renovate automation**

Delete the custom manager that edits `DEFAULT_PIP_SPEC` and its FastMCP package
rule. Keep the HAOS and Home Assistant Core managers unchanged.

- [ ] **Step 2: Update runtime documentation**

Explain that HA-MCP owns FastMCP when installed, standalone ESPHome MCP accepts
compatible FastMCP 3.x, and incompatible loaded runtimes require component
updates plus a Home Assistant restart. Remove the statement that CI enforces an
exact lockstep pin.

- [ ] **Step 3: Bump release metadata to 0.1.9**

Set `project.version`, manifest `version`, and `const.VERSION` to `0.1.9`. Update
the README's example latest release tag from `v0.1.8` to `v0.1.9`.

- [ ] **Step 4: Run metadata and focused unit validation**

Run:

```bash
python scripts/validate_release_metadata.py
pytest -q tests/src/unit/test_metadata.py
```

Expected: metadata validation succeeds and all metadata tests pass.

- [ ] **Step 5: Commit release-facing changes**

```bash
git add renovate.json README.md pyproject.toml custom_components/esphome_mcp/manifest.json custom_components/esphome_mcp/const.py
git commit -m "chore: release peer-owned runtime support"
```

### Task 5: Verify, publish, and observe CI

**Files:**
- Verify: all changed files
- GitHub write: branch `fix/peer-owned-fastmcp-runtime` and a new pull request

**Interfaces:**
- Consumes: All prior tasks.
- Produces: A pull request authored by `kingpanther13` with automated CI/E2E.

- [ ] **Step 1: Run local focused verification**

Run:

```bash
pytest -q tests/src/unit/test_embedded_server_dependencies.py tests/src/unit/test_runtime_dependency_sandbox.py tests/src/unit/test_metadata.py
python scripts/check_runtime_dependency_sandbox.py
python scripts/validate_release_metadata.py
ruff check custom_components tests scripts
ruff format --check custom_components tests scripts
git diff --check origin/master...HEAD
```

Expected: every command exits zero.

- [ ] **Step 2: Audit the final diff and history**

Confirm no `sys.modules` mutation, direct package installer, exact FastMCP pin,
FastMCP Renovate manager, or unrelated file change remains. Confirm every commit
uses `kingpanther13 <kingpanther13@users.noreply.github.com>`.

- [ ] **Step 3: Verify GitHub identity and push**

```bash
gh api user --jq .login
git push -u origin fix/peer-owned-fastmcp-runtime
```

The identity command must print `kingpanther13` before the push.

- [ ] **Step 4: Open the pull request**

Create a PR against `master` whose body explains the peer-owned runtime, why
vendoring was rejected, the automatic Actions approval policy change, focused
local verification, and a `Release note: ...` line for version `0.1.9`.

- [ ] **Step 5: Monitor GitHub checks**

Use `gh pr checks --watch` and inspect every failing run with `gh run view --log-failed`.
Address code failures on the same branch. Treat HAOS infrastructure failures as
infrastructure only when the run logs prove the failure occurred outside the
changed runtime path.
