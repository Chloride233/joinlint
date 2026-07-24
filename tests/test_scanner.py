from __future__ import annotations

import shutil
from dataclasses import asdict
from pathlib import Path

from joinlint.candidates import discover_candidates
from joinlint.config import add_source
from joinlint.model import Entity, Grain, ModelV1, Relationship, write_model
from joinlint.scanner import scan_snapshot
from joinlint.snapshots import snapshot_source
from joinlint.validation import validate_relationship
from tests.fixtures.build_conformance_sqlite import build_conformance_sqlite


FIXTURE_DIRECTORY = Path(__file__).parent / "fixtures" / "conformance" / "csv"


def test_profile_records_exact_counts_without_samples(project: Path) -> None:
    shutil.copytree(FIXTURE_DIRECTORY, project / "data")
    add_source(project, "sales", "data", "csv_directory")

    with snapshot_source(project, "sales") as snapshot:
        catalog = scan_snapshot(snapshot)

    order_id = catalog.table("orders").column("order_id")
    assert (order_id.null_count, order_id.distinct_count, order_id.is_unique) == (0, 3, True)
    assert not hasattr(order_id, "values")


def test_csv_and_sqlite_profiles_are_equal_for_conformance_fixture(tmp_path: Path) -> None:
    csv_project = tmp_path / "csv-project"
    sqlite_project = tmp_path / "sqlite-project"
    for project in (csv_project, sqlite_project):
        (project / ".joinlint").mkdir(parents=True)
        (project / ".joinlint" / "config.yaml").write_text("version: 1\nsources: {}\n", encoding="utf-8")
    shutil.copytree(FIXTURE_DIRECTORY, csv_project / "data")
    shutil.copytree(FIXTURE_DIRECTORY, sqlite_project / "csv-data")
    (sqlite_project / "data").mkdir()
    build_conformance_sqlite(sqlite_project / "csv-data", sqlite_project / "data" / "app.sqlite")
    add_source(csv_project, "sales", "data", "csv_directory")
    add_source(sqlite_project, "sales", "data/app.sqlite", "sqlite")

    with snapshot_source(csv_project, "sales") as csv_snapshot:
        csv_catalog = scan_snapshot(csv_snapshot)
    with snapshot_source(sqlite_project, "sales") as sqlite_snapshot:
        sqlite_catalog = scan_snapshot(sqlite_snapshot)

    assert csv_catalog == sqlite_catalog


def test_csv_and_sqlite_candidates_cardinalities_and_findings_are_equal(tmp_path: Path) -> None:
    csv_project = tmp_path / "csv-project"
    sqlite_project = tmp_path / "sqlite-project"
    for project in (csv_project, sqlite_project):
        (project / ".joinlint").mkdir(parents=True)
        (project / ".joinlint" / "config.yaml").write_text("version: 1\nsources: {}\n", encoding="utf-8")
    shutil.copytree(FIXTURE_DIRECTORY, csv_project / "data")
    shutil.copytree(FIXTURE_DIRECTORY, sqlite_project / "csv-data")
    (sqlite_project / "data").mkdir()
    build_conformance_sqlite(sqlite_project / "csv-data", sqlite_project / "data" / "app.sqlite")
    add_source(csv_project, "sales", "data", "csv_directory")
    add_source(sqlite_project, "sales", "data/app.sqlite", "sqlite")
    csv_model = _conformance_model(".csv")
    sqlite_model = _conformance_model("")
    write_model(csv_project, csv_model)
    write_model(sqlite_project, sqlite_model)

    csv_candidates, csv_results = _conformance_outputs(csv_project, csv_model)
    sqlite_candidates, sqlite_results = _conformance_outputs(sqlite_project, sqlite_model)

    assert csv_candidates == sqlite_candidates
    assert csv_results == sqlite_results


def _conformance_model(extension: str) -> ModelV1:
    entities = {
        "customers": Entity(source="sales", object=f"customers{extension}", grain=Grain(keys=["customer_id"], status="confirmed")),
        "line_items": Entity(source="sales", object=f"order_items{extension}", grain=Grain(keys=["order_item_id"], status="confirmed")),
        "orders": Entity(source="sales", object=f"orders{extension}", grain=Grain(keys=["order_id"], status="confirmed")),
        "payments": Entity(source="sales", object=f"payments{extension}", grain=Grain(keys=["payment_id"], status="confirmed")),
    }
    return ModelV1(
        version=1,
        entities=entities,
        relationships=[
            Relationship(id="item_to_order", from_="line_items.order_id", to="orders.order_id", cardinality="many_to_one", status="confirmed"),
            Relationship(id="order_to_customer", from_="orders.customer_id", to="customers.customer_id", cardinality="many_to_one", status="confirmed"),
            Relationship(id="payment_to_order", from_="payments.order_id", to="orders.order_id", cardinality="many_to_one", status="confirmed"),
        ],
    )


def _conformance_outputs(project: Path, model: ModelV1) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    with snapshot_source(project, "sales") as snapshot:
        catalog = scan_snapshot(snapshot)
        candidates = [
            {
                "source_id": candidate.source_id,
                "from_endpoint": candidate.from_endpoint,
                "to_endpoint": candidate.to_endpoint,
                "cardinality": candidate.cardinality,
                "confidence": candidate.confidence,
                "evidence": asdict(candidate.evidence),
                "types": candidate.types,
            }
            for candidate in discover_candidates(snapshot, catalog, model)
        ]
        results = [
            {
                "relationship_id": result.relationship_id,
                "observed_cardinality": result.observed_cardinality,
                "from_rows_per_to": result.from_rows_per_to,
                "to_rows_per_from": result.to_rows_per_from,
                "finding_codes": [finding.code for finding in result.findings],
            }
            for result in (validate_relationship(relationship, snapshot, catalog, model) for relationship in model.relationships)
        ]
    return candidates, results
