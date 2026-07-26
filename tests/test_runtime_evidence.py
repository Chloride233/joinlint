from __future__ import annotations

import sqlite3
from pathlib import Path

from joinlint.model import Entity, Grain, ModelV1, Relationship
from joinlint.runtime.evidence import (
    authorize,
    build_authorized_graph,
    relationship_definitions,
    verify_relationship,
)
from joinlint.runtime.sources import extract_sqlite_catalog, locate_sqlite_sources, snapshot_sqlite


def make_database(path: Path, *, orphan: bool = False) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA foreign_keys = OFF;
        CREATE TABLE customers(id INTEGER PRIMARY KEY);
        CREATE TABLE orders(
          id INTEGER PRIMARY KEY,
          customer_id INTEGER NOT NULL,
          FOREIGN KEY(customer_id) REFERENCES customers(id)
        );
        INSERT INTO customers VALUES (1), (2);
        INSERT INTO orders VALUES (10, 1), (11, 1), (12, 2);
        """
    )
    if orphan:
        connection.execute("INSERT INTO orders VALUES (13, 999)")
    connection.commit()
    connection.close()


def test_declared_relationship_receives_exact_current_authorization(tmp_path: Path) -> None:
    make_database(tmp_path / "data.sqlite")
    source = locate_sqlite_sources(tmp_path, ("data.sqlite",))[0]

    with snapshot_sqlite(tmp_path, source) as snapshot:
        catalog = extract_sqlite_catalog(snapshot)
        seed = relationship_definitions(catalog)[0]
        evidence = verify_relationship(snapshot, catalog, seed)
        projection = authorize(evidence, snapshot.document.snapshot_id)
        graph = build_authorized_graph((seed,), (evidence,), snapshot.document.snapshot_id)

    assert evidence.evidence_mode == "exact"
    assert evidence.measurements.parent_unique
    assert evidence.measurements.orphan_count == 0
    assert projection.usable
    assert len(graph) == 1


def test_orphan_blocks_declared_relationship(tmp_path: Path) -> None:
    make_database(tmp_path / "data.sqlite", orphan=True)
    source = locate_sqlite_sources(tmp_path, ("data.sqlite",))[0]

    with snapshot_sqlite(tmp_path, source) as snapshot:
        catalog = extract_sqlite_catalog(snapshot)
        seed = relationship_definitions(catalog)[0]
        evidence = verify_relationship(snapshot, catalog, seed)
        projection = authorize(evidence, snapshot.document.snapshot_id)

    assert "ORPHAN_CHILD_ROW" in {finding.code for finding in evidence.findings}
    assert not projection.usable


def test_matching_legacy_relationship_is_imported_as_curated(tmp_path: Path) -> None:
    make_database(tmp_path / "data.sqlite")
    source = locate_sqlite_sources(tmp_path, ("data.sqlite",))[0]
    legacy = ModelV1(
        version=1,
        entities={
            "orders": Entity(source="legacy", object="orders", grain=Grain(keys=["id"], status="confirmed")),
            "customers": Entity(
                source="legacy", object="customers", grain=Grain(keys=["id"], status="confirmed")
            ),
        },
        relationships=[
            Relationship(
                id="legacy-rel",
                from_="orders.customer_id",
                to="customers.id",
                cardinality="many_to_one",
                status="confirmed",
            )
        ],
    )

    with snapshot_sqlite(tmp_path, source) as snapshot:
        catalog = extract_sqlite_catalog(snapshot)
        seeds = relationship_definitions(catalog, legacy, curated_source_ids=("legacy",))

    assert len(seeds) == 1
    assert seeds[0].provenance == "curated"


def test_evidence_queries_are_internal_and_never_receive_user_sql(tmp_path: Path) -> None:
    make_database(tmp_path / "data.sqlite")
    source = locate_sqlite_sources(tmp_path, ("data.sqlite",))[0]
    observed: list[str] = []
    user_sql = "SELECT secret FROM private_user_query"

    with snapshot_sqlite(tmp_path, source) as snapshot:
        catalog = extract_sqlite_catalog(snapshot)
        verify_relationship(
            snapshot,
            catalog,
            relationship_definitions(catalog)[0],
            query_observer=observed.append,
        )

    assert observed
    assert all(user_sql not in statement for statement in observed)
    assert all(statement.startswith("SELECT") for statement in observed)
