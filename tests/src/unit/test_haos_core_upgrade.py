"""An optional Core update reuses the running HAOS VM and verifies its version."""

from __future__ import annotations

import pytest
from websockets.exceptions import ConnectionClosed

from .test_haos_e2e_harness import HAOS_RUNTIME_PATH, _load_module


@pytest.mark.parametrize("disconnected", [False, True])
def test_upgrade_reads_back_after_one_update_request(monkeypatch, disconnected) -> None:
    runtime = _load_module("esphome_mcp_test_haos_runtime", HAOS_RUNTIME_PATH)
    calls = []
    versions = iter(["2026.10.2", "2026.10.2", "2026.11.0b4"])

    def request(method, url, **kwargs):
        assert method == "GET"
        return {"version": next(versions), "state": "RUNNING"}

    def command(base_url, token, body, **kwargs):
        calls.append(body)
        if disconnected:
            raise ConnectionClosed(None, None)
        return {"result": "ok"}

    monkeypatch.setattr(runtime, "_http_json", request)
    monkeypatch.setattr(runtime, "websocket_command", command)
    monkeypatch.setattr(runtime.time, "sleep", lambda _: None)
    runtime.upgrade_core("http://ha", "token", "2026.11.0b4")
    assert len(calls) == 1
    assert calls[0]["type"] == "supervisor/api"
    assert calls[0]["endpoint"] == "/core/update"
    assert calls[0]["method"] == "post"
    assert calls[0]["data"] == {"version": "2026.11.0b4"}


def test_current_core_needs_no_update(monkeypatch) -> None:
    runtime = _load_module("esphome_mcp_test_haos_runtime", HAOS_RUNTIME_PATH)
    calls = []

    def request(method, url, **kwargs):
        calls.append(method)
        return {"version": "2026.11.0b4", "state": "RUNNING"}

    monkeypatch.setattr(runtime, "_http_json", request)
    runtime.upgrade_core("http://ha", "token", "2026.11.0b4")
    assert calls == ["GET"]


def test_failed_update_does_not_silently_test_the_old_core(monkeypatch) -> None:
    runtime = _load_module("esphome_mcp_test_haos_runtime", HAOS_RUNTIME_PATH)
    monkeypatch.setattr(runtime, "_http_json", lambda *args, **kwargs: {"version": "2026.10.2"})
    monkeypatch.setattr(runtime, "websocket_command", lambda *args, **kwargs: {"result": "ok"})
    with pytest.raises(TimeoutError, match="2026.11.0b4"):
        runtime.upgrade_core("http://ha", "token", "2026.11.0b4", timeout=0)
