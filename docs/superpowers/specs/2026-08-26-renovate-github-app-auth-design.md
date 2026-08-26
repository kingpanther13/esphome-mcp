# Renovate GitHub App authentication

## Goal

Run CI automatically for ESPHome MCP pull requests created or updated by the
self-hosted Renovate workflow, without using a personal long-lived credential
or changing HA-MCP, `ha-mcp-release-bot`, or Dependabot's identity.

## Problem

The Renovate workflow currently authenticates with the repository's
`GITHUB_TOKEN`. GitHub puts workflows caused by a pull request opened,
synchronized, or reopened with that token into an approval-required state as
recursion protection. Repository workflow permissions and the setting that
allows Actions to create or approve pull requests do not bypass that behavior.

This is separate from the repository's fork-contributor approval policy. The
Renovate actor, `github-actions[bot]`, is already a contributor, and a prior PR
from that actor was merged.

References:

- <https://docs.github.com/en/actions/concepts/security/github_token>
- <https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/making-authenticated-api-requests-with-a-github-app-in-a-github-actions-workflow>
- <https://github.com/renovatebot/renovate/blob/main/lib/modules/platform/github/readme.md>

## Decision

Create a private GitHub App named `ESPHome MCP Renovate`, owned by
`kingpanther13`, and install it only on `kingpanther13/esphome-mcp`. The app has
no active webhook and receives only the repository permissions needed by the
self-hosted Renovate process:

- Administration: read
- Checks: read and write
- Commit statuses: read and write
- Contents: read and write
- Issues: read and write
- Pull requests: read and write
- Workflows: read and write
- Metadata: read (implicit)

The workflow stores the app client ID in the repository variable
`RENOVATE_APP_CLIENT_ID` and the complete PEM private key in the repository
secret `RENOVATE_APP_PRIVATE_KEY`. It uses
`actions/create-github-app-token@v3` to mint a repository-scoped installation
token for each run and passes only that short-lived token to
`renovatebot/github-action`.

No app installation token is persisted. GitHub installation tokens expire
after one hour, and the private key is never written to the repository or job
logs.

## Workflow permissions

The repository default workflow permission returns to read-only. The Renovate
workflow also declares only `contents: read` for its built-in `GITHUB_TOKEN`,
which is used by checkout. All Renovate writes use the dedicated installation
token.

The repository setting allowing GitHub Actions to create and approve pull
requests may remain enabled, but it is not part of this solution and does not
grant the Renovate app any permission.

## Dependabot boundary

Dependabot already operates as GitHub's `dependabot[bot]` App rather than as a
workflow using this repository's `GITHUB_TOKEN`. It must not receive the
Renovate app private key or installation token. Existing Dependabot
configuration and disabled auto-merge scaffolding remain unchanged.

Verification uses prior and future Dependabot PR check runs to confirm that
its App-authored updates continue to start CI normally. No redundant
credential or auto-merge behavior is added for Dependabot.

## Repository changes

The implementation changes only ESPHome MCP:

1. `.github/workflows/renovate.yml` generates the installation token and uses
   it for Renovate.
2. A focused unit test fails if Renovate returns to `GITHUB_TOKEN`, omits token
   generation, or grants write access to its built-in workflow token.
3. The existing runtime-contract design is corrected so it no longer
   attributes approval-required runs to first-time-contributor state.
4. The repository stores the app client ID and private key through GitHub's
   variable and secret APIs. Secret values are never printed.

No HA-MCP repository, HA-MCP workflow, organization app, or
`ha-mcp-release-bot` resource is in the read-write scope of this work.

## Bootstrap and rollout

1. Register the private app through GitHub's manifest flow and confirm its
   requested permissions.
2. Install it on only `kingpanther13/esphome-mcp`.
3. Store the app client ID and private key in the ESPHome repository.
4. Open the workflow change as a user-authored PR and pass the repository's
   normal CI and review gates.
5. Merge the workflow change.
6. Request a rebase/retry of Renovate PR #39 and dispatch Renovate from the
   updated `master` branch.
7. Verify the Renovate commit and PR update are authored by the new App, and
   verify every downstream workflow starts without `action_required`.

If app registration or installation is incomplete, the token-generation step
fails before Renovate runs and makes no dependency-branch changes. The existing
PR remains recoverable through manual approval.

## Testing and acceptance criteria

Local static tests assert the workflow's credential data flow and least-
privilege declaration. Ruff, formatting, metadata validation, and focused unit
tests run locally where supported; the complete unit and E2E matrices run in
CI.

The change is complete only when:

- the app is installed only on ESPHome MCP;
- the variable and secret exist without exposing the private key;
- the workflow no longer passes `secrets.GITHUB_TOKEN` to Renovate;
- the full PR CI and requested review are clean;
- a post-merge Renovate run succeeds with the App installation token;
- PR #39 receives normal queued/in-progress CI runs rather than
  `action_required`; and
- a Dependabot PR remains able to start CI without the Renovate credential.

## Rollback

Restore Renovate's `GITHUB_TOKEN` input, remove the repository variable and
secret, and uninstall or delete the dedicated app. Renovate PRs will again
require manual workflow approval, but dependency state and existing pull
requests remain intact.
