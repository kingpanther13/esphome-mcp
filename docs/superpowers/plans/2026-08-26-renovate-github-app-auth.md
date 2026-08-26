# Renovate GitHub App Authentication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Authenticate self-hosted Renovate with a dedicated, repository-scoped GitHub App so Renovate PR updates start CI automatically while Dependabot keeps its existing GitHub App identity.

**Architecture:** A private GitHub App installed only on `kingpanther13/esphome-mcp` provides a one-hour installation token minted by `actions/create-github-app-token@v3`. The Renovate workflow consumes that token and reduces its built-in `GITHUB_TOKEN` to read-only. Static tests protect the credential data flow; a post-merge Renovate rebase of PR #39 proves downstream CI starts without manual approval.

**Tech Stack:** GitHub Actions YAML, GitHub App manifest/API, Renovate 44.41.0, Python/pytest static workflow tests, GitHub CLI.

**Spec:** `docs/superpowers/specs/2026-08-26-renovate-github-app-auth-design.md`

## Global Constraints

- Change only `kingpanther13/esphome-mcp`; do not mutate HA-MCP, `homeassistant-ai`, or `ha-mcp-release-bot`.
- The App is private, owned by `kingpanther13`, and installed only on `kingpanther13/esphome-mcp`.
- Store the client ID as `RENOVATE_APP_CLIENT_ID` and the PEM private key as `RENOVATE_APP_PRIVATE_KEY`.
- Never print or commit the private key, manifest conversion response, client secret, or installation token.
- Dependabot keeps its existing GitHub App identity and disabled auto-merge scaffolding.
- Run only locally reliable static/focused tests; use CI for the complete unit and E2E matrices.

---

### Task 1: Protect the Renovate authentication contract

**Files:**
- Create: `tests/src/unit/test_renovate_auth.py`
- Modify: `.github/workflows/renovate.yml`
- Modify: `docs/superpowers/specs/2026-08-24-ha-mcp-master-runtime-contract-design.md`

**Interfaces:**
- Consumes: repository variable `RENOVATE_APP_CLIENT_ID`; repository secret `RENOVATE_APP_PRIVATE_KEY`.
- Produces: step output `steps.renovate_app_token.outputs.token` passed to `renovatebot/github-action`.

- [ ] **Step 1: Write the failing workflow test**

Create a test that loads `.github/workflows/renovate.yml` with `yaml.BaseLoader` and asserts these behavioral invariants:

```python
def test_renovate_uses_a_short_lived_github_app_token() -> None:
    workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["renovate"]
    steps = job["steps"]
    token_step = next(step for step in steps if step.get("id") == "renovate_app_token")
    renovate_step = next(step for step in steps if step.get("name") == "Self-hosted Renovate")

    assert job["permissions"] == {"contents": "read"}
    assert token_step["uses"] == "actions/create-github-app-token@v3"
    assert token_step["with"] == {
        "client-id": "${{ vars.RENOVATE_APP_CLIENT_ID }}",
        "private-key": "${{ secrets.RENOVATE_APP_PRIVATE_KEY }}",
        "owner": "${{ github.repository_owner }}",
        "repositories": "${{ github.event.repository.name }}",
    }
    assert renovate_step["with"]["token"] == (
        "${{ steps.renovate_app_token.outputs.token }}"
    )
    assert "secrets.GITHUB_TOKEN" not in WORKFLOW.read_text()
```

Also assert `.github/dependabot.yml` remains present and the Renovate credential names do not occur in it.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
python -m pytest tests/src/unit/test_renovate_auth.py -q
```

Expected: failure because the token-generation step is absent and Renovate still consumes `secrets.GITHUB_TOKEN`.

- [ ] **Step 3: Implement the minimal workflow change**

Change the workflow job permissions to `contents: read`, insert this step before checkout, and pass its output to Renovate:

```yaml
permissions:
  contents: read

steps:
  - name: Generate Renovate App token
    id: renovate_app_token
    uses: actions/create-github-app-token@v3
    with:
      client-id: ${{ vars.RENOVATE_APP_CLIENT_ID }}
      private-key: ${{ secrets.RENOVATE_APP_PRIVATE_KEY }}
      owner: ${{ github.repository_owner }}
      repositories: ${{ github.event.repository.name }}
