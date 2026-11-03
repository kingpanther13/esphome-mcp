"""An optional Core update reuses the running HAOS VM and verifies its version."""

from __future__ import annotations

import urllib.error

import pytest

from .test_haos_e2e_harness import HAOS_RUNTIME_PATH, _load_module


@pytest.mark.parametrize("disconnected", [False, True])
def test_upgrade_reads_back_after_one_update_request(monkeypatch, disconnected) -> None:
    runtime = _load_module("esphome_mcp_test_haos_runtime", HAOS_RUNTIME_PATH)
    calls = []
    versions = iter(["2026.10.2", "2026.10.2", "2026.11.0b4"])

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if method == "GET":
            return {"version": next(versions), "state": "RUNNING"}
        if disconnected:
            raise urllib.error.URLError("Core restarted before answering")
        return {"result": "ok"}

    monkeypatch.setattr(runtime, "_http_json", request)
    monkeypatch.setattr(runtime.time, "sleep", lambda _: None)
    runtime.upgrade_core("http://ha", "token", "2026.11.0b4")
    writes = [call for call in calls if call[0] == "POST"]
    assert len(writes) == 1
    assert writes[0][1] == "http://ha/api/hassio/core/update"
    assert writes[0][2]["body"] == {"version": "2026.11.0b4"}


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
    monkeypatch.setattr(
        runtime, "_http_json", lambda *args, **kwargs: {"version": "2026.10.2"}
    )
    with pytest.raises(TimeoutError, match="2026.11.0b4"):
        runtime.upgrade_core("http://ha", "token", "2026.11.0b4", timeout=0)
