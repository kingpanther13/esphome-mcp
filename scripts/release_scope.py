"""Decide which changes against a base ref actually reach the shipped component.

Both the Renovate bump task and the CI version-bump check import this, so the
two sides of the policy — "bump when a change reaches the component" and
"require a bump when a change reaches the component" — cannot drift apart.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT_PREFIX = "custom_components/esphome_mcp/"


def _git(args: list[str], root: Path) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True)


def component_facing_changes(base_ref: str, root: Path = ROOT) -> list[str]:
    """Return changed component paths, vs ``base_ref``, that ship to users.

    The worktree is compared directly so uncommitted edits count — Renovate's
    post-upgrade tasks run before anything is committed.
    """
    changed = _git(["diff", "--name-only", base_ref], root)
    return [path for path in changed.splitlines() if path.startswith(COMPONENT_PREFIX)]
