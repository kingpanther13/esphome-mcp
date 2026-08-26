"""Tests for the identities used by automated dependency updates."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
RENOVATE_WORKFLOW = ROOT / ".github" / "workflows" / "renovate.yml"
DEPENDABOT_CONFIG = ROOT / ".github" / "dependabot.yml"


def test_renovate_uses_a_short_lived_github_app_token() -> None:
    """Renovate updates must trigger CI without exposing a long-lived user token."""
    workflow = yaml.load(RENOVATE_WORKFLOW.read_text(), Loader=yaml.BaseLoader)
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
    }
    assert renovate_step["with"]["token"] == ("${{ steps.renovate_app_token.outputs.token }}")
    assert "secrets.GITHUB_TOKEN" not in RENOVATE_WORKFLOW.read_text()


def test_dependabot_does_not_receive_renovate_credentials() -> None:
    """Dependabot keeps its native GitHub App identity and credential boundary."""
    dependabot = DEPENDABOT_CONFIG.read_text()

    assert 'package-ecosystem: "github-actions"' in dependabot
    assert 'package-ecosystem: "pip"' in dependabot
    assert "RENOVATE_APP_CLIENT_ID" not in dependabot
    assert "RENOVATE_APP_PRIVATE_KEY" not in dependabot
