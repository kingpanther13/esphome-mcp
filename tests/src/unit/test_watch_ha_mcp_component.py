"""Tests for the GitHub-hosted HA-MCP custom-component watcher."""

from __future__ import annotations

import base64
import importlib.util
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = ROOT / "scripts" / "watch_ha_mcp_component.py"
WATCH_WORKFLOW = ROOT / ".github" / "workflows" / "watch-ha-mcp-component.yml"
UPSTREAM_SHA = "c" * 40
MASTER_SHA = "a" * 40
BRANCH_SHA = "b" * 40
RENOVATE_BRANCH = "renovate/ha-mcp-master-runtime-contract-digest"


class _ApiServer(ThreadingHTTPServer):
    """HTTP server with literal GitHub response fixtures and a request log."""

    routes: dict[tuple[str, str], tuple[int, Any]]
    requests: list[tuple[str, str, Any]]


class _ApiHandler(BaseHTTPRequestHandler):
    """Serve deterministic JSON responses without mocking the HTTP client."""

    server: _ApiServer

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._respond(None)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length)) if length else None
        self._respond(body)

    def _respond(self, body: Any) -> None:
        self.server.requests.append((self.command, self.path, body))
        status, payload = self.server.routes.get(
            (self.command, self.path),
            (500, {"message": f"unexpected request: {self.command} {self.path}"}),
        )
        encoded = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        """Keep test output quiet."""


@pytest.fixture
def api_server() -> Iterator[_ApiServer]:
    """Run a real local HTTP boundary for one watcher test."""
    server = _ApiServer(("127.0.0.1", 0), _ApiHandler)
    server.routes = {}
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _load_watcher() -> ModuleType:
    assert SCRIPT_PATH.is_file(), "the HA-MCP component watcher script is missing"
    spec = importlib.util.spec_from_file_location("ha_mcp_component_watcher", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _api_url(server: _ApiServer) -> str:
    host, port = server.server_address
    return f"http://{host}:{port}"


def _contract_payload(sha: str) -> dict[str, str]:
    content = base64.b64encode(f'HA_MCP_MASTER_SHA = "{sha}"\n'.encode()).decode()
    return {"type": "file", "encoding": "base64", "content": content}


def _write_contract(tmp_path: Path, sha: str = MASTER_SHA) -> Path:
    contract = tmp_path / "contract.py"
    contract.write_text(f'HA_MCP_MASTER_SHA = "{sha}"\n')
    return contract


def _configure_environment(
    monkeypatch: pytest.MonkeyPatch,
    server: _ApiServer,
) -> None:
    monkeypatch.setenv("GITHUB_API_URL", _api_url(server))
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "kingpanther13/esphome-mcp")


def _latest_component_path() -> str:
    return (
        "/repos/homeassistant-ai/ha-mcp/commits"
        "?sha=master&path=custom_components%2Fha_mcp_tools&per_page=1"
    )


def _open_pr_path() -> str:
    return (
        "/repos/kingpanther13/esphome-mcp/pulls"
        "?state=open"
        "&head=kingpanther13%3Arenovate%2Fha-mcp-master-runtime-contract-digest"
        "&base=master&per_page=1"
    )


def _branch_contract_path() -> str:
    return (
        "/repos/kingpanther13/esphome-mcp/contents/"
        "custom_components/esphome_mcp/ha_mcp_runtime/contract.py"
        "?ref=renovate%2Fha-mcp-master-runtime-contract-digest"
    )


def _dispatch_path() -> str:
    return (
        "/repos/kingpanther13/esphome-mcp/actions/"
        "workflows/renovate.yml/dispatches"
    )


def _base_routes(server: _ApiServer, *, master_status: str) -> None:
    server.routes.update(
        {
            ("GET", _latest_component_path()): (200, [{"sha": UPSTREAM_SHA}]),
            (
                "GET",
                f"/repos/homeassistant-ai/ha-mcp/compare/{UPSTREAM_SHA}...{MASTER_SHA}",
            ): (200, {"status": master_status}),
        }
    )


