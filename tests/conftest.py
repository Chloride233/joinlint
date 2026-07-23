from __future__ import annotations

from pathlib import Path

import pytest


def make_project(root: Path) -> Path:
    project = root / "project"
    (project / ".joinlint").mkdir(parents=True)
    (project / ".joinlint" / "config.yaml").write_text(
        "version: 1\nsources: {}\n", encoding="utf-8"
    )
    return project


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return make_project(tmp_path)
