"""
Every commit SHA quoted in a tracked file must resolve.

A short SHA in prose is a reference to something outside the file, and nothing
checks it. That is the same shape as every other entry in the README's failure
table, with one extra property that makes it worse: a dead SHA looks exactly
like a live one, so it survives review indefinitely.

It had already happened here, twice, and was found only because a history
rewrite prompted someone to look. `docs/incidents/2026-09-11-torn-backfill.md`
cited `d7a3bb6` as the structural fix and `ccc6a17` as the SLICE_COLUMN fix.
Neither existed. They were killed by the `git filter-repo` purge of `data/slices`
that ran before the first push -- the incident report has been pointing at
nothing since the day the repository went to GitHub, and the document reads
perfectly well either way.

A third, `7d06ca2`, was live until the author-name rewrite of 2026-09-15 and
would have joined them silently. It is now `64fb41c`, translated through
filter-repo's own commit-map rather than guessed.

The two dead ones were recovered by searching the surviving history for the
change each sentence describes -- `git log -S SLICE_COLUMN -- ingestion/replay.py`
for one, the completion-marker commit for the other -- and verified by reading
the diffs, not by matching commit subjects.

WHY THIS IS A TEST RATHER THAN A RESOLUTION
-------------------------------------------
Rewriting history is legitimate and will happen again. The fix is not to ban
SHAs from documentation; it is to make a stale one fail the build the next time
anything runs, while the rewrite is still fresh in somebody's memory and the
commit-map is still on disk.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Where prose lives. Deliberately not the whole tree: source files are full of
# hex-looking tokens that are not commit references.
SEARCH_GLOBS = ("*.md", "*.yml", "*.yaml")

# Backtick-quoted or bare, 7 to 40 hex characters, with at least one letter so a
# plain number like a row count or a port cannot be mistaken for a SHA.
SHA = re.compile(r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")

# Files whose SHAs belong to OTHER repositories and cannot resolve here.
# dbt writes package-lock.yml itself, pinning dbt_utils to a commit in
# dbt-labs/dbt-utils -- a reference that is correct precisely because it points
# somewhere else.
FOREIGN_SHA_FILES = {"transform/package-lock.yml"}

# Hex-shaped strings that are content hashes, colour values or measured figures
# rather than references to commits.
NOT_A_COMMIT: set[str] = set()


def tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", *SEARCH_GLOBS],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [ROOT / line for line in out.stdout.splitlines() if line.strip()]


def referenced_shas() -> dict[str, list[str]]:
    """Every commit-shaped token in tracked prose, keyed by SHA."""
    found: dict[str, list[str]] = {}
    for path in tracked_files():
        if path.relative_to(ROOT).as_posix() in FOREIGN_SHA_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            # Colour literals are hex and are not commits.
            cleaned = re.sub(r"#[0-9a-fA-F]{3,8}\b", "", line)
            for match in SHA.finditer(cleaned):
                sha = match.group(0)
                if sha in NOT_A_COMMIT:
                    continue
                where = f"{path.relative_to(ROOT).as_posix()}:{line_no}"
                found.setdefault(sha, []).append(where)
    return found


def is_shallow() -> bool:
    out = subprocess.run(
        ["git", "rev-parse", "--is-shallow-repository"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip() == "true"


def test_every_referenced_commit_resolves():
    references = referenced_shas()
    if not references:
        pytest.skip("no commit SHAs are referenced in tracked prose")

    # A shallow clone cannot resolve a SHA that is simply older than its depth,
    # so without this the failure reads "these commits do not exist" and sends
    # the reader hunting for a dangling reference that is perfectly fine. That
    # is the same shallow-checkout trap the secret-scan job already documents,
    # inverted: there it made a check pass without looking, here it makes a
    # check fail while looking at almost nothing.
    #
    # Still a failure, not a skip. A skip is a pass, and the point of this file
    # is that a reference nobody resolved is indistinguishable from one that
    # resolves.
    assert not is_shallow(), (
        "this is a shallow clone, so commit SHAs older than its depth cannot be "
        "resolved and this test cannot do its job. In CI, give the job:\n"
        "    - uses: actions/checkout@v4\n"
        "      with:\n"
        "        fetch-depth: 0"
    )

    dangling: list[str] = []
    for sha, places in sorted(references.items()):
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            dangling.append(f"  {sha}  cited at {', '.join(places)}")

    assert not dangling, (
        "these commit SHAs are cited in tracked files but do not exist in this "
        "repository:\n" + "\n".join(dangling) + "\n\n"
        "A history rewrite changes every SHA. If one just happened, translate the "
        "old reference through .git/filter-repo/commit-map rather than guessing; "
        "if the commit predates a purge, find the change the sentence describes "
        "with `git log -S` and verify the diff before editing the reference."
    )
