"""Tests for the identities used by automated dependency updates."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
RENOVATE_WORKFLOW = ROOT / ".github" / "workflows" / "renovate.yml"
DEPENDABOT_CONFIG = ROOT / ".github" / "dependabot.yml"


def test_renovate_uses_a_short_lived_github_app_token() -> None:
    """Renovate updates must trigger CI without exposing a long-lived user token."""
    workflow = yaml.safe_load(RENOVATE_WORKFLOW.read_text())
    job = workflow["jobs"]["renovate"]
    steps = job["steps"]
    token_steps = [step for step in steps if step.get("id") == "renovate_app_token"]

    assert len(token_steps) == 1
    token_step = token_steps[0]
    renovate_step = next(step for step in steps if step.get("name") == "Self-hosted Renovate")

    assert job["permissions"] == {"contents": "read"}
    assert token_step["uses"] == "actions/create-github-app-token@v3"
    assert token_step["with"] == {
        "client-id": "${{ vars.RENOVATE_APP_CLIENT_ID }}",
        "private-key": "${{ secrets.RENOVATE_APP_PRIVATE_KEY }}",
        "owner": "${{ github.repository_owner }}",
        "repositories": "${{ github.event.repository.name }}",
        "permission-administration": "read",
        "permission-checks": "write",
        "permission-contents": "write",
        "permission-issues": "write",
        "permission-pull-requests": "write",
        "permission-statuses": "write",
        "permission-workflows": "write",
    }
    checkout_step = next(step for step in steps if step.get("name") == "Checkout")
    assert checkout_step["with"]["persist-credentials"] is False
    assert renovate_step["with"]["token"] == ("${{ steps.renovate_app_token.outputs.token }}")
    assert "secrets.GITHUB_TOKEN" not in RENOVATE_WORKFLOW.read_text()


def test_renovate_rebases_behind_branches_promptly() -> None:
    """A merge into master must wake Renovate rather than leaving branches parked."""
    workflow = yaml.safe_load(RENOVATE_WORKFLOW.read_text())
    # PyYAML resolves the bare `on:` key to the boolean True.
    triggers = workflow[True]

    assert triggers["push"]["branches"] == ["master"]
    assert triggers["schedule"] == [{"cron": "0 * * * *"}]
    assert "workflow_dispatch" in triggers

    # Overlapping push and schedule runs would race rebasing the same branches,
    # and cancelling one mid-rebase can leave a branch half-written.
    assert workflow["concurrency"] == {"group": "renovate", "cancel-in-progress": False}


def test_behind_dependabot_branches_are_updated_with_the_app_token() -> None:
    """Dependabot never rebases clean-but-behind PRs, and GITHUB_TOKEN pushes
    would not re-trigger the required PR checks, so the update must run with
    the app identity."""
    workflow = yaml.safe_load(RENOVATE_WORKFLOW.read_text())
    job = workflow["jobs"]["dependabot-update-branch"]

    token_step = next(step for step in job["steps"] if step.get("id") == "renovate_app_token")
    assert token_step["uses"] == "actions/create-github-app-token@v3"
    assert token_step["with"]["permission-contents"] == "write"
    assert token_step["with"]["permission-pull-requests"] == "write"

    update_step = next(step for step in job["steps"] if "update-branch" in step.get("run", ""))
    assert update_step["env"]["GH_TOKEN"] == "${{ steps.renovate_app_token.outputs.token }}"
    assert 'select(.user.login == "dependabot[bot]")' in update_step["run"]
    assert "secrets.GITHUB_TOKEN" not in yaml.dump(job)


def test_dependabot_does_not_receive_renovate_credentials() -> None:
    """Dependabot keeps its native GitHub App identity and credential boundary."""
    dependabot = DEPENDABOT_CONFIG.read_text()

    assert 'package-ecosystem: "github-actions"' in dependabot
    assert 'package-ecosystem: "pip"' in dependabot
    assert "RENOVATE_APP_CLIENT_ID" not in dependabot
    assert "RENOVATE_APP_PRIVATE_KEY" not in dependabot
