"""Tests for Renovate's automatic ESPHome release-version update."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_ha_mcp_contract_update_runs_allowed_version_bump() -> None:
    """A future HA-MCP master update must produce a releaseable ESPHome PR."""
    renovate = json.loads((ROOT / "renovate.json").read_text())
    workflow = (ROOT / ".github" / "workflows" / "renovate.yml").read_text()
    contract_rule = next(
        rule for rule in renovate["packageRules"] if rule["description"].startswith("HA-MCP master")
    )

    commands = contract_rule["postUpgradeTasks"]["commands"]
    assert commands == [
        "python scripts/sync_ha_mcp_runtime_contract.py --contract-ref",
        "python scripts/bump_component_version.py origin/master",
    ]
    assert contract_rule["postUpgradeTasks"]["fileFilters"] == [
        "custom_components/esphome_mcp/ha_mcp_runtime/contract.py",
        "custom_components/esphome_mcp/manifest.json",
        "custom_components/esphome_mcp/const.py",
        "pyproject.toml",
    ]

    allowed_match = re.search(
        r"RENOVATE_ALLOWED_COMMANDS:\s*>-\s*\n\s*(\[[^\n]+\])",
        workflow,
    )
    assert allowed_match is not None
    allowed_patterns = json.loads(allowed_match.group(1))
    assert all(
        any(re.fullmatch(pattern, command) for pattern in allowed_patterns) for command in commands
    )
