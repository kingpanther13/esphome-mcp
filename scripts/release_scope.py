"""Decide which changes against a base ref actually reach the shipped component.

Both the Renovate bump task and the CI version-bump check import this, so the
two sides of the policy — "bump when a change reaches the component" and
"require a bump when a change reaches the component" — cannot drift apart.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT_PREFIX = "custom_components/esphome_mcp/"
CONTRACT_PATH = "custom_components/esphome_mcp/ha_mcp_runtime/contract.py"
_MASTER_SHA_RE = re.compile(r'^HA_MCP_MASTER_SHA = "[0-9a-f]{40}"$', re.MULTILINE)


def _git(args: list[str], root: Path) -> str:
    # No strip: `git show` output must byte-match the worktree file.
    return subprocess.check_output(["git", *args], cwd=root, text=True)


def _mask_master_sha(source: str) -> str | None:
    masked, count = _MASTER_SHA_RE.subn('HA_MCP_MASTER_SHA = "<masked>"', source)
    if count != 1:
        return None
    return masked


def _contract_change_is_sha_only(base_ref: str, root: Path) -> bool:
    try:
        base_source = _git(["show", f"{base_ref}:{CONTRACT_PATH}"], root)
    except subprocess.CalledProcessError:
        return False
    try:
        current_source = (root / CONTRACT_PATH).read_text()
    except OSError:
        return False
    base_masked = _mask_master_sha(base_source)
    current_masked = _mask_master_sha(current_source)
    if base_masked is None or current_masked is None:
        return False
    return base_masked == current_masked


def component_facing_changes(base_ref: str, root: Path = ROOT) -> list[str]:
    """Return changed component paths, vs ``base_ref``, that ship to users.

    The worktree is compared directly so uncommitted edits count — Renovate's
    post-upgrade tasks run before anything is committed. A contract.py change
    whose only difference is the pinned master SHA is excluded: the SHA feeds
    log strings only, so a server- or CI-only move of the upstream ha-mcp
    repository does not change what this component installs or runs.
    """
    changed = _git(["diff", "--name-only", base_ref], root)
    paths = [path for path in changed.splitlines() if path.startswith(COMPONENT_PREFIX)]
    if CONTRACT_PATH in paths and _contract_change_is_sha_only(base_ref, root):
        paths.remove(CONTRACT_PATH)
    return paths
