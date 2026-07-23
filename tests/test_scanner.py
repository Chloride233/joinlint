from __future__ import annotations

import shutil
from pathlib import Path

from joinlint.config import add_source
from joinlint.scanner import scan_snapshot
from joinlint.snapshots import snapshot_source
from tests.fixtures.build_conformance_sqlite import build_conformance_sqlite


FIXTURE_DIRECTORY = Path(__file__).parent / "fixtures" / "conformance" / "csv"


def test_profile_records_exact_counts_without_samples(project: Path) -> None:
    shutil.copytree(FIXTURE_DIRECTORY, project / "data")
    add_source(project, "sales", "data", "csv_directory")

    with snapshot_source(project, "sales") as snapshot:
        catalog = scan_snapshot(snapshot)

    order_id = catalog.table("orders").column("order_id")
    assert (order_id.null_count, order_id.distinct_count, order_id.is_unique) == (0, 3, True)
    assert not hasattr(order_id, "values")


def test_csv_and_sqlite_profiles_are_equal_for_conformance_fixture(tmp_path: Path) -> None:
    csv_project = tmp_path / "csv-project"
    sqlite_project = tmp_path / "sqlite-project"
    for project in (csv_project, sqlite_project):
        (project / ".joinlint").mkdir(parents=True)
        (project / ".joinlint" / "config.yaml").write_text("version: 1\nsources: {}\n", encoding="utf-8")
    shutil.copytree(FIXTURE_DIRECTORY, csv_project / "data")
    shutil.copytree(FIXTURE_DIRECTORY, sqlite_project / "csv-data")
    (sqlite_project / "data").mkdir()
    build_conformance_sqlite(sqlite_project / "csv-data", sqlite_project / "data" / "app.sqlite")
    add_source(csv_project, "sales", "data", "csv_directory")
    add_source(sqlite_project, "sales", "data/app.sqlite", "sqlite")

    with snapshot_source(csv_project, "sales") as csv_snapshot:
        csv_catalog = scan_snapshot(csv_snapshot)
    with snapshot_source(sqlite_project, "sales") as sqlite_snapshot:
        sqlite_catalog = scan_snapshot(sqlite_snapshot)

    assert csv_catalog == sqlite_catalog
