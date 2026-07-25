from __future__ import annotations

from pathlib import Path

from joinlint.config import add_source
from joinlint.model import Relationship
from joinlint.scanner import scan_snapshot
from joinlint.snapshots import snapshot_source
from joinlint.validation import validate_path, validate_relationship


def test_many_to_many_join_reports_both_directional_multipliers(project: Path) -> None:
    (project / "data").mkdir()
    (project / "data" / "left.csv").write_text("id,key\n1,x\n2,x\n", encoding="utf-8")
    (project / "data" / "right.csv").write_text("id,key\n3,x\n4,x\n", encoding="utf-8")
    add_source(project, "sales", "data", "csv_directory")
    edge = Relationship(
        id="left_to_right",
        from_="left.key",
        to="right.key",
        cardinality="many_to_many",
        status="confirmed",
    )

    with snapshot_source(project, "sales") as snapshot:
        result = validate_relationship(edge, snapshot, scan_snapshot(snapshot))

    assert "MANY_TO_MANY_FANOUT" in {finding.code for finding in result.findings}
    assert result.from_rows_per_to == 2
    assert result.to_rows_per_from == 2


def test_duplicate_parent_key_never_validates_as_unique(project: Path) -> None:
    (project / "data").mkdir()
    (project / "data" / "children.csv").write_text("id,parent_id\n1,a\n", encoding="utf-8")
    (project / "data" / "parents.csv").write_text("id\na\na\n", encoding="utf-8")
    add_source(project, "sales", "data", "csv_directory")
    edge = Relationship(
        id="child_to_parent",
        from_="children.parent_id",
        to="parents.id",
        cardinality="many_to_one",
        status="confirmed",
    )

    with snapshot_source(project, "sales") as snapshot:
        result = validate_relationship(edge, snapshot, scan_snapshot(snapshot))

    assert "REFERENCED_KEY_NOT_UNIQUE" in {finding.code for finding in result.findings}
    assert "CARDINALITY_DRIFT" in {finding.code for finding in result.findings}


def test_many_to_one_join_reports_grain_change_without_blocking(project: Path) -> None:
    (project / "data").mkdir()
    (project / "data" / "children.csv").write_text(
        "id,parent_id\n1,a\n2,a\n3,b\n", encoding="utf-8"
    )
    (project / "data" / "parents.csv").write_text("id\na\nb\n", encoding="utf-8")
    add_source(project, "sales", "data", "csv_directory")
    edge = Relationship(
        id="child_to_parent",
        from_="children.parent_id",
        to="parents.id",
        cardinality="many_to_one",
        status="confirmed",
    )

    with snapshot_source(project, "sales") as snapshot:
        result = validate_relationship(edge, snapshot, scan_snapshot(snapshot))

    assert "GRAIN_CHANGE" in {finding.code for finding in result.findings}
    assert not any(finding.severity == "blocking" for finding in result.findings)


def test_null_and_orphan_child_keys_have_distinct_findings(project: Path) -> None:
    (project / "data").mkdir()
    (project / "data" / "children.csv").write_text(
        "id,parent_id\n1,a\n2,\n3,missing\n", encoding="utf-8"
    )
    (project / "data" / "parents.csv").write_text("id\na\n", encoding="utf-8")
    add_source(project, "sales", "data", "csv_directory")
    edge = Relationship(
        id="child_to_parent",
        from_="children.parent_id",
        to="parents.id",
        cardinality="many_to_one",
        status="confirmed",
    )

    with snapshot_source(project, "sales") as snapshot:
        result = validate_relationship(edge, snapshot, scan_snapshot(snapshot))

    assert {"CHILD_KEY_NULL", "ORPHAN_CHILD_ROW"} <= {finding.code for finding in result.findings}


def test_text_keys_do_not_collapse_numeric_lexemes(project: Path) -> None:
    (project / "data").mkdir()
    (project / "data" / "children.csv").write_text("id,parent_code\n1,001\n2,a\n", encoding="utf-8")
    (project / "data" / "parents.csv").write_text("code\n1\na\n", encoding="utf-8")
    add_source(project, "sales", "data", "csv_directory")
    edge = Relationship(
        id="child_to_parent",
        from_="children.parent_code",
        to="parents.code",
        cardinality="one_to_one",
        status="confirmed",
    )

    with snapshot_source(project, "sales") as snapshot:
        result = validate_relationship(edge, snapshot, scan_snapshot(snapshot))

    assert "ORPHAN_CHILD_ROW" in {finding.code for finding in result.findings}


def test_path_with_two_grain_changes_reports_compound_fanout(project: Path) -> None:
    (project / "data").mkdir()
    (project / "data" / "children.csv").write_text(
        "id,parent_id\n1,a\n2,a\n3,b\n", encoding="utf-8"
    )
    (project / "data" / "parents.csv").write_text("id,grand_id\na,g1\nb,g1\n", encoding="utf-8")
    (project / "data" / "grands.csv").write_text("id\ng1\n", encoding="utf-8")
    add_source(project, "sales", "data", "csv_directory")
    edges = [
        Relationship(
            id="child_to_parent",
            from_="children.parent_id",
            to="parents.id",
            cardinality="many_to_one",
            status="confirmed",
        ),
        Relationship(
            id="parent_to_grand",
            from_="parents.grand_id",
            to="grands.id",
            cardinality="many_to_one",
            status="confirmed",
        ),
    ]

    with snapshot_source(project, "sales") as snapshot:
        result = validate_path(edges, snapshot, scan_snapshot(snapshot))

    assert "COMPOUND_FANOUT" in {finding.code for finding in result.findings}
