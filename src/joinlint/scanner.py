from __future__ import annotations

import csv
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from joinlint.errors import JoinLintError
from joinlint.snapshots import SourceSnapshot


PhysicalType = Literal["integer", "number", "text", "boolean", "unknown"]
_INTEGER_PATTERN = re.compile(r"[+-]?\d+")


@dataclass(frozen=True)
class ColumnProfile:
    name: str
    physical_type: PhysicalType
    null_count: int
    distinct_count: int
    is_unique: bool


@dataclass(frozen=True)
class TableProfile:
    name: str
    row_count: int
    columns: tuple[ColumnProfile, ...]

    def column(self, name: str) -> ColumnProfile:
        for column in self.columns:
            if column.name == name:
                return column
        raise KeyError(name)


@dataclass(frozen=True)
class ScanCatalog:
    source_id: str
    tables: tuple[TableProfile, ...]

    def table(self, name: str) -> TableProfile:
        for table in self.tables:
            if table.name == name:
                return table
        raise KeyError(name)


def scan_snapshot(snapshot: SourceSnapshot) -> ScanCatalog:
    if snapshot.kind == "csv_directory":
        tables = _scan_csv(snapshot)
    else:
        tables = _scan_sqlite(snapshot)
    return ScanCatalog(
        source_id=snapshot.source_id,
        tables=tuple(sorted(tables, key=lambda table: table.name.encode("utf-8"))),
    )


def _scan_csv(snapshot: SourceSnapshot) -> list[TableProfile]:
    tables: list[TableProfile] = []
    for snapshot_file in snapshot.files:
        try:
            with snapshot_file.path.open(encoding="utf-8", newline="") as source:
                reader = csv.reader(source)
                headers = next(reader, None)
                if headers is None:
                    raise JoinLintError("UNSUPPORTED_SOURCE", "CSV file has no header", 2)
                rows = list(reader)
        except csv.Error as exc:
            raise JoinLintError("UNSUPPORTED_SOURCE", "CSV file is malformed", 2) from exc
        table_name = snapshot_file.relative_path.with_suffix("").as_posix()
        tables.append(_profile_table(table_name, headers, rows))
    return tables


def _scan_sqlite(snapshot: SourceSnapshot) -> list[TableProfile]:
    if len(snapshot.files) != 1:
        raise JoinLintError("INTERNAL_ERROR", "SQLite snapshot must have one database file", 4)
    connection = sqlite3.connect(f"file:{snapshot.files[0].path}?mode=ro", uri=True)
    try:
        table_rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        tables: list[TableProfile] = []
        for (table_name,) in table_rows:
            quoted_name = _quote_identifier(table_name)
            headers = [row[1] for row in connection.execute(f"PRAGMA table_info({quoted_name})")]
            rows = connection.execute(f"SELECT * FROM {quoted_name}").fetchall()
            tables.append(_profile_table(table_name, headers, rows))
        return tables
    except sqlite3.Error as exc:
        raise JoinLintError("INCONCLUSIVE_SCAN", "SQLite snapshot could not be read", 3) from exc
    finally:
        connection.close()


def _quote_identifier(identifier: str) -> str:
    return f'"{identifier.replace('"', '""')}"'


def _profile_table(name: str, headers: Sequence[str], rows: Iterable[Sequence[object]]) -> TableProfile:
    if not headers or any(not header for header in headers) or len(set(headers)) != len(headers):
        raise JoinLintError("UNSUPPORTED_SOURCE", "table has invalid column names", 2)
    values_by_column: dict[str, list[object]] = {header: [] for header in headers}
    row_count = 0
    for row in rows:
        if len(row) != len(headers):
            raise JoinLintError("UNSUPPORTED_SOURCE", "table row has an invalid field count", 2)
        row_count += 1
        for header, value in zip(headers, row, strict=True):
            values_by_column[header].append(value)
    columns = tuple(
        sorted(
            (_profile_column(header, values) for header, values in values_by_column.items()),
            key=lambda column: column.name.encode("utf-8"),
        )
    )
    return TableProfile(name=name, row_count=row_count, columns=columns)


def _profile_column(name: str, values: Sequence[object]) -> ColumnProfile:
    non_null = [value for value in values if value not in (None, "")]
    normalized = {_normalize_scalar(value) for value in non_null}
    return ColumnProfile(
        name=name,
        physical_type=_infer_type(non_null),
        null_count=len(values) - len(non_null),
        distinct_count=len(normalized),
        is_unique=len(non_null) == len(normalized),
    )


def _normalize_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format(Decimal(str(value)).normalize(), "f")
    return str(value)


def _infer_type(values: Sequence[object]) -> PhysicalType:
    inferred_types = {_infer_scalar_type(value) for value in values}
    if not inferred_types:
        return "unknown"
    if inferred_types == {"integer"}:
        return "integer"
    if inferred_types <= {"integer", "number"}:
        return "number"
    if inferred_types == {"boolean"}:
        return "boolean"
    return "text"


def _infer_scalar_type(value: object) -> PhysicalType:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    text = str(value)
    if text.lower() in {"true", "false"}:
        return "boolean"
    if _INTEGER_PATTERN.fullmatch(text):
        return "integer"
    try:
        Decimal(text)
    except InvalidOperation:
        return "text"
    return "number"
