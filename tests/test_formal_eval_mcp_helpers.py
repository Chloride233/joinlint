from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from benchmarks.formal_eval.database_mcp import execute_readonly_sql, submit_sql_payload
from benchmarks.formal_eval.deterministic import sanitized_mcp_environment
from benchmarks.formal_eval.oracle_mcp import OracleDocument, plan_oracle, validate_oracle_sql
from benchmarks.formal_eval.recording_joinlint_mcp import record_validation_outcome
from benchmarks.formal_eval.validation_failure_marker import (
    VALIDATION_FAILURE_MARKER_CLEAR,
    VALIDATION_FAILURE_MARKER_FAILED,
    ValidationFailureMarker,
)
from benchmarks.formal_eval.validation_ledger import (
    VALIDATION_LEDGER_WRITE_FAILED,
    ValidationLedger,
)


SAFE_JOIN_SQL = "SELECT * FROM orders o JOIN customers c ON o.customer_id = c.id"


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
    assert execute_readonly_sql(
        database,
        "SELECT sql FROM sqlite_schema WHERE name = 'items'",
    ) == {
        "status": "error",
        "code": "SYSTEM_CATALOG_ACCESS_DENIED",
    }
    assert execute_readonly_sql(database, "SELECT * FROM pragma_foreign_key_list('items')") == {
        "status": "error",
        "code": "SYSTEM_CATALOG_ACCESS_DENIED",
    }
    assert execute_readonly_sql(database, "SELECT id FROM items", max_rows=1) == {
        "status": "error",
        "code": "RESULT_LIMIT_EXCEEDED",
    }


def test_submission_payload_acknowledges_without_echoing_sql() -> None:
    assert submit_sql_payload("SELECT secret FROM records", "") == {"status": "ok"}


def test_submission_payload_requires_exact_successfully_validated_sql(tmp_path: Path) -> None:
    ledger = ValidationLedger(tmp_path / "validated-sql.json")
    ledger.record("SELECT id FROM records")

    assert submit_sql_payload(
        "SELECT id FROM records",
        "",
        validation_ledger=ledger,
    ) == {
        "status": "ok",
        "guard_contract_version": 1,
        "guard_decision": "accepted_validated_sql",
    }
    assert submit_sql_payload(
        "SELECT id FROM records ",
        "",
        validation_ledger=ledger,
    ) == {
        "status": "error",
        "code": "FINAL_SQL_NOT_VALIDATED",
        "guard_contract_version": 1,
        "guard_decision": "rejected_unvalidated_sql",
    }
    assert submit_sql_payload("", "no safe join", validation_ledger=ledger) == {
        "status": "ok",
        "guard_contract_version": 1,
        "guard_decision": "accepted_abstention",
    }


def test_submission_payload_fails_closed_on_non_utf8_validation_ledger(
    tmp_path: Path,
) -> None:
    ledger = ValidationLedger(tmp_path / "validated-sql.json")
    ledger.path.write_bytes(b"\xff")

    assert submit_sql_payload(
        "SELECT id FROM records",
        "",
        validation_ledger=ledger,
    ) == {
        "status": "error",
        "code": "FINAL_SQL_NOT_VALIDATED",
        "guard_contract_version": 1,
        "guard_decision": "rejected_unvalidated_sql",
    }


def test_recording_joinlint_mcp_records_only_successful_validation(tmp_path: Path) -> None:
    ledger = ValidationLedger(tmp_path / "validated-sql.json")

    record_validation_outcome(ledger, "SELECT id FROM records", {"status": "findings"})
    assert ledger.matches("SELECT id FROM records") is False

    record_validation_outcome(ledger, "SELECT id FROM records", {"status": "ok"})
    assert ledger.matches("SELECT id FROM records") is True


def test_validation_ledger_concurrent_records_use_distinct_temporary_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = ValidationLedger(tmp_path / "validated-sql.json")
    replace_barrier = Barrier(2)
    observed_sources: list[Path] = []
    original_replace = os.replace

    def synchronized_replace(source: Path, destination: Path) -> None:
        if Path(destination) == ledger.path:
            observed_sources.append(Path(source))
            replace_barrier.wait(timeout=5)
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", synchronized_replace)
    statements = ("SELECT 1", "SELECT 2")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(ledger.record, statement) for statement in statements]
        for future in futures:
            future.result()

    assert len(observed_sources) == 2
    assert len(set(observed_sources)) == 2
    assert sum(ledger.matches(statement) for statement in statements) == 1


