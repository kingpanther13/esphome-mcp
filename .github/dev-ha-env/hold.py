"""Opt-in HAOS development holder, copied into the E2E package by Actions."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from haos_runtime import ESPHOME_MCP_SERVER_WEBHOOK_ID

from .test_embedded_server_haos import embedded_server  # noqa: F401


def test_hold(embedded_server: tuple[str, str | None, str]) -> None:  # noqa: F811
    """Keep the ordinary HAOS fixture available until the requested deadline."""
    base_url, _session_id, _configuration = embedded_server
    targets = Path(os.environ["DEVENV_TARGETS"])
    targets.write_text(json.dumps({
        "ha": base_url, "mcp": base_url,
        "mcp_path": f"/api/webhook/{ESPHOME_MCP_SERVER_WEBHOOK_ID}",
    }))
    minutes = min(240, max(1, int(os.environ["DEVENV_MINUTES"])))
    deadline = time.monotonic() + minutes * 60
    print("STATUS HAOS dev environment ready", flush=True)
    while time.monotonic() < deadline:
        time.sleep(min(30, max(0, deadline - time.monotonic())))
    print("STATUS HAOS dev environment lifetime complete", flush=True)
