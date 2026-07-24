from __future__ import annotations

from pathlib import Path

import pytest

from joinlint.baseline import load_baseline, update_baseline
from joinlint.errors import JoinLintError
from joinlint.services import run_check


def _write_confirmed_project(project: Path) -> None:
    (project / "data").mkdir()
    (project / "data" / "parents.csv").write_text("id\na\nb\n", encoding="utf-8")
    (project / "data" / "children.csv").write_text("id,parent_id\n1,a\n2,a\n", encoding="utf-8")
    (project / ".joinlint" / "config.yaml").write_text(
        """version: 1
sources:
  sales:
    kind: csv_directory
    path: data
    limits:
      max_source_bytes: 1000
      max_scan_seconds: 60
""",
        encoding="utf-8",
    )
    (project / ".joinlint" / "model.yaml").write_text(
        """version: 1
entities:
  children:
    source: sales
    object: children.csv
    grain:
      keys: [id]
      status: confirmed
  parents:
    source: sales
    object: parents.csv
    grain:
      keys: [id]
      status: confirmed
relationships:
  - id: child_to_parent
    from: children.parent_id
    to: parents.id
    cardinality: many_to_one
    status: confirmed
""",
        encoding="utf-8",
    )


def test_check_without_baseline_is_inconclusive(project: Path) -> None:
    _write_confirmed_project(project)

    with pytest.raises(JoinLintError) as captured:
        run_check(project)

    assert captured.value.code == "BASELINE_MISSING"


def test_check_detects_cardinality_drift_without_writing_generated_state(project: Path) -> None:
    _write_confirmed_project(project)
    update_baseline(project)
    baseline_before = (project / ".joinlint" / "baseline.json").read_bytes()
    generated = project / ".joinlint" / "generated"
    assert not generated.exists()
    with (project / "data" / "parents.csv").open("a", encoding="utf-8") as source:
        source.write("a\n")

    result = run_check(project)

    assert "CARDINALITY_DRIFT" in {finding.code for finding in result.findings}
    assert (project / ".joinlint" / "baseline.json").read_bytes() == baseline_before
    assert not generated.exists()
    assert load_baseline(project)["version"] == 1
