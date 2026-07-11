"""Tests for pull-request-driven GitHub release notes."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = ROOT / "scripts" / "release_notes.py"


def _load_release_notes() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_notes", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_extract_release_notes_preserves_user_markdown() -> None:
    """The release section ends at the next top-level PR section."""
    release_notes = _load_release_notes()
    body = """## Summary

Internal implementation details.

## Release notes

- Fixed startup hangs when HA MCP and ESPHome MCP load together.
- Added a restart-specific repair when shared dependencies conflict.

### Compatibility

FastMCP is pinned to the HA MCP-compatible version.

## Testing

- Unit tests pass.
"""

    assert (
        release_notes.extract_release_notes(body)
        == """- Fixed startup hangs when HA MCP and ESPHome MCP load together.
- Added a restart-specific repair when shared dependencies conflict.

### Compatibility

FastMCP is pinned to the HA MCP-compatible version."""
    )


def test_extract_release_notes_ignores_comments_and_fenced_headings() -> None:
    """Template comments and Markdown examples cannot terminate the section."""
    release_notes = _load_release_notes()
    body = """## Release notes

<!-- Replace this comment. -->
- Added a Markdown example:

```markdown
## This is example content
```

## Checklist
"""

    assert (
        release_notes.extract_release_notes(body)
        == """- Added a Markdown example:

```markdown
## This is example content
```"""
    )


def test_select_merged_pull_accepts_unambiguous_rebase_association() -> None:
    """A commit-specific API result survives GitHub rewriting a rebase SHA."""
    release_notes = _load_release_notes()
    pull = {
        "number": 19,
        "merged_at": "2026-07-11T00:00:00Z",
        "merge_commit_sha": "github-rewritten-sha",
    }

    assert release_notes.select_merged_pull([pull], "release-target-sha") is pull


@pytest.mark.parametrize(
    "body, expected",
    [
        ("## Summary\nNothing here.\n", "missing"),
        ("## Release notes\n<!-- guidance -->\n## Testing\n", "empty"),
        ("## Release notes\nN/A\n", "must describe"),
        ("## Release notes\nNone\n", "must describe"),
        (
            "## Release notes\nFirst\n## Release notes\nSecond\n",
            "multiple",
        ),
    ],
)
def test_extract_release_notes_rejects_unpublishable_sections(
    body: str,
    expected: str,
) -> None:
    """Missing, ambiguous, and placeholder notes fail closed."""
    release_notes = _load_release_notes()

    with pytest.raises(release_notes.ReleaseNotesError, match=expected):
        release_notes.extract_release_notes(body)


def test_render_release_notes_uses_exact_merge_pull() -> None:
    """The release body is sourced from the PR that produced the target commit."""
    release_notes = _load_release_notes()
    pulls = [
        {
            "number": 17,
            "title": "Add useful release notes",
            "html_url": "https://github.com/kingpanther13/esphome-mcp/pull/17",
            "body": "## Release notes\n\n- Releases now explain what changed.\n",
            "merged_at": "2026-07-11T00:00:00Z",
            "merge_commit_sha": "abc123",
        },
        {
            "number": 16,
            "title": "Unrelated PR",
            "html_url": "https://github.com/kingpanther13/esphome-mcp/pull/16",
            "body": "## Release notes\n\n- Wrong notes.\n",
            "merged_at": "2026-07-10T00:00:00Z",
            "merge_commit_sha": "def456",
        },
    ]

    assert (
        release_notes.render_release_notes(pulls, "abc123")
        == """## What's changed

- Releases now explain what changed.

---

[Pull request #17](https://github.com/kingpanther13/esphome-mcp/pull/17): Add useful release notes
"""
    )


@pytest.mark.parametrize(
    "pulls, expected",
    [
        ([], "no merged pull request"),
        ({"number": 1}, "JSON list"),
        (
            [
                {"merged_at": "now", "merge_commit_sha": "abc123"},
                {"merged_at": "now", "merge_commit_sha": "abc123"},
            ],
            "multiple merged pull requests",
        ),
    ],
)
def test_select_merged_pull_rejects_unsafe_api_results(
    pulls: object,
    expected: str,
) -> None:
    """Publication stops when commit-to-PR provenance is missing or ambiguous."""
    release_notes = _load_release_notes()

    with pytest.raises(release_notes.ReleaseNotesError, match=expected):
        release_notes.select_merged_pull(pulls, "abc123")


def test_validate_pull_request_event_requires_notes_only_for_a_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Maintenance PRs pass while a version bump with empty notes fails."""
    release_notes = _load_release_notes()
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({"pull_request": {"body": "## Release notes\n"}}))

    monkeypatch.setattr(release_notes, "release_version_changed", lambda _base_ref: False)
    assert release_notes.validate_pull_request_event(event_path, "origin/master") is False

    monkeypatch.setattr(release_notes, "release_version_changed", lambda _base_ref: True)
    with pytest.raises(release_notes.ReleaseNotesError, match="empty"):
        release_notes.validate_pull_request_event(event_path, "origin/master")


def test_render_command_writes_notes_file(tmp_path: Path) -> None:
    """The CLI emits the notes file consumed by gh release create."""
    release_notes = _load_release_notes()
    pulls_path = tmp_path / "pulls.json"
    output_path = tmp_path / "release.md"
    pulls_path.write_text(
        json.dumps(
            [
                {
                    "number": 18,
                    "title": "Ship notes",
                    "html_url": "https://github.com/kingpanther13/esphome-mcp/pull/18",
                    "body": "## Release notes\n\n- Shipped.\n",
                    "merged_at": "now",
                    "merge_commit_sha": "release-sha",
                }
            ]
        )
    )

    result = release_notes.main(
        [
            "render",
            "--pulls-json",
            str(pulls_path),
            "--sha",
            "release-sha",
            "--output",
            str(output_path),
        ]
    )

    assert result == 0
    assert "- Shipped." in output_path.read_text()
