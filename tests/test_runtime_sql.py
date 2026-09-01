from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from joinlint.runtime.domain import EntityRef
from joinlint.runtime.evidence import build_authorized_graph, relationship_definitions, verify_relationship
from joinlint.runtime.planner import plan_join
from joinlint.runtime.sources import extract_sqlite_catalog, locate_sqlite_sources, snapshot_sqlite
from joinlint.runtime.sql import SQLValidationError, normalize_sql_graph, validate_sql_graph


def make_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE customers(id INTEGER PRIMARY KEY, manager_id INTEGER,
          FOREIGN KEY(manager_id) REFERENCES customers(id));
        CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL,
          FOREIGN KEY(customer_id) REFERENCES customers(id));
        INSERT INTO customers VALUES (1, NULL), (2, 1);
        INSERT INTO orders VALUES (10, 1), (11, 1), (12, 2);
        """
    )
    connection.commit()
    connection.close()


def make_composite_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE customers(
          tenant_id INTEGER NOT NULL,
          id INTEGER NOT NULL,
          PRIMARY KEY(tenant_id, id)
        );
        CREATE TABLE orders(
          id INTEGER PRIMARY KEY,
          tenant_id INTEGER NOT NULL,
          customer_id INTEGER NOT NULL,
          FOREIGN KEY(tenant_id, customer_id) REFERENCES customers(tenant_id, id)
        );
        CREATE TABLE regions(region_id INTEGER PRIMARY KEY);
        CREATE TABLE stores(
          id INTEGER PRIMARY KEY,
          region_id INTEGER NOT NULL,
          FOREIGN KEY(region_id) REFERENCES regions(region_id)
        );
        INSERT INTO customers VALUES (1, 10), (1, 11);
        INSERT INTO orders VALUES (100, 1, 10), (101, 1, 11);
        INSERT INTO regions VALUES (7);
        INSERT INTO stores VALUES (70, 7);
        """
    )
    connection.commit()
    connection.close()


def runtime(tmp_path: Path):  # type: ignore[no-untyped-def]
    make_database(tmp_path / "data.sqlite")
    source = locate_sqlite_sources(tmp_path, ("data.sqlite",))[0]
    snapshot = snapshot_sqlite(tmp_path, source)
    catalog = extract_sqlite_catalog(snapshot)
    seeds = relationship_definitions(catalog)
    evidence = tuple(verify_relationship(snapshot, catalog, seed) for seed in seeds)
    graph = build_authorized_graph(seeds, evidence, snapshot.document.snapshot_id)
    return snapshot, catalog, graph


def entity_id(catalog, table: str) -> str:  # type: ignore[no-untyped-def]
    return next(entity.entity_id for entity in catalog.entities if entity.physical_name == table)


def test_alias_and_equality_order_produce_equivalent_semantic_graph(tmp_path: Path) -> None:
    snapshot, catalog, graph = runtime(tmp_path)
    try:
        first = normalize_sql_graph(
            "SELECT * FROM orders o JOIN customers c ON o.customer_id = c.id",
            catalog,
            catalog.source_id,
        )
        second = normalize_sql_graph(
            "SELECT * FROM customers AS x JOIN orders AS y ON x.id = y.customer_id",
            catalog,
            catalog.source_id,
        )
        assert validate_sql_graph(first, graph).decision == "pass"
        assert validate_sql_graph(second, graph).decision == "pass"
        assert first == second
        assert validate_sql_graph(first, graph).matched_relationship_ids == validate_sql_graph(
            second, graph
        ).matched_relationship_ids
    finally:
        snapshot.close()


def test_cte_passthrough_column_resolves_to_physical_endpoint(tmp_path: Path) -> None:
    snapshot, catalog, graph = runtime(tmp_path)
    try:
        normalized = normalize_sql_graph(
            "WITH o AS (SELECT customer_id FROM orders) "
            "SELECT * FROM o JOIN customers c ON o.customer_id = c.id",
            catalog,
            catalog.source_id,
        )
        assert validate_sql_graph(normalized, graph).decision == "pass"
    finally:
        snapshot.close()