def test_recording_joinlint_mcp_reports_ledger_write_as_infrastructure_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = ValidationLedger(tmp_path / "validated-sql.json")
    marker_path = tmp_path / "validation-failure.marker"
    marker_path.write_bytes(VALIDATION_FAILURE_MARKER_CLEAR)
    marker = ValidationFailureMarker(marker_path)
    response: dict[str, object] = {"status": "ok"}

    def fail_record(sql: str) -> None:
        del sql
        raise OSError("read-only filesystem")

    monkeypatch.setattr(ledger, "record", fail_record)

    try:
        record_validation_outcome(
            ledger,
            "SELECT id FROM records",
            response,
            failure_marker=marker,
        )
    finally:
        marker.close()

    assert response["status"] == "error"
    assert response["error"]["code"] == VALIDATION_LEDGER_WRITE_FAILED  # type: ignore[index]
    assert marker_path.read_bytes() == VALIDATION_FAILURE_MARKER_FAILED
    assert "read-only filesystem" not in repr(response)
    assert str(tmp_path) not in repr(response)


@pytest.mark.parametrize(
    "marker_error",
    [OSError("secret marker path"), ValueError("malformed secret marker path")],
    ids=("write-error", "malformed-marker"),
)
def test_recording_joinlint_mcp_preserves_stable_error_when_marker_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    marker_error: Exception,
) -> None:
    ledger = ValidationLedger(tmp_path / "validated-sql.json")
    marker_path = tmp_path / "validation-failure.marker"
    marker_path.write_bytes(VALIDATION_FAILURE_MARKER_CLEAR)
    marker = ValidationFailureMarker(marker_path)
    response: dict[str, object] = {"status": "ok"}

    def fail_record(sql: str) -> None:
        del sql
        raise OSError("secret ledger path")

    def fail_marker() -> None:
        raise marker_error

    monkeypatch.setattr(ledger, "record", fail_record)
    monkeypatch.setattr(marker, "mark_failed", fail_marker)

    try:
        record_validation_outcome(
            ledger,
            "SELECT id FROM records",
            response,
            failure_marker=marker,
        )
    finally:
        marker.close()

    assert response["status"] == "error"
    assert response["error"]["code"] == VALIDATION_LEDGER_WRITE_FAILED  # type: ignore[index]
    assert marker_path.read_bytes() == VALIDATION_FAILURE_MARKER_CLEAR
    assert "secret ledger path" not in repr(response)
    assert "secret marker path" not in repr(response)
    assert str(tmp_path) not in repr(response)


def test_real_stdio_ledger_failure_marker_is_durable_before_response_returns(
    tmp_path: Path,
) -> None:
    database = tmp_path / "data.sqlite"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE customers(id INTEGER PRIMARY KEY);
        CREATE TABLE orders(
          id INTEGER PRIMARY KEY,
          customer_id INTEGER NOT NULL,
          FOREIGN KEY(customer_id) REFERENCES customers(id)
        );
        """
    )
    connection.commit()
    connection.close()
    marker = tmp_path / "validation-failure.marker"
    marker.write_bytes(VALIDATION_FAILURE_MARKER_CLEAR)

    response = asyncio.run(
        asyncio.wait_for(
            _failed_validation_process_round_trip(
                tmp_path,
                tmp_path / "missing" / "validated-sql.json",
                marker,
            ),
            timeout=20,
        )
    )

    assert response["status"] == "error"
    assert response["error"]["code"] == VALIDATION_LEDGER_WRITE_FAILED  # type: ignore[index]
    assert marker.read_bytes() == VALIDATION_FAILURE_MARKER_FAILED


def test_guarded_submission_across_two_real_stdio_mcp_processes(tmp_path: Path) -> None:
    database = tmp_path / "data.sqlite"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE customers(id INTEGER PRIMARY KEY);
        CREATE TABLE orders(
          id INTEGER PRIMARY KEY,
          customer_id INTEGER NOT NULL,
          FOREIGN KEY(customer_id) REFERENCES customers(id)
        );
        INSERT INTO customers VALUES (1), (2);
        INSERT INTO orders VALUES (10, 1), (11, 2);
        """
    )
    connection.commit()
    connection.close()
    ledger = tmp_path / "validated-sql.json"
    failure_marker = tmp_path / "validation-failure.marker"
    failure_marker.write_bytes(VALIDATION_FAILURE_MARKER_CLEAR)
    ValidationLedger(ledger).record(SAFE_JOIN_SQL)

    before_validation, exact, drifted = asyncio.run(
        asyncio.wait_for(
            _guarded_submission_process_round_trip(
                tmp_path,
                database,
                ledger,
                failure_marker,
            ),
            timeout=20,
        )
    )

    assert before_validation == {
        "status": "error",
        "code": "FINAL_SQL_NOT_VALIDATED",
        "guard_contract_version": 1,
        "guard_decision": "rejected_unvalidated_sql",
    }
    assert exact == {
        "status": "ok",
        "guard_contract_version": 1,
        "guard_decision": "accepted_validated_sql",
    }
    assert drifted == {
        "status": "error",
        "code": "FINAL_SQL_NOT_VALIDATED",
        "guard_contract_version": 1,
        "guard_decision": "rejected_unvalidated_sql",
    }


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


