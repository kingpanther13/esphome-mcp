"""Smoke-test the FastMCP release tracked by the standalone CI canary."""

from __future__ import annotations

from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
CANARY_REQUIREMENT = ROOT / "tests" / "fastmcp_canary.txt"


def _read_canary_requirement() -> Requirement:
    """Read the single active requirement from the canary input."""
    active = [
        line.strip()
        for line in CANARY_REQUIREMENT.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(active) != 1:
        raise RuntimeError("FastMCP canary must contain exactly one active requirement")
    requirement = Requirement(active[0])
    specifiers = list(requirement.specifier)
    if requirement.name.lower() != "fastmcp" or len(specifiers) != 1:
        raise RuntimeError("FastMCP canary must be one exact fastmcp==X.Y.Z pin")
    if specifiers[0].operator != "==" or specifiers[0].version.endswith(".*"):
        raise RuntimeError("FastMCP canary must be one exact fastmcp==X.Y.Z pin")
    return requirement


def main() -> int:
    """Verify the canary wheel and the API surface ESPHome MCP consumes."""
    requirement = _read_canary_requirement()
    installed_version = Version(metadata.version("fastmcp"))
    if installed_version not in requirement.specifier:
        raise RuntimeError(f"Installed FastMCP {installed_version} does not satisfy {requirement}")

    from fastmcp import FastMCP

    server = FastMCP("ESPHome MCP standalone canary")
    app = server.http_app(path="/mcp", stateless_http=True)
    if not callable(app):
        raise RuntimeError("FastMCP http_app did not return an ASGI application")
    print(f"FastMCP standalone canary passed with {installed_version}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
