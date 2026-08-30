from __future__ import annotations

import tomllib
from pathlib import Path


def test_local_eval_dependencies_exclude_remote_extensions() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    extras = project["optional-dependencies"]
    local_eval = {requirement.split("==", 1)[0] for requirement in extras["eval"]}
    remote = {requirement.split("==", 1)[0] for requirement in extras["remote"]}

    assert local_eval == {"inspect-ai", "openai"}
    assert remote == {"anthropic", "inspect-sandboxes", "inspect-swe", "modal"}
    assert local_eval.isdisjoint(remote)