async def _guarded_submission_process_round_trip(
    project: Path,
    database: Path,
    ledger: Path,
    failure_marker: Path,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    repository = Path(__file__).parents[1]
    environment = {
        **sanitized_mcp_environment(),
        "PYTHONPATH": os.pathsep.join((str(repository), str(repository / "src"))),
        "XDG_CACHE_HOME": str(project / "cache"),
    }
    database_server = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "benchmarks.formal_eval.database_mcp",
            "--database",
            str(database),
            "--validation-ledger",
            str(ledger),
        ],
        cwd=project,
        env=environment,
    )
    recording_server = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "benchmarks.formal_eval.recording_joinlint_mcp",
            "--project",
            str(project),
            "--validation-ledger",
            str(ledger),
            "--validation-failure-marker",
            str(failure_marker),
        ],
        cwd=project,
        env=environment,
    )
    async with stdio_client(database_server) as (database_read, database_write):
        async with ClientSession(database_read, database_write) as database_session:
            await database_session.initialize()
            execution = await database_session.call_tool(
                "execute_sql",
                {"sql": "SELECT COUNT(*) AS order_count FROM orders"},
            )
            assert _structured(execution.structuredContent) == {
                "status": "ok",
                "columns": ["order_count"],
                "rows": [[2]],
            }
            before_validation = await database_session.call_tool(
                "submit_sql",
                {"sql": SAFE_JOIN_SQL, "warning": ""},
            )
            async with stdio_client(recording_server) as (joinlint_read, joinlint_write):
                async with ClientSession(joinlint_read, joinlint_write) as joinlint_session:
                    await joinlint_session.initialize()
                    plan = await joinlint_session.call_tool(
                        "get_join_plan",
                        {
                            "entity_refs": [
                                {"ref": "orders", "entity": "orders"},
                                {"ref": "customers", "entity": "customers"},
                            ],
                            "start_ref": "orders",
                            "expected_grain_ref": "orders",
                        },
                    )
                    plan_body = _structured(plan.structuredContent)
                    plan_id = plan_body["data"]["proof"]["plan_id"]  # type: ignore[index]
                    validation = await joinlint_session.call_tool(
                        "validate_sql",
                        {"sql": SAFE_JOIN_SQL, "plan_id": plan_id},
                    )
                    assert _structured(validation.structuredContent)["status"] == "ok"
                    exact = await database_session.call_tool(
                        "submit_sql",
                        {"sql": SAFE_JOIN_SQL, "warning": ""},
                    )
                    drifted = await database_session.call_tool(
                        "submit_sql",
                        {"sql": SAFE_JOIN_SQL + " ", "warning": ""},
                    )
    return (
        _structured(before_validation.structuredContent),
        _structured(exact.structuredContent),
        _structured(drifted.structuredContent),
    )


async def _failed_validation_process_round_trip(
    project: Path,
    ledger: Path,
    failure_marker: Path,
) -> dict[str, object]:
    repository = Path(__file__).parents[1]
    environment = {
        **sanitized_mcp_environment(),
        "PYTHONPATH": os.pathsep.join((str(repository), str(repository / "src"))),
        "XDG_CACHE_HOME": str(project / "cache"),
    }
    recording_server = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "benchmarks.formal_eval.recording_joinlint_mcp",
            "--project",
            str(project),
            "--validation-ledger",
            str(ledger),
            "--validation-failure-marker",
            str(failure_marker),
        ],
        cwd=project,
        env=environment,
    )
    async with stdio_client(recording_server) as (joinlint_read, joinlint_write):
        async with ClientSession(joinlint_read, joinlint_write) as joinlint_session:
            await joinlint_session.initialize()
            plan = await joinlint_session.call_tool(
                "get_join_plan",
                {
                    "entity_refs": [
                        {"ref": "orders", "entity": "orders"},
                        {"ref": "customers", "entity": "customers"},
                    ],
                    "start_ref": "orders",
                    "expected_grain_ref": "orders",
                },
            )
            plan_body = _structured(plan.structuredContent)
            plan_id = plan_body["data"]["proof"]["plan_id"]  # type: ignore[index]
            validation = await joinlint_session.call_tool(
                "validate_sql",
                {"sql": SAFE_JOIN_SQL, "plan_id": plan_id},
            )
    return _structured(validation.structuredContent)


def _structured(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value
