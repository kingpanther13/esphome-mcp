"""Tests for Renovate's automatic ESPHome release-version update."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_vendored_runtime_update_runs_allowed_version_bump() -> None:
    """A vendored FastMCP / MCP SDK bump must produce a releaseable ESPHome PR."""
    renovate = json.loads((ROOT / "renovate.json").read_text())
    workflow = (ROOT / ".github" / "workflows" / "renovate.yml").read_text()
    vendor_rule = next(
        rule for rule in renovate["packageRules"] if rule["description"].startswith("Vendored")
    )

    commands = vendor_rule["postUpgradeTasks"]["commands"]
    assert commands == [
        "python scripts/vendor_fastmcp.py",
        "python scripts/bump_component_version.py origin/master",
    ]
    assert vendor_rule["postUpgradeTasks"]["fileFilters"] == [
        "custom_components/esphome_mcp/**",
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
