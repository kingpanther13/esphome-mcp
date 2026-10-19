"""Tests for release notes on automated vendored-runtime updates."""

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


def test_vendored_runtime_update_supplies_publishable_release_notes() -> None:
    """The automatic version bump must not create an unpublishable release PR."""
    renovate = json.loads((ROOT / "renovate.json").read_text())
    vendor_rule = next(
        rule for rule in renovate["packageRules"] if rule["description"].startswith("Vendored")
    )
    release_notes = _load_release_notes()

    body = "\n\n".join(vendor_rule["prBodyNotes"])

    assert release_notes.extract_release_notes(body) == (
        "- Updated ESPHome MCP's bundled FastMCP / MCP SDK runtime."
    )
