from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP

from benchmarks.agent_join.sql_edges import validate_readonly_select


def execute_readonly_sql(
    database: Path,
    sql: str,
    *,
    deadline_seconds: float = 5.0,
    max_rows: int = 1_000,
) -> dict[str, object]:
    if len(sql.encode("utf-8")) > 65_536:
        return {"status": "error", "code": "SQL_TOO_LARGE"}
    validated = validate_readonly_select(sql)
    if validated.sql is None:
        return {"status": "error", "code": validated.error_code or "UNSAFE_SQL"}
    if database.is_symlink() or not database.is_file():
        return {"status": "error", "code": "DATABASE_UNAVAILABLE"}
    path = database.resolve(strict=True)
    uri = f"file:{quote(path.as_posix(), safe='/')}?mode=ro"
    started = time.monotonic()
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.set_progress_handler(
            lambda: 1 if time.monotonic() - started > deadline_seconds else 0,
            1_000,
        )
        cursor = connection.execute(validated.sql)
        rows = cursor.fetchmany(max_rows + 1)
        if len(rows) > max_rows:
            return {"status": "error", "code": "RESULT_LIMIT_EXCEEDED"}
        return {
            "status": "ok",
            "columns": [item[0] for item in cursor.description or ()],
            "rows": [list(row) for row in rows],
        }
    except sqlite3.OperationalError as error:
        code = "STATEMENT_TIMEOUT" if "interrupted" in str(error).lower() else "SQL_EXECUTION_ERROR"
        return {"status": "error", "code": code}
    finally:
        connection.close()


def create_database_server(database: Path) -> FastMCP:
    mcp = FastMCP("EvaluationDatabase")

    @mcp.tool(name="execute_sql")
    def execute_sql(sql: str) -> dict[str, object]:
        """Execute one bounded read-only SQLite SELECT for the evaluation task."""
        return execute_readonly_sql(database, sql)

    return mcp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    arguments = parser.parse_args(argv)
    create_database_server(arguments.database).run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

