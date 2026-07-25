from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from joinlint.candidates import accept_candidate, discover_candidates, normalize_value, reject_candidate, visible_candidates
from joinlint.config import add_source
from joinlint.errors import JoinLintError
from joinlint.model import load_model
from joinlint.scanner import scan_snapshot
from joinlint.services import scan_project
from joinlint.snapshots import snapshot_source


FIXTURE_DIRECTORY = Path(__file__).parent / "fixtures" / "conformance" / "csv"


def _write_model(project: Path) -> None:
    (project / ".joinlint" / "model.yaml").write_text(
        """version: 1
entities:
  purchase_orders:
    source: sales
    object: orders.csv
    grain:
      keys: [order_id]
      status: confirmed
  line_items:
    source: sales
    object: order_items.csv
    grain:
      keys: [order_item_id]
      status: confirmed
relationships: []
""",
        encoding="utf-8",
    )


def _order_items_candidate(project: Path):
    with snapshot_source(project, "sales") as snapshot:
        catalog = scan_snapshot(snapshot)
        candidates = discover_candidates(snapshot, catalog, load_model(project))
    return next(
        candidate
        for candidate in candidates
        if candidate.from_endpoint == "line_items.order_id" and candidate.to_endpoint == "purchase_orders.order_id"
    )


def test_candidate_has_exact_coverage_and_directed_cardinality(project: Path) -> None:
    shutil.copytree(FIXTURE_DIRECTORY, project / "data")
    add_source(project, "sales", "data", "csv_directory")
    _write_model(project)

    candidate = _order_items_candidate(project)

    assert candidate.cardinality == "many_to_one"
    assert candidate.evidence.inclusion_numerator == 3
    assert candidate.evidence.inclusion_denominator == 3
    assert candidate.evidence.matched_distinct_count == 2
    assert candidate.evidence.orphan_count == 0


def test_accept_candidate_writes_confirmed_relationship(project: Path) -> None:
    shutil.copytree(FIXTURE_DIRECTORY, project / "data")
    add_source(project, "sales", "data", "csv_directory")
    _write_model(project)
    candidate = _order_items_candidate(project)

    accept_candidate(project, candidate)

    relationship = load_model(project).relationships[0]
    assert relationship.id == candidate.id
    assert relationship.from_ == candidate.from_endpoint
    assert relationship.to == candidate.to_endpoint


def test_same_evidence_keeps_local_rejection_but_changed_evidence_invalidates_it(project: Path) -> None:
    shutil.copytree(FIXTURE_DIRECTORY, project / "data")
    add_source(project, "sales", "data", "csv_directory")
    _write_model(project)
    candidate = _order_items_candidate(project)

    reject_candidate(project, candidate)
    scan_project(project)

    with snapshot_source(project, "sales") as snapshot:
        assert candidate.id not in {
            item.id for item in visible_candidates(project, snapshot, scan_snapshot(snapshot), load_model(project))
        }

    with (project / "data" / "order_items.csv").open("a", encoding="utf-8") as source:
        source.write("103,999,c\n")
    changed_candidate = _order_items_candidate(project)
    with snapshot_source(project, "sales") as snapshot:
        visible_ids = {
            item.id for item in visible_candidates(project, snapshot, scan_snapshot(snapshot), load_model(project))
        }

    assert changed_candidate.id == candidate.id
    assert changed_candidate.evidence.orphan_count == 1
    assert candidate.id in visible_ids


def test_accept_rejects_candidate_when_model_has_changed(project: Path) -> None:
    shutil.copytree(FIXTURE_DIRECTORY, project / "data")
    add_source(project, "sales", "data", "csv_directory")
    _write_model(project)
    candidate = _order_items_candidate(project)
    (project / ".joinlint" / "model.yaml").write_text(
        (project / ".joinlint" / "model.yaml").read_text(encoding="utf-8").replace(
            "object: orders.csv", "object: orders_v2.csv"
        ),
        encoding="utf-8",
    )

    with pytest.raises(JoinLintError) as captured:
        accept_candidate(project, candidate)
    assert captured.value.code == "CANDIDATE_STALE"


def test_value_normalization_matches_csv_numeric_text_to_sqlite_values() -> None:
    assert normalize_value("10.0", "number") == normalize_value(10.0, "number") == "10"
    assert normalize_value("001", "text") != normalize_value("1", "text")
