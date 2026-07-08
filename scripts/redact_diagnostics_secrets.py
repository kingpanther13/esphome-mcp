#!/usr/bin/env python3
"""Redact credential values from HAOS diagnostics before artifact upload."""

from __future__ import annotations

import io
import json
import sys
import tarfile
from pathlib import Path
from typing import Any

SECRET_KEYS = frozenset(
    {"token", "access_token", "refresh_token", "client_secret", "password", "api_key"}
)
CONFIG_ENTRIES_MEMBERS = ("./core.config_entries", "core.config_entries")
REDACTED = "**REDACTED**"


def redact_config_entries(doc: dict[str, Any]) -> int:
    """Replace credential-named values in config-entry data blocks."""
    redacted = 0
    for entry in doc.get("data", {}).get("entries", []):
        data = entry.get("data")
        if not isinstance(data, dict):
            continue
        for key, value in data.items():
            if key in SECRET_KEYS and isinstance(value, str) and value:
                data[key] = REDACTED
                redacted += 1
    return redacted


def redact_storage_tar(tar_path: Path) -> int:
    """Rewrite a `.storage` tarball in place with config-entry secrets redacted."""
    members: list[tuple[tarfile.TarInfo, bytes | None]] = []
    total = 0
    with tarfile.open(tar_path, "r") as tf:
        for info in tf.getmembers():
            payload: bytes | None = None
            if info.isfile():
                extracted = tf.extractfile(info)
                payload = extracted.read() if extracted is not None else b""
                if info.name in CONFIG_ENTRIES_MEMBERS:
                    doc = json.loads(payload.decode("utf-8"))
                    total += redact_config_entries(doc)
                    payload = json.dumps(doc, indent=2).encode("utf-8")
                    info.size = len(payload)
            members.append((info, payload))

    with tarfile.open(tar_path, "w") as tf:
        for info, payload in members:
            tf.addfile(info, io.BytesIO(payload) if payload is not None else None)
    return total


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    root = Path(argv[1])
    if not root.is_dir():
        print(f"redact: {root} does not exist; nothing to do")
        return 0

    for tar_path in sorted(root.rglob("storage.tar")):
        try:
            count = redact_storage_tar(tar_path)
            print(f"redact: {tar_path}; {count} value(s) redacted")
        except Exception as exc:
            tar_path.unlink(missing_ok=True)
            print(
                f"redact: FAILED for {tar_path} ({type(exc).__name__}: {exc}); tar deleted",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
