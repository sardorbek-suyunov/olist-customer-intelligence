"""
The two requirements files must not disagree about a version.

dashboard/requirements.txt exists so the deployed app installs six packages
instead of the whole build toolchain. That is a second list of dependencies, and
a second list of anything is this project's recurring bug -- so rather than
trusting whoever edits one of them next, the subset relationship is asserted.

What this deliberately does NOT assert is that the lists are equal. They should
not be: the root file builds the marts, the dashboard file reads them.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# The comma is in the name class because extras are part of the name:
# `dlt[duckdb,bigquery]>=1.5.0`. Without it the parser rejects a perfectly valid
# line, and since the parser asserts on anything it cannot read, the test failed
# for the one dependency it was never about.
REQUIREMENT = re.compile(r"^\s*([A-Za-z0-9_.\-\[\],]+?)\s*([<>=!~]+.*)?$")


def parse(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#")[0].strip()
        if not line:
            continue
        match = REQUIREMENT.match(line)
        assert match, f"unparseable requirement in {path.name}: {raw!r}"
        out[match.group(1).lower()] = (match.group(2) or "").strip()
    return out


def test_every_dashboard_dependency_is_in_the_root_file() -> None:
    root = parse(ROOT / "requirements.txt")
    app = parse(ROOT / "dashboard" / "requirements.txt")
    missing = sorted(set(app) - set(root))
    assert not missing, (
        f"{', '.join(missing)} is pinned for the dashboard but absent from the root "
        "requirements, so CI would never install it and a break would only appear "
        "on the deployed app"
    )


def test_the_versions_do_not_disagree() -> None:
    root = parse(ROOT / "requirements.txt")
    app = parse(ROOT / "dashboard" / "requirements.txt")
    for package, spec in app.items():
        assert spec == root[package], (
            f"{package} is {spec!r} for the dashboard and {root[package]!r} at the "
            "root. The deployed app would resolve a different version from the one "
            "the tests run against."
        )


def test_the_dashboard_does_not_pull_in_a_bigquery_client() -> None:
    """
    The deployed demo runs the agent against the committed Parquet, which is why
    it needs no service-account key on a public host. A BigQuery client appearing
    here would mean that decision had quietly been reversed.
    """
    app = parse(ROOT / "dashboard" / "requirements.txt")
    assert "google-cloud-bigquery" not in app
