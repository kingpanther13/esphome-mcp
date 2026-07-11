"""Validate and render GitHub release notes from a merged pull request."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = "custom_components/esphome_mcp/manifest.json"

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_RELEASE_HEADING_RE = re.compile(r"^##[ \t]+release notes[ \t]*$", re.IGNORECASE)
_TOP_LEVEL_HEADING_RE = re.compile(r"^#{1,2}(?:[ \t]+|$)")
_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
_EMPTY_RELEASE_NOTES = {
    "n a",
    "na",
    "no release notes",
    "none",
    "not applicable",
}


class ReleaseNotesError(ValueError):
    """Raised when release notes cannot be safely produced."""


def _fence_marker(line: str) -> str | None:
    match = _FENCE_RE.match(line)
    return match.group(1) if match else None


def extract_release_notes(body: str) -> str:
    """Extract the single level-two Release notes section from a PR body."""
    cleaned = _HTML_COMMENT_RE.sub("", body)
    lines = cleaned.splitlines()
    section_starts: list[int] = []
    active_fence: str | None = None

    for index, line in enumerate(lines):
        marker = _fence_marker(line)
        if marker is not None:
            if active_fence is None:
                active_fence = marker
            elif marker[0] == active_fence[0] and len(marker) >= len(active_fence):
                active_fence = None
            continue
        if active_fence is None and _RELEASE_HEADING_RE.fullmatch(line.strip()):
            section_starts.append(index)

    if not section_starts:
        raise ReleaseNotesError("pull request body is missing a '## Release notes' section")
    if len(section_starts) > 1:
        raise ReleaseNotesError("pull request body contains multiple '## Release notes' sections")

    start = section_starts[0] + 1
    end = len(lines)
    active_fence = None
    for index in range(start, len(lines)):
        marker = _fence_marker(lines[index])
        if marker is not None:
            if active_fence is None:
                active_fence = marker
            elif marker[0] == active_fence[0] and len(marker) >= len(active_fence):
                active_fence = None
            continue
        if active_fence is None and _TOP_LEVEL_HEADING_RE.match(lines[index].strip()):
            end = index
            break

    notes = "\n".join(lines[start:end]).strip()
    if not notes:
        raise ReleaseNotesError("the '## Release notes' section is empty")

    normalized = re.sub(r"[^a-z0-9]+", " ", notes.casefold()).strip()
    if normalized in _EMPTY_RELEASE_NOTES:
        raise ReleaseNotesError("the '## Release notes' section must describe what changed")
    return notes


def select_merged_pull(pulls: Any, merge_sha: str) -> dict[str, Any]:
    """Select the merged PR that introduced the release target commit."""
    if not isinstance(pulls, list):
        raise ReleaseNotesError("commit-to-pull response must be a JSON list")

    merged = [pull for pull in pulls if isinstance(pull, dict) and pull.get("merged_at")]
    exact_matches = [pull for pull in merged if pull.get("merge_commit_sha") == merge_sha]
    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(exact_matches) > 1:
        raise ReleaseNotesError(f"multiple merged pull requests claim release commit {merge_sha}")

    # GitHub can rewrite commit SHAs for rebase merges. The endpoint itself is
    # commit-specific, so one merged result remains unambiguous in that case.
    if len(merged) == 1:
        return merged[0]
    if not merged:
        raise ReleaseNotesError(
            f"no merged pull request is associated with release commit {merge_sha}"
        )
    raise ReleaseNotesError(
        f"multiple merged pull requests are associated with release commit {merge_sha}"
    )


def render_release_notes(pulls: Any, merge_sha: str) -> str:
    """Render a GitHub release body from the exact merged PR."""
    pull = select_merged_pull(pulls, merge_sha)
    number = pull.get("number")
    title = pull.get("title")
    url = pull.get("html_url")
    body = pull.get("body")

    if not isinstance(number, int):
        raise ReleaseNotesError("merged pull request is missing its number")
    if not isinstance(title, str) or not title.strip():
        raise ReleaseNotesError("merged pull request is missing its title")
    if not isinstance(url, str) or not url.startswith("https://github.com/"):
        raise ReleaseNotesError("merged pull request is missing its GitHub URL")
    if not isinstance(body, str):
        raise ReleaseNotesError("merged pull request body is empty")

    notes = extract_release_notes(body)
    return (
        f"## What's changed\n\n{notes}\n\n---\n\n[Pull request #{number}]({url}): {title.strip()}\n"
    )


def _git(args: list[str]) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def _manifest_version_from_worktree() -> str:
    manifest = json.loads((ROOT / MANIFEST_PATH).read_text())
    return str(manifest["version"])


def _manifest_version_from_ref(ref: str) -> str:
    manifest = json.loads(_git(["show", f"{ref}:{MANIFEST_PATH}"]))
    return str(manifest["version"])


def release_version_changed(base_ref: str) -> bool:
    """Return whether this PR changes the version that will be released."""
    return _manifest_version_from_worktree() != _manifest_version_from_ref(base_ref)


def validate_pull_request_event(event_path: Path, base_ref: str) -> bool:
    """Validate release notes when a PR changes the release version."""
    if not release_version_changed(base_ref):
        return False

    event = json.loads(event_path.read_text())
    pull_request = event.get("pull_request")
    if not isinstance(pull_request, dict):
        raise ReleaseNotesError("GitHub event does not contain a pull_request object")
    body = pull_request.get("body")
    extract_release_notes(body if isinstance(body, str) else "")
    return True


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-pr")
    validate.add_argument("--base-ref", required=True)
    validate.add_argument("--event-path", type=Path, required=True)

    render = subparsers.add_parser("render")
    render.add_argument("--pulls-json", type=Path, required=True)
    render.add_argument("--sha", required=True)
    render.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "validate-pr":
            required = validate_pull_request_event(args.event_path, args.base_ref)
            if required:
                print("Release notes are valid for the new component version.")
            else:
                print("Release notes are not required because the component version is unchanged.")
            return 0

        pulls = json.loads(args.pulls_json.read_text())
        args.output.write_text(render_release_notes(pulls, args.sha))
        print(f"Release notes written to {args.output}.")
        return 0
    except (ReleaseNotesError, json.JSONDecodeError, OSError, subprocess.CalledProcessError) as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
