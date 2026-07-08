"""Parse MCP Streamable HTTP responses.

Adapted from ha-mcp's HAOS E2E utilities. The important behavior is handling
both JSON and SSE response bodies without relying on ``str.splitlines()``, which
can split on characters that are legal inside a JSON payload.
"""

from __future__ import annotations

import json
import re
from typing import Any

_SSE_LINE_SPLIT = re.compile(r"\r\n|\r|\n")


def sse_event_payloads(text: str) -> list[str]:
    """Return each SSE event payload from a Streamable HTTP body."""
    payloads: list[str] = []
    data_lines: list[str] = []

    def flush() -> None:
        if data_lines:
            payloads.append("\n".join(data_lines))
            data_lines.clear()

    for line in _SSE_LINE_SPLIT.split(text):
        if line.startswith("data:"):
            value = line[len("data:") :]
            if value.startswith(" "):
                value = value[1:]
            data_lines.append(value)
        elif line == "":
            flush()
    flush()
    return payloads


def parse_mcp_response(content_type: str, body: bytes | str) -> dict[str, Any] | None:
    """Parse a Streamable HTTP MCP body into one JSON-RPC response dict."""
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    if "text/event-stream" in content_type:
        for payload in sse_event_payloads(text):
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and ("result" in obj or "error" in obj):
                return obj
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None
