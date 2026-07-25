from __future__ import annotations

import hashlib
import shutil
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import sqlglot
from sqlglot import exp

from benchmarks.agent_join.sql_edges import validate_readonly_select
from benchmarks.agent_join.vendor import spider_result_eq


_RESULT_EQ_LOCK = threading.Lock()
_ALLOWED_AUTHOR_ACTIONS = {
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_RECURSIVE,
    sqlite3.SQLITE_SELECT,
}


@dataclass(frozen=True)
class ExecutionResult:
    executed: bool
    equivalent: bool | None = None
    rows: tuple[tuple[object, ...], ...] | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.executed == (self.error_code is not None):
            raise ValueError("executed results cannot carry an error code")
        if self.rows is not None and self.equivalent is not None:
            raise ValueError("a result contains rows or equivalence, never both")


def execute_readonly(
    database: Path,
    sql: str,
    *,
    deadline_seconds: float,
    max_rows: int,
) -> ExecutionResult:
    if deadline_seconds <= 0 or max_rows <= 0:
        raise ValueError("execution limits must be positive")
    validated = validate_readonly_select(sql)
    if validated.sql is None:
        return ExecutionResult(executed=False, error_code=validated.error_code)
    if not database.is_file() or database.is_symlink():
        return ExecutionResult(executed=False, error_code="DATABASE_UNAVAILABLE")
    if database.with_name(database.name + "-wal").exists() or database.with_name(
        database.name + "-shm"
    ).exists():
        return ExecutionResult(executed=False, error_code="DATABASE_NOT_FROZEN")

    deadline = time.monotonic() + deadline_seconds
    with tempfile.TemporaryDirectory(prefix="joinlint-execution-") as temporary:
        snapshot = Path(temporary) / "database.sqlite"
        shutil.copy2(database, snapshot)
        uri = f"file:{quote(str(snapshot), safe='/')}?mode=ro&immutable=1"
        connection: sqlite3.Connection | None = None
        cursor: sqlite3.Cursor | None = None
        try:
            connection = sqlite3.connect(uri, uri=True)
            connection.set_authorizer(_authorizer)
            connection.set_progress_handler(
                lambda: 1 if time.monotonic() >= deadline else 0,
                1_000,
            )
            cursor = connection.execute(validated.sql)
            rows = cursor.fetchmany(max_rows + 1)
            if len(rows) > max_rows:
                return ExecutionResult(
                    executed=False,
                    error_code="RESULT_LIMIT_EXCEEDED",
                )
            return ExecutionResult(executed=True, rows=tuple(tuple(row) for row in rows))
        except sqlite3.Error as error:
            message = str(error).lower()
            if "interrupted" in message or time.monotonic() >= deadline:
                code = "EXECUTION_TIMEOUT"
            elif "not authorized" in message or "authorization denied" in message:
                code = "UNSAFE_SQL"
            else:
                code = "EXECUTION_ERROR"
            return ExecutionResult(executed=False, error_code=code)
        finally:
            if cursor is not None:
                cursor.close()
            if connection is not None:
                connection.close()


def execution_matches(
    database: Path,
    task_id: str,
    gold_sql: str,
    predicted_sql: str,
    *,
    deadline_seconds: float,
    max_rows: int,
) -> ExecutionResult:
    gold = execute_readonly(
        database,
        gold_sql,
        deadline_seconds=deadline_seconds,
        max_rows=max_rows,
    )
    if not gold.executed or gold.rows is None:
        return ExecutionResult(executed=False, error_code="GOLD_EXECUTION_FAILED")
    predicted = execute_readonly(
        database,
        predicted_sql,
        deadline_seconds=deadline_seconds,
        max_rows=max_rows,
    )
    if not predicted.executed or predicted.rows is None:
        return predicted

    order_matters = _order_matters(gold_sql)
    with _RESULT_EQ_LOCK:
        state = spider_result_eq.random.getstate()
        try:
            spider_result_eq.random.seed(hashlib.sha256(task_id.encode("utf-8")).digest())
            equivalent = spider_result_eq.result_eq(
                list(gold.rows),
                list(predicted.rows),
                order_matters=order_matters,
            )
        finally:
            spider_result_eq.random.setstate(state)
    return ExecutionResult(executed=True, equivalent=equivalent)


def _authorizer(
    action: int,
    argument_one: str | None,
    argument_two: str | None,
    database_name: str | None,
    trigger_name: str | None,
) -> int:
    del argument_one, database_name, trigger_name
    if action == sqlite3.SQLITE_FUNCTION and (argument_two or "").lower() == "load_extension":
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK if action in _ALLOWED_AUTHOR_ACTIONS else sqlite3.SQLITE_DENY


def _order_matters(sql: str) -> bool:
    try:
        expression = sqlglot.parse_one(sql, read="sqlite")
    except sqlglot.errors.ParseError:
        return False
    return expression.find(exp.Order) is not None