def test_self_join_uses_instances_and_matches_proof_modulo_aliases(tmp_path: Path) -> None:
    snapshot, catalog, graph = runtime(tmp_path)
    try:
        customers = entity_id(catalog, "customers")
        proof = plan_join(
            (
                EntityRef(ref="employee", entity=customers),
                EntityRef(ref="manager", entity=customers),
            ),
            "employee",
            "employee",
            4,
            False,
            graph,
        )
        normalized = normalize_sql_graph(
            "SELECT * FROM customers e JOIN customers m ON e.manager_id = m.id",
            catalog,
            catalog.source_id,
        )
        outcome = validate_sql_graph(normalized, graph, proof=proof)
        assert outcome.decision == "pass"
        assert outcome.proof_matched is True
    finally:
        snapshot.close()


def test_eight_instance_self_join_is_deterministic_without_factorial_alias_cost(
    tmp_path: Path,
) -> None:
    snapshot, catalog, _ = runtime(tmp_path)
    try:
        first_aliases = [f"n{index}" for index in range(8)]
        second_aliases = [f"renamed_{7 - index}" for index in range(8)]

        def self_join_sql(aliases: list[str]) -> str:
            return "SELECT * FROM customers " + aliases[0] + " " + " ".join(
                f"JOIN customers {aliases[index]} "
                f"ON {aliases[index - 1]}.manager_id = {aliases[index]}.id"
                for index in range(1, len(aliases))
            )

        first = normalize_sql_graph(
            self_join_sql(first_aliases),
            catalog,
            catalog.source_id,
        )
        second = normalize_sql_graph(
            self_join_sql(second_aliases),
            catalog,
            catalog.source_id,
        )

        assert first == second
        assert len(first.entity_refs) == 8
        assert len(first.edges) == 7
    finally:
        snapshot.close()


def test_proof_graph_mismatch_blocks_sql(tmp_path: Path) -> None:
    snapshot, catalog, graph = runtime(tmp_path)
    try:
        orders = entity_id(catalog, "orders")
        customers = entity_id(catalog, "customers")
        proof = plan_join(
            (
                EntityRef(ref="orders", entity=orders),
                EntityRef(ref="customers", entity=customers),
            ),
            "orders",
            "orders",
            4,
            False,
            graph,
        )
        normalized = normalize_sql_graph(
            "SELECT * FROM customers e JOIN customers m ON e.manager_id = m.id",
            catalog,
            catalog.source_id,
        )
        outcome = validate_sql_graph(normalized, graph, proof=proof)
        assert outcome.decision == "block"
        assert {finding.code for finding in outcome.findings} == {"PROOF_GRAPH_MISMATCH"}
    finally:
        snapshot.close()


@pytest.mark.parametrize(
    ("sql", "code", "blocking"),
    [
        ("DELETE FROM orders", "UNSUPPORTED_SQL_STATEMENT", True),
        ("SELECT * FROM orders; SELECT * FROM customers", "MULTIPLE_STATEMENTS", True),
        ("SELECT * FROM orders CROSS JOIN customers", "UNSUPPORTED_JOIN_KIND", True),
        ("SELECT * FROM orders NATURAL JOIN customers", "UNSUPPORTED_JOIN_KIND", True),
        (
            "SELECT * FROM orders o JOIN customers c "
            "ON o.customer_id = c.id AND o.id > c.id",
            "NON_EQUALITY_JOIN",
            True,
        ),
        (
            "SELECT * FROM orders o JOIN customers c "
            "ON o.customer_id = c.id OR o.id = c.id",
            "NON_EQUALITY_JOIN",
            True,
        ),
        ("SELECT (", "SQL_PARSE_ERROR", False),
    ],
)
def test_unsupported_sql_shapes_fail_with_stable_classification(
    tmp_path: Path, sql: str, code: str, blocking: bool
) -> None:
    snapshot, catalog, _ = runtime(tmp_path)
    try:
        with pytest.raises(SQLValidationError) as captured:
            normalize_sql_graph(sql, catalog, catalog.source_id)
        assert captured.value.code == code
        assert captured.value.blocking is blocking
    finally:
        snapshot.close()


