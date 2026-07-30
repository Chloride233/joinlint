from __future__ import annotations

import sqlite3
from pathlib import Path

from benchmarks.formal_eval.database_mcp import execute_readonly_sql, submit_sql_payload
from benchmarks.formal_eval.oracle_mcp import OracleDocument, plan_oracle, validate_oracle_sql


def test_evaluation_database_tool_is_read_only_and_bounded(tmp_path: Path) -> None:
    database = tmp_path / "data.sqlite"
    connection = sqlite3.connect(database)
    connection.executescript("CREATE TABLE items(id INTEGER); INSERT INTO items VALUES (1), (2);")
    connection.commit()
    connection.close()

    assert execute_readonly_sql(database, "SELECT id FROM items ORDER BY id") == {
        "status": "ok",
        "columns": ["id"],
        "rows": [[1], [2]],
    }
    assert execute_readonly_sql(database, "DELETE FROM items") == {
        "status": "error",
        "code": "UNSAFE_SQL",
    }
    assert execute_readonly_sql(database, "SELECT id FROM items", max_rows=1) == {
        "status": "error",
        "code": "RESULT_LIMIT_EXCEEDED",
    }


def test_submission_payload_acknowledges_without_echoing_sql() -> None:
    assert submit_sql_payload("SELECT secret FROM records", "") == {"status": "ok"}


def test_oracle_mcp_uses_the_same_two_tool_contract() -> None:
    document = OracleDocument(
        schema={
            "orders": {"customer_id": "INTEGER"},
            "customers": {"id": "INTEGER"},
        },
        allowed_graphs=((('orders.customer_id', 'customers.id'),),),
    )

    plan = plan_oracle(
        document,
        [
            {"ref": "orders", "entity": "orders"},
            {"ref": "customers", "entity": "customers"},
        ],
        "orders",
    )
    valid = validate_oracle_sql(
        document,
        "SELECT * FROM orders JOIN customers ON orders.customer_id = customers.id",
    )
    invalid = validate_oracle_sql(
        document,
        "SELECT * FROM orders JOIN customers ON orders.customer_id = customers.id + 1",
    )

    assert plan["schema_version"] == 3
    assert plan["status"] == "ok"
    assert plan["data"]["proof"]["claim_scope"] == "physical_join_only"
    assert valid["status"] == "ok"
    assert invalid["status"] == "findings"
    assert invalid["findings"][0]["guidance"]["next_action"] == "revise_sql"
