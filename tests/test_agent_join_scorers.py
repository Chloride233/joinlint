from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

pytest.importorskip("inspect_ai")
pytest.importorskip("sqlglot")

from benchmarks.agent_join.sql_edges import (  # noqa: E402
    canonical_edge,
    extract_join_edges,
    extract_submission,
    score_join_graph,
    validate_readonly_select,
)
from benchmarks.agent_join.execution import execute_readonly, execution_matches  # noqa: E402
from tests.agent_join_helpers import build_orders_database  # noqa: E402


SCHEMA = {
    "customers": {"id": "INTEGER", "name": "TEXT"},
    "orders": {"id": "INTEGER", "customer_id": "INTEGER", "total": "REAL"},
}
EDGE = ("customers.id", "orders.customer_id")


def test_extracts_alias_and_where_join_as_the_same_edge() -> None:
    on_edges = extract_join_edges(
        "SELECT * FROM orders AS o JOIN customers AS c ON o.customer_id = c.id",
        SCHEMA,
    )
    where_edges = extract_join_edges(
        "SELECT * FROM orders o, customers c WHERE c.id = o.customer_id",
        SCHEMA,
    )
    assert on_edges == frozenset({EDGE})
    assert where_edges == frozenset({EDGE})


def test_cte_preserves_a_physical_join_inside_the_cte() -> None:
    edges = extract_join_edges(
        "WITH x AS (SELECT o.id FROM orders o JOIN customers c "
        "ON c.id = o.customer_id) SELECT * FROM x",
        SCHEMA,
    )
    assert edges == frozenset({EDGE})


def test_constants_and_same_physical_table_do_not_create_edges() -> None:
    assert extract_join_edges("SELECT * FROM orders WHERE id = 10", SCHEMA) == frozenset()
    assert (
        extract_join_edges(
            "SELECT * FROM orders a JOIN orders b ON a.customer_id = b.customer_id",
            SCHEMA,
        )
        == frozenset()
    )


def test_wrong_join_requires_no_extra_and_no_missing_edges() -> None:
    allowed = [frozenset({EDGE})]
    assert score_join_graph(allowed[0], allowed).wrong_join is False
    assert score_join_graph(frozenset(), allowed).wrong_join is True
    extra = frozenset({EDGE, ("customers.id", "orders.id")})
    assert score_join_graph(extra, allowed).wrong_join is True


def test_best_allowed_graph_is_selected_deterministically() -> None:
    alternative = frozenset({("customers.id", "orders.id")})
    score = score_join_graph(frozenset({EDGE}), [alternative, frozenset({EDGE})])
    assert score.wrong_join is False
    assert score.matched_graph == [EDGE]


def test_readonly_validator_rejects_mutation_and_multiple_statements() -> None:
    assert validate_readonly_select("SELECT * FROM orders").sql == "SELECT * FROM orders"
    for sql in (
        "DELETE FROM orders",
        "ATTACH DATABASE '/tmp/x' AS x",
        "PRAGMA writable_schema=ON",
        "SELECT 1; SELECT 2",
        "SELECT load_extension('/tmp/unsafe')",
    ):
        assert validate_readonly_select(sql).error_code == "UNSAFE_SQL"


def test_submission_requires_exact_json_string_fields() -> None:
    submission = extract_submission('{"sql":" SELECT 1 ","warning":""}')
    assert submission.sql == "SELECT 1"
    assert submission.warning == ""
    for value in (
        "not-json",
        "[]",
        '{"sql":"SELECT 1"}',
        '{"sql":"SELECT 1","warning":"","extra":true}',
        '{"sql":1,"warning":""}',
    ):
        with pytest.raises((TypeError, ValueError, json.JSONDecodeError)):
            extract_submission(value)


def test_ambiguous_or_invalid_sql_does_not_produce_edges() -> None:
    with pytest.raises(ValueError):
        extract_join_edges("", SCHEMA)
    with pytest.raises(ValueError):
        extract_join_edges(
            "SELECT * FROM orders JOIN customers ON id = id",
            SCHEMA,
        )


def test_canonical_edge_uses_utf8_order() -> None:
    assert canonical_edge("orders.customer_id", "customers.id") == EDGE


def test_readonly_execution_matches_equivalent_column_order(tmp_path: Path) -> None:
    database = build_orders_database(tmp_path)
    result = execution_matches(
        database,
        "fixture-task",
        "SELECT customer_id, id FROM orders ORDER BY id",
        "SELECT id, customer_id FROM orders ORDER BY id",
        deadline_seconds=5,
        max_rows=10_000,
    )
    assert result.executed is True
    assert result.equivalent is True


def test_execution_never_runs_mutation_attach_or_multiple_statements(
    tmp_path: Path,
) -> None:
    database = build_orders_database(tmp_path)
    original = database.read_bytes()
    for sql in (
        "DELETE FROM orders",
        "ATTACH DATABASE '/tmp/x' AS x",
        "SELECT 1; SELECT 2",
        "SELECT load_extension('/tmp/unsafe')",
    ):
        result = execute_readonly(
            database,
            sql,
            deadline_seconds=5,
            max_rows=10_000,
        )
        assert result.error_code == "UNSAFE_SQL"
    assert database.read_bytes() == original


def test_execution_deadline_and_row_limit_fail_closed(tmp_path: Path) -> None:
    database = build_orders_database(tmp_path)
    timeout = execute_readonly(
        database,
        "WITH RECURSIVE counter(x) AS ("
        "SELECT 1 UNION ALL SELECT x + 1 FROM counter"
        ") SELECT sum(x) FROM counter",
        deadline_seconds=0.001,
        max_rows=10_000,
    )
    limited = execute_readonly(
        database,
        "WITH RECURSIVE counter(x) AS ("
        "SELECT 1 UNION ALL SELECT x + 1 FROM counter WHERE x < 20"
        ") SELECT x FROM counter",
        deadline_seconds=5,
        max_rows=2,
    )
    assert timeout.error_code == "EXECUTION_TIMEOUT"
    assert limited.error_code == "RESULT_LIMIT_EXCEEDED"


def test_official_equivalence_restores_global_random_state(tmp_path: Path) -> None:
    database = build_orders_database(tmp_path)
    random.seed(1234)
    before = random.getstate()
    result = execution_matches(
        database,
        "fixture-task",
        "SELECT orders.id, customer_id, total, name FROM orders "
        "JOIN customers ON customers.id = orders.customer_id",
        "SELECT name, total, customer_id, orders.id FROM orders "
        "JOIN customers ON customers.id = orders.customer_id",
        deadline_seconds=5,
        max_rows=10_000,
    )
    assert result.equivalent is True
    assert random.getstate() == before


def test_order_by_makes_row_order_semantically_relevant(tmp_path: Path) -> None:
    database = build_orders_database(tmp_path)
    result = execution_matches(
        database,
        "fixture-order",
        "SELECT id FROM orders ORDER BY id ASC",
        "SELECT id FROM orders ORDER BY id DESC",
        deadline_seconds=5,
        max_rows=10_000,
    )
    assert result.equivalent is False
