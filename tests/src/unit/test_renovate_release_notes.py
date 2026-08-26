"""Tests for release notes on automated HA-MCP contract updates."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[3]
RELEASE_NOTES_SCRIPT = ROOT / "scripts" / "release_notes.py"


def _load_release_notes() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_notes", RELEASE_NOTES_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ha_mcp_contract_update_supplies_publishable_release_notes() -> None:
    """The automatic version bump must not create an unpublishable release PR."""
    renovate = json.loads((ROOT / "renovate.json").read_text())
    contract_rule = next(
        rule for rule in renovate["packageRules"] if rule["description"].startswith("HA-MCP master")
    )
    release_notes = _load_release_notes()

    body = "\n\n".join(contract_rule["prBodyNotes"])

    assert release_notes.extract_release_notes(body) == (
        "- Updated ESPHome MCP's embedded runtime contract to the latest HA-MCP `master` snapshot."
    )
