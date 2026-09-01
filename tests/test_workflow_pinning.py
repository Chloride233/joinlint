from __future__ import annotations

import re
from pathlib import Path

import yaml


_APPROVED_ACTIONS = frozenset(
    {
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",  # v7.0.1
        "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093",  # v4.3.0
        "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",  # v7.0.0
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",  # v4.6.2
    }
)


def _uses_references(value: object) -> list[str]:
    if isinstance(value, dict):
        found = [item for key, item in value.items() if key == "uses"]
        return found + [item for child in value.values() for item in _uses_references(child)]
    if isinstance(value, list):
        return [item for child in value for item in _uses_references(child)]
    return []


def test_all_workflow_actions_use_approved_immutable_commits() -> None:
    violations: list[str] = []
    workflow_paths = sorted(Path(".github/workflows").glob("*.y*ml"))

    for workflow_path in workflow_paths:
        workflow = yaml.load(
            workflow_path.read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )
        for uses in _uses_references(workflow):
            _, separator, revision = uses.rpartition("@")
            if (
                uses not in _APPROVED_ACTIONS
                or separator != "@"
                or re.fullmatch(r"[0-9a-f]{40}", revision) is None
            ):
                violations.append(f"{workflow_path}: {uses}")

    assert workflow_paths
    assert not violations, "unapproved workflow actions:\n" + "\n".join(violations)


def test_ci_runs_once_per_feature_branch_update() -> None:
    workflow = yaml.load(
        Path(".github/workflows/ci.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )

    assert set(workflow["on"]) == {"push", "pull_request"}
    assert workflow["on"]["push"] == {"branches": ["main"]}
    assert workflow["on"]["pull_request"] == ""
    assert workflow["concurrency"] == {
        "group": "ci-${{ github.workflow }}-${{ github.ref }}",
        "cancel-in-progress": "true",
    }
