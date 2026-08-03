from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from joinlint.errors import JoinLintError
from joinlint.mcp_contracts import GetJoinPlanRequest, ValidateSQLRequest
from joinlint.runtime.cache import RuntimeCache
from joinlint.runtime.domain import EntityRef
from joinlint.runtime.service import RuntimeService
from joinlint.runtime.sql import SQLValidationError


def make_database(path: Path, *, wal: bool = False) -> None:
    connection = sqlite3.connect(path)
    if wal:
        connection.execute("PRAGMA journal_mode = WAL")
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


def plan_request() -> GetJoinPlanRequest:
    return GetJoinPlanRequest(
        entity_refs=(
            EntityRef(ref="orders", entity="orders"),
            EntityRef(ref="customers", entity="customers"),
        ),
        start_ref="orders",
        expected_grain_ref="orders",
    )


def test_two_call_flow_reuses_one_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_database(tmp_path / "data.sqlite")
    service = RuntimeService(
        tmp_path,
        ("data.sqlite",),
        cache=RuntimeCache(tmp_path / "cache"),
    )
    import joinlint.runtime.service as service_module

    original = service_module.snapshot_sqlite
    calls = 0

    def counted_snapshot(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(service_module, "snapshot_sqlite", counted_snapshot)

    plan = service.get_join_plan(plan_request())
    assert plan.data is not None
    validation = service.validate_sql(
        ValidateSQLRequest(
            sql="SELECT * FROM orders o JOIN customers c ON o.customer_id = c.id",
            plan_id=plan.data.proof.plan_id,
        )
    )

    assert validation.status == "ok"
    assert calls == 1


def test_proof_bound_validation_rejects_a_different_expected_grain(tmp_path: Path) -> None:
    make_database(tmp_path / "data.sqlite")
    service = RuntimeService(
        tmp_path,
        ("data.sqlite",),
        cache=RuntimeCache(tmp_path / "cache"),
    )
    plan = service.get_join_plan(plan_request())
    assert plan.data is not None

    with pytest.raises(JoinLintError) as captured:
        service.validate_sql(
            ValidateSQLRequest(
                sql="SELECT * FROM orders o JOIN customers c ON o.customer_id = c.id",
                plan_id=plan.data.proof.plan_id,
                expected_grain_ref="customers",
            )
        )

    assert captured.value.code == "INVALID_ARGUMENT"
    assert captured.value.affected_refs == ("customers",)


@pytest.mark.parametrize("wal", [False, True])
def test_source_commit_invalidates_reused_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wal: bool,
) -> None:
    database = tmp_path / "data.sqlite"
    make_database(database, wal=wal)
    service = RuntimeService(
        tmp_path,
        ("data.sqlite",),
        cache=RuntimeCache(tmp_path / "cache"),
    )
    import joinlint.runtime.service as service_module

    original = service_module.snapshot_sqlite
    calls = 0

    def counted_snapshot(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(service_module, "snapshot_sqlite", counted_snapshot)
    plan = service.get_join_plan(plan_request())
    assert plan.data is not None

    connection = sqlite3.connect(database)
    connection.execute("INSERT INTO customers VALUES (3)")
    connection.commit()
    connection.close()

    with pytest.raises(Exception) as captured:
        service.validate_sql(
            ValidateSQLRequest(
                sql="SELECT * FROM orders o JOIN customers c ON o.customer_id = c.id",
                plan_id=plan.data.proof.plan_id,
            )
        )

    assert getattr(captured.value, "code", None) == "PROOF_STALE"
    assert calls == 2


def test_sql_resource_preflight_runs_before_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = RuntimeService(tmp_path, auto=True, cache=RuntimeCache(tmp_path / "cache"))
    import joinlint.runtime.service as service_module

    monkeypatch.setattr(
        service_module,
        "snapshot_sqlite",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("snapshot called")),
    )
    ctes = ", ".join(f"c{index} AS (SELECT {index} AS value)" for index in range(17))

    with pytest.raises(SQLValidationError) as captured:
        service.validate_sql(
            ValidateSQLRequest(sql=f"WITH {ctes} SELECT value FROM c0")
        )

    assert captured.value.code == "REQUEST_TOO_LARGE"
