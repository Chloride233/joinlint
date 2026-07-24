from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from joinlint.config import add_source
from joinlint.errors import JoinLintError
from joinlint.snapshots import fingerprint, snapshot_source


def test_fingerprint_uses_the_version_one_binary_frame() -> None:
    content_digest = hashlib.sha256(b"contents").hexdigest()
    actual = fingerprint([("orders.csv", 8, content_digest)])

    frame = b"joinlint-source-v1" + (10).to_bytes(8, "big") + b"orders.csv"
    frame += (8).to_bytes(8, "big") + bytes.fromhex(content_digest)
    assert actual == hashlib.sha256(frame).hexdigest()


def test_csv_membership_change_is_inconclusive(monkeypatch: pytest.MonkeyPatch, project: Path) -> None:
    directory = project / "data"
    directory.mkdir()
    (directory / "orders.csv").write_text("id\n1\n", encoding="utf-8")
    add_source(project, "sales", "data", "csv_directory")

    def add_extra_file() -> None:
        (directory / "late.csv").write_text("id\n2\n", encoding="utf-8")

    monkeypatch.setattr("joinlint.snapshots.after_copy_hook", add_extra_file)

    with pytest.raises(JoinLintError) as captured:
        snapshot_source(project, "sales")
    assert captured.value.code == "SOURCE_CHANGED_DURING_SCAN"


def test_csv_snapshot_timeout_is_inconclusive(monkeypatch: pytest.MonkeyPatch, project: Path) -> None:
    directory = project / "data"
    directory.mkdir()
    (directory / "orders.csv").write_text("id\n1\n", encoding="utf-8")
    (project / ".joinlint" / "config.yaml").write_text(
        """version: 1
sources:
  sales:
    kind: csv_directory
    path: data
    limits:
      max_source_bytes: 100
      max_scan_seconds: 1
""",
        encoding="utf-8",
    )

    class ExpiredClock:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self) -> float:
            self.calls += 1
            return 0.0 if self.calls == 1 else 2.0

    monkeypatch.setattr("joinlint.snapshots.monotonic", ExpiredClock())

    with pytest.raises(JoinLintError) as captured:
        snapshot_source(project, "sales")
    assert captured.value.code == "INCONCLUSIVE_SCAN"


def test_sqlite_snapshot_includes_committed_wal_rows(project: Path) -> None:
    directory = project / "data"
    directory.mkdir()
    database = directory / "app.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE orders (order_id INTEGER PRIMARY KEY)")
    connection.execute("INSERT INTO orders VALUES (1)")
    connection.commit()
    add_source(project, "warehouse", "data/app.sqlite", "sqlite")

    try:
        with snapshot_source(project, "warehouse") as snapshot:
            copied = sqlite3.connect(snapshot.files[0].path)
            try:
                assert copied.execute("SELECT count(*) FROM orders").fetchone()[0] == 1
            finally:
                copied.close()
    finally:
        connection.close()


def test_sqlite_snapshot_respects_source_byte_limit(project: Path) -> None:
    directory = project / "data"
    directory.mkdir()
    database = directory / "app.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE records (id INTEGER)")
        connection.commit()
    finally:
        connection.close()
    (project / ".joinlint" / "config.yaml").write_text(
        """version: 1
sources:
  warehouse:
    kind: sqlite
    path: data/app.sqlite
    limits:
      max_source_bytes: 1
      max_scan_seconds: 60
""",
        encoding="utf-8",
    )

    with pytest.raises(JoinLintError) as captured:
        snapshot_source(project, "warehouse")

    assert captured.value.code == "INCONCLUSIVE_SCAN"
