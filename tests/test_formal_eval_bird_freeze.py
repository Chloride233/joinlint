from __future__ import annotations

import sqlite3
from pathlib import Path

from benchmarks.formal_eval.bird_freeze import (
    _database_scale,
    _fanout_type,
    _source_file,
    _unique_column_sets,
)


def test_database_scale_uses_frozen_byte_thresholds() -> None:
    assert _database_scale(64 * 1024 * 1024 - 1) == "small"
    assert _database_scale(64 * 1024 * 1024) == "medium"
    assert _database_scale(512 * 1024 * 1024 - 1) == "medium"
    assert _database_scale(512 * 1024 * 1024) == "large"


def test_fanout_classification_handles_one_to_many_and_compound() -> None:
    unique = (frozenset({"customers.id"}), frozenset({"orders.id"}))

    assert _fanout_type((("orders.customer_id", "customers.id"),), unique) == "one_to_many"
    assert _fanout_type(
        (
            ("orders.customer_id", "customers.id"),
            ("items.order_id", "orders.id"),
        ),
        unique,
    ) == "compound"
    assert _fanout_type((("left.value", "right.value"),), ()) == "many_to_many"


def test_unique_metadata_read_does_not_create_wal_sidecars(tmp_path: Path) -> None:
    database = tmp_path / "fixture.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
    connection.execute("CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER)")
    connection.commit()
    connection.close()

    unique = _unique_column_sets(database)

    assert frozenset({"parent.id"}) in unique
    assert frozenset({"child.id"}) in unique
    assert not database.with_name(database.name + "-wal").exists()
    assert not database.with_name(database.name + "-shm").exists()


def test_source_file_rejects_escape_and_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    source.write_bytes(b"source")
    link = tmp_path / "link.sqlite"
    link.symlink_to(source)

    assert _source_file(tmp_path, "source.sqlite") == source
    for value in ("../source.sqlite", "/source.sqlite", "nested\\source.sqlite", "link.sqlite"):
        try:
            _source_file(tmp_path, value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe source path was accepted: {value}")