def test_stale_snapshot_dispatches_only_the_ha_mcp_renovate_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_server: _ApiServer,
) -> None:
    """Dropping the stale branch must stop the event-driven dependency update."""
    module = _load_watcher()
    contract = _write_contract(tmp_path)
    _configure_environment(monkeypatch, api_server)
    _base_routes(api_server, master_status="behind")
    api_server.routes[("GET", _open_pr_path())] = (200, [])
    api_server.routes[("POST", _dispatch_path())] = (204, None)

    assert module.main(["--contract", str(contract)]) == 0

    posts = [request for request in api_server.requests if request[0] == "POST"]
    assert posts == [
        (
            "POST",
            _dispatch_path(),
            {"ref": "master", "inputs": {"logLevel": "info", "scope": "ha-mcp"}},
        )
    ]


@pytest.mark.parametrize("status", ["ahead", "identical"])
def test_merged_snapshot_containing_latest_component_commit_is_a_noop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_server: _ApiServer,
    status: str,
) -> None:
    """Reversing the containment verdict would dispatch every five minutes."""
    module = _load_watcher()
    contract = _write_contract(tmp_path)
    _configure_environment(monkeypatch, api_server)
    _base_routes(api_server, master_status=status)

    assert module.main(["--contract", str(contract)]) == 0
    assert all(method != "POST" for method, _path, _body in api_server.requests)


def test_open_renovate_branch_containing_latest_component_commit_is_a_noop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_server: _ApiServer,
) -> None:
    """Ignoring the open branch would spam dispatches while its CI is running."""
    module = _load_watcher()
    contract = _write_contract(tmp_path)
    _configure_environment(monkeypatch, api_server)
    _base_routes(api_server, master_status="behind")
    api_server.routes[("GET", _open_pr_path())] = (
        200,
        [{"number": 67, "head": {"ref": RENOVATE_BRANCH}}],
    )
    api_server.routes[("GET", _branch_contract_path())] = (
        200,
        _contract_payload(BRANCH_SHA),
    )
    api_server.routes[
        (
            "GET",
            f"/repos/homeassistant-ai/ha-mcp/compare/{UPSTREAM_SHA}...{BRANCH_SHA}",
        )
    ] = (200, {"status": "ahead"})

    assert module.main(["--contract", str(contract)]) == 0
    assert all(method != "POST" for method, _path, _body in api_server.requests)


def test_new_component_commit_after_open_branch_dispatches_one_refresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_server: _ApiServer,
) -> None:
    """Treating any open branch as current would miss the next dev snapshot."""
    module = _load_watcher()
    contract = _write_contract(tmp_path)
    _configure_environment(monkeypatch, api_server)
    _base_routes(api_server, master_status="behind")
    api_server.routes[("GET", _open_pr_path())] = (
        200,
        [{"number": 67, "head": {"ref": RENOVATE_BRANCH}}],
    )
    api_server.routes[("GET", _branch_contract_path())] = (
        200,
        _contract_payload(BRANCH_SHA),
    )
    api_server.routes[
        (
            "GET",
            f"/repos/homeassistant-ai/ha-mcp/compare/{UPSTREAM_SHA}...{BRANCH_SHA}",
        )
    ] = (200, {"status": "behind"})
    api_server.routes[("POST", _dispatch_path())] = (204, None)

    assert module.main(["--contract", str(contract)]) == 0
    assert sum(method == "POST" for method, _path, _body in api_server.requests) == 1


def test_watcher_workflow_runs_on_github_with_least_privilege() -> None:
    """Removing the cadence, permissions, or token boundary breaks the watcher."""
    assert WATCH_WORKFLOW.is_file(), "the HA-MCP component watcher workflow is missing"
    workflow = yaml.safe_load(WATCH_WORKFLOW.read_text())
    triggers = workflow[True]
    job = workflow["jobs"]["watch"]

    assert triggers["schedule"] == [
        {"cron": "2,7,12,17,22,27,32,37,42,47,52,57 * * * *"}
    ]
    assert "workflow_dispatch" in triggers
    assert workflow["permissions"] == {
        "actions": "write",
        "contents": "read",
        "pull-requests": "read",
    }
    assert workflow["concurrency"] == {
        "group": "ha-mcp-component-watcher",
        "cancel-in-progress": False,
    }
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == 5

    checkout = next(
        step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@")
    )
    watch = next(step for step in job["steps"] if step.get("name") == "Check HA-MCP component")
    assert checkout["uses"] == "actions/checkout@v7"
    assert checkout["with"]["persist-credentials"] is False
    assert watch["run"] == "python scripts/watch_ha_mcp_component.py"
    assert watch["env"] == {
        "GITHUB_TOKEN": "${{ github.token }}",
        "GITHUB_REPOSITORY": "${{ github.repository }}",
    }

