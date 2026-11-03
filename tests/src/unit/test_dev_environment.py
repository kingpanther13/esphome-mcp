"""The opt-in remote dev holder must report its actual process outcome."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

TUNNELS = Path(__file__).resolve().parents[3] / ".github" / "dev-ha-env" / "tunnels.sh"


@pytest.mark.parametrize("exit_status, expected", [("0", 0), ("7", 7), (None, 1)])
def test_dev_holder_reports_exit_status(
    tmp_path: Path, exit_status: str | None, expected: int
) -> None:
    (tmp_path / "hold.pid").write_text("999999999")
    (tmp_path / "env.log").write_text("STATUS holder ended\n")
    if exit_status is not None:
        (tmp_path / "hold.exit").write_text(exit_status)
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; keep_running last', "bash", str(TUNNELS)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == expected, result.stdout + result.stderr
