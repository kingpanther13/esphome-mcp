"""Dispatch ESPHome Renovate when an HA-MCP component commit is not covered."""

from __future__ import annotations

import argparse
import ast
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "custom_components/esphome_mcp/ha_mcp_runtime/contract.py"
UPSTREAM = "homeassistant-ai/ha-mcp"
RENOVATE_BRANCH = "renovate/ha-mcp-master-runtime-contract-digest"


def _sha(value: Any) -> str:
    """Reject malformed commit identifiers before constructing API paths."""
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ValueError("GitHub or the contract returned an invalid commit SHA")
    return value


def _contract_sha(source: str) -> str:
    """Read the generated pin as data, without executing component code."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign):
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        else:
            continue
        if "HA_MCP_MASTER_SHA" in names and isinstance(node.value, ast.Constant):
            return _sha(node.value.value)
    raise ValueError("Contract does not define HA_MCP_MASTER_SHA")


class GitHub:
    """Small REST client; all writes target this repository's Renovate dispatch."""

    def __init__(self) -> None:
        self.api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        self.repository = os.environ["GITHUB_REPOSITORY"]
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository) is None:
            raise ValueError("GITHUB_REPOSITORY must be owner/repository")
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "User-Agent": "esphome-mcp-component-watcher",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def request(self, path: str, *, body: dict[str, Any] | None = None) -> Any:
        """Use a bounded HTTP request and surface API failures to the workflow."""
        data = None if body is None else json.dumps(body).encode()
        headers = dict(self.headers)
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{self.api}/repos/{path}", data=data, headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            content = response.read()
        return json.loads(content) if content else None

    def contains(self, snapshot: str, component: str) -> bool:
        """GitHub compares base...head: an ahead head contains the base commit."""
        if snapshot == component:
            return True
        payload = self.request(f"{UPSTREAM}/compare/{component}...{snapshot}")
        status = payload.get("status") if isinstance(payload, dict) else None
        if status not in {"ahead", "identical", "behind", "diverged"}:
            raise ValueError(f"Unexpected GitHub comparison status: {status!r}")
        return status in {"ahead", "identical"}

    def open_snapshot(self) -> str | None:
        """Observe an open Renovate update; tolerate its branch disappearing."""
        owner = self.repository.split("/", 1)[0]
        query = urllib.parse.urlencode(
            {
                "state": "open",
                "head": f"{owner}:{RENOVATE_BRANCH}",
                "base": "master",
                "per_page": 1,
            }
        )
        pulls = self.request(f"{self.repository}/pulls?{query}")
        if not isinstance(pulls, list):
            raise ValueError("GitHub returned an invalid pull-request list")
        if not pulls:
            return None
        query = urllib.parse.urlencode({"ref": RENOVATE_BRANCH})
        try:
            payload = self.request(f"{self.repository}/contents/{CONTRACT}?{query}")
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return None
            raise
        if not isinstance(payload, dict) or payload.get("encoding") != "base64":
            raise ValueError("GitHub returned invalid contract contents")
        content = payload.get("content")
        if not isinstance(content, str):
            raise ValueError("GitHub returned missing contract contents")
        # GitHub wraps base64 content in lines; validate after removing whitespace.
        source = base64.b64decode("".join(content.split()), validate=True).decode()
        return _contract_sha(source)


def main(argv: list[str] | None = None) -> int:
    """Check every component commit, including unchanged-version dev snapshots."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, default=ROOT / CONTRACT)
    parser.add_argument("--check-only", action="store_true", help="Report without dispatching")
    args = parser.parse_args(argv)
    try:
        merged = _contract_sha(args.contract.read_text())
        github = GitHub()
        query = urllib.parse.urlencode(
            {"sha": "master", "path": "custom_components/ha_mcp_tools", "per_page": 1}
        )
        commits = github.request(f"{UPSTREAM}/commits?{query}")
        if not isinstance(commits, list) or not commits or not isinstance(commits[0], dict):
            raise ValueError("GitHub returned no HA-MCP component commit")
        component = _sha(commits[0].get("sha"))
        print(f"Latest HA-MCP component commit: {component}; merged contract: {merged}")
        if github.contains(merged, component):
            print("Merged contract already contains the latest component commit.")
            return 0
        pending = github.open_snapshot()
        if pending and github.contains(pending, component):
            print(f"Open Renovate update already contains the component commit: {pending}")
            return 0
        if args.check_only:
            print("HA-MCP update needed; check-only mode skips dispatch.")
            return 0
        github.request(
            f"{github.repository}/actions/workflows/renovate.yml/dispatches",
            body={"ref": "master", "inputs": {"logLevel": "info", "scope": "ha-mcp"}},
        )
        print(f"Dispatched HA-MCP-only Renovate discovery for component commit {component}.")
        return 0
    except (OSError, ValueError, KeyError, SyntaxError) as err:
        print(f"ERROR: HA-MCP component watcher failed: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
