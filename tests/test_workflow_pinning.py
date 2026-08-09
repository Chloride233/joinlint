from __future__ import annotations

import re
from pathlib import Path

import yaml


def _remote_uses(value: object) -> list[str]:
    if isinstance(value, dict):
        found = [item for key, item in value.items() if key == "uses"]
        return found + [item for child in value.values() for item in _remote_uses(child)]
    if isinstance(value, list):
        return [item for child in value for item in _remote_uses(child)]
    return []


def test_all_remote_workflow_actions_are_pinned_to_full_commits() -> None:
    violations: list[str] = []
    workflow_paths = sorted(Path(".github/workflows").glob("*.y*ml"))

    for workflow_path in workflow_paths:
        workflow = yaml.load(
            workflow_path.read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )
        for uses in _remote_uses(workflow):
            if uses.startswith("./"):
                continue
            _, separator, revision = uses.rpartition("@")
            if (
                uses.startswith("docker://")
                or separator != "@"
                or re.fullmatch(r"[0-9a-f]{40}", revision) is None
            ):
                violations.append(f"{workflow_path}: {uses}")

    assert workflow_paths
    assert not violations, "unpinned workflow actions:\n" + "\n".join(violations)
