"""Component e2e lane placeholder.

This keeps the CI lane and marker layout in place while the first real HA
container test is built out around the ESPHome fixture setup.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


def test_component_files_exist() -> None:
    """The custom component files needed by HACS and HA are present."""
    root = Path(__file__).resolve().parents[5]
    component = root / "custom_components" / "esphome_mcp"

    assert (component / "manifest.json").is_file()
    assert (component / "config_flow.py").is_file()
    assert (component / "mcp_webhook.py").is_file()
    assert (component / "embedded_server.py").is_file()