```

Set the Renovate action input to:

```yaml
token: ${{ steps.renovate_app_token.outputs.token }}
```

Correct the existing runtime-contract design to explain that App authentication, not contributor history, removes manual approval.

- [ ] **Step 4: Run focused checks and verify GREEN**

Run:

```bash
python -m pytest tests/src/unit/test_renovate_auth.py tests/src/unit/test_renovate_version_bump.py tests/src/unit/test_metadata.py -q
ruff check tests/src/unit/test_renovate_auth.py tests/src/unit/test_renovate_version_bump.py tests/src/unit/test_metadata.py
ruff format --check tests/src/unit/test_renovate_auth.py tests/src/unit/test_renovate_version_bump.py tests/src/unit/test_metadata.py
```

Expected: all selected tests and formatting checks pass.

- [ ] **Step 5: Commit the workflow behavior**

```bash
git add .github/workflows/renovate.yml tests/src/unit/test_renovate_auth.py docs/superpowers/specs/2026-08-24-ha-mcp-master-runtime-contract-design.md
git commit -m "fix(ci): authenticate Renovate with a GitHub App"
```

### Task 2: Register and install the dedicated App

**Files:**
- Create temporarily outside the repository: manifest registration page and callback capture state.
- Do not commit generated App credentials.

**Interfaces:**
- Consumes: GitHub App manifest confirmation and repository installation confirmation.
- Produces: GitHub App client ID and private key stored directly in ESPHome repository configuration.

- [ ] **Step 1: Start a localhost manifest callback**

Generate an unguessable state value, start a bounded localhost server, and prepare a self-submitting manifest registration page for a private `ESPHome MCP Renovate` app with the permissions in the spec and an inactive webhook.

- [ ] **Step 2: Complete App registration confirmation**

Open the local registration page through an Android `VIEW` intent. GitHub redirects back to localhost with a temporary manifest code and matching state.

- [ ] **Step 3: Convert the manifest code without exposing credentials**

Exchange the code through `POST /app-manifests/{code}/conversions`. In one guarded shell operation, save the client ID with:

```bash
gh variable set RENOVATE_APP_CLIENT_ID --repo kingpanther13/esphome-mcp
```

and pipe the PEM directly to:

```bash
gh secret set RENOVATE_APP_PRIVATE_KEY --repo kingpanther13/esphome-mcp
```

Never print the response or PEM. Delete the guarded temporary credential file immediately after both writes.

- [ ] **Step 4: Install only on ESPHome MCP**

Open the App installation page, select only `kingpanther13/esphome-mcp`, and confirm. Read back the installation through the GitHub App API and verify its repository selection contains exactly that repository.

- [ ] **Step 5: Verify repository configuration metadata**

Use `gh variable list` and `gh secret list` to confirm both names exist. These APIs expose names and timestamps, not secret values.

### Task 3: Validate and publish the ESPHome workflow change

**Files:**
- Modify if required by CI/review: only files already in this branch.

**Interfaces:**
- Consumes: committed workflow change and installed App credentials.
- Produces: merged ESPHome pull request.

- [ ] **Step 1: Run locally reliable verification**

Run:

```bash
ruff check custom_components tests scripts
ruff format --check custom_components tests scripts
python -m pytest tests/src/unit/test_renovate_auth.py tests/src/unit/test_renovate_version_bump.py tests/src/unit/test_metadata.py -q
python scripts/validate_release_metadata.py
python scripts/check_version_bump.py origin/master
git diff --check origin/master...HEAD
```

- [ ] **Step 2: Push and create a template-compliant PR**

Push `fix/renovate-github-app-auth`, preserve every pull-request template heading, and disclose that complete unit/E2E validation is delegated to CI.

- [ ] **Step 3: Monitor CI to green**

Account for every check on the exact head. Diagnose failures from logs and fix only verified ESPHome-side causes.

- [ ] **Step 4: Request and account for CodeRabbit**

After CI is green, post `@coderabbitai full review`. Read the full review plus all GraphQL review threads, address every actionable item, and rerun CI for any new head.

- [ ] **Step 5: Squash merge after the final gate**

Verify the exact head is green, mergeable, and has zero unresolved review threads, then squash merge the authorized PR and read back the merge commit on `origin/master`.

### Task 4: Prove Renovate and Dependabot behavior

**Files:**
- No repository changes unless a verified defect requires a new reviewed PR.

**Interfaces:**
- Consumes: merged App-authenticated Renovate workflow.
- Produces: evidence that PR #39 gets automatically running CI and Dependabot remains independent.

- [ ] **Step 1: Restore least-privilege repository default**

Set default workflow permissions to read and preserve the current setting that permits Actions to create and approve pull requests.

- [ ] **Step 2: Request PR #39 rebase and dispatch Renovate**

Tick Renovate's rebase/retry marker if needed and dispatch `renovate.yml` from updated `master` with debug logging.

- [ ] **Step 3: Verify App-authored update and automatic CI**

Read back PR #39's new head, actor, file set, and workflow runs. Acceptance requires queued, in-progress, or successful runs; any `action_required` conclusion fails the test.

- [ ] **Step 4: Verify Dependabot remains independent**

Read back a recent Dependabot-authored PR and its check runs. Confirm it uses `dependabot[bot]`, starts CI normally, and has no reference to either Renovate credential.

- [ ] **Step 5: Report final state**

Report the App slug/installation scope, repository variable/secret names, merged PR and commit, Renovate proof run, PR #39 CI state, Dependabot evidence, and any remaining manual action.