def test_left_join_parent_grain_is_blocked_by_compatibility(tmp_path: Path) -> None:
    snapshot, catalog, graph = runtime(tmp_path)
    try:
        normalized = normalize_sql_graph(
            "SELECT * FROM customers c LEFT JOIN orders o ON o.customer_id = c.id",
            catalog,
            catalog.source_id,
        )
        outcome = validate_sql_graph(normalized, graph, expected_grain_ref="customers")
        assert outcome.decision == "block"
        assert {finding.code for finding in outcome.findings} == {"GRAIN_INCOMPATIBLE"}

        alias_permutation = normalize_sql_graph(
            "SELECT * FROM customers z LEFT JOIN orders a ON z.id = a.customer_id",
            catalog,
            catalog.source_id,
        )
        assert alias_permutation == normalized
        assert validate_sql_graph(
            alias_permutation,
            graph,
            expected_grain_ref="customers",
        ).decision == "block"
    finally:
        snapshot.close()


@pytest.mark.parametrize(
    "sql",
    [
        (
            "SELECT * FROM (SELECT customer_id FROM orders) o "
            "JOIN customers c ON o.customer_id = c.id"
        ),
        (
            "SELECT * FROM orders o, customers c "
            "WHERE o.customer_id = c.id"
        ),
    ],
)
def test_subquery_and_where_equality_are_supported(tmp_path: Path, sql: str) -> None:
    snapshot, catalog, graph = runtime(tmp_path)
    try:
        normalized = normalize_sql_graph(sql, catalog, catalog.source_id)
        assert validate_sql_graph(normalized, graph).decision == "pass"
    finally:
        snapshot.close()


def test_composite_and_using_joins_are_grouped_and_validated(tmp_path: Path) -> None:
    make_composite_database(tmp_path / "data.sqlite")
    source = locate_sqlite_sources(tmp_path, ("data.sqlite",))[0]
    snapshot = snapshot_sqlite(tmp_path, source)
    try:
        catalog = extract_sqlite_catalog(snapshot)
        seeds = relationship_definitions(catalog)
        evidence = tuple(verify_relationship(snapshot, catalog, seed) for seed in seeds)
        graph = build_authorized_graph(seeds, evidence, snapshot.document.snapshot_id)

        composite = normalize_sql_graph(
            "SELECT * FROM orders o JOIN customers c "
            "ON c.id = o.customer_id AND o.tenant_id = c.tenant_id",
            catalog,
            catalog.source_id,
        )
        using = normalize_sql_graph(
            "SELECT * FROM stores s JOIN regions r USING (region_id)",
            catalog,
            catalog.source_id,
        )

        assert len(composite.edges) == 1
        assert len(composite.edges[0].endpoint_pairs) == 2
        assert validate_sql_graph(composite, graph).decision == "pass"
        assert validate_sql_graph(using, graph).decision == "pass"
    finally:
        snapshot.close()


def test_sqlite_identifier_matching_is_case_insensitive(tmp_path: Path) -> None:
    make_database(tmp_path / "data.sqlite")
    source = locate_sqlite_sources(tmp_path, ("data.sqlite",))[0]
    snapshot = snapshot_sqlite(tmp_path, source)
    try:
        catalog = extract_sqlite_catalog(snapshot)
        seeds = relationship_definitions(catalog)
        evidence = tuple(verify_relationship(snapshot, catalog, seed) for seed in seeds)
        graph = build_authorized_graph(seeds, evidence, snapshot.document.snapshot_id)

        normalized = normalize_sql_graph(
            "SELECT * FROM ORDERS o JOIN CUSTOMERS c "
            "ON o.CUSTOMER_ID = c.ID",
            catalog,
            catalog.source_id,
        )

        assert validate_sql_graph(normalized, graph).decision == "pass"
    finally:
        snapshot.close()
