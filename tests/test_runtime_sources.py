from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from joinlint.errors import JoinLintError
from joinlint.runtime.sources import extract_sqlite_catalog, locate_sqlite_sources, snapshot_sqlite


def make_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE tenants(id INTEGER PRIMARY KEY);
        CREATE TABLE customers(
          tenant_id INTEGER NOT NULL,
          id INTEGER NOT NULL,
          manager_id INTEGER,
          PRIMARY KEY(tenant_id, id),
          FOREIGN KEY(tenant_id) REFERENCES tenants(id),
          FOREIGN KEY(tenant_id, manager_id) REFERENCES customers(tenant_id, id)
        );
        CREATE TABLE orders(
          id INTEGER PRIMARY KEY,
          tenant_id INTEGER NOT NULL,
          customer_id INTEGER NOT NULL,
          FOREIGN KEY(tenant_id, customer_id) REFERENCES customers(tenant_id, id)
        );
        """
    )
    connection.commit()
    connection.close()


def test_explicit_sources_override_auto_discovery(tmp_path: Path) -> None:
    make_database(tmp_path / "explicit.sqlite")
    make_database(tmp_path / "ignored.sqlite")

    sources = locate_sqlite_sources(tmp_path, ("explicit.sqlite",), auto=True)

    assert [source.relative_locator for source in sources] == ["explicit.sqlite"]


def test_auto_discovers_root_and_bounded_conventional_directories(tmp_path: Path) -> None:
    make_database(tmp_path / "root.sqlite")
    nested = tmp_path / "data" / "one" / "two"
    nested.mkdir(parents=True)
    make_database(nested / "nested.db")
    too_deep = nested / "three"
    too_deep.mkdir()
    make_database(too_deep / "ignored.sqlite")

    sources = locate_sqlite_sources(tmp_path)

    assert {source.relative_locator for source in sources} == {
        "root.sqlite",
        "data/one/two/nested.db",
    }


def test_discovery_rejects_symlinks_inside_data_boundary(tmp_path: Path) -> None:
    make_database(tmp_path / "real.sqlite")
    data = tmp_path / "data"
    data.mkdir()
    (data / "linked.sqlite").symlink_to(tmp_path / "real.sqlite")

    with pytest.raises(JoinLintError, match="symlinks"):
        locate_sqlite_sources(tmp_path)


def test_snapshot_and_catalog_include_composite_and_self_foreign_keys(tmp_path: Path) -> None:
    make_database(tmp_path / "commerce.sqlite")
    source = locate_sqlite_sources(tmp_path, ("commerce.sqlite",))[0]

    with snapshot_sqlite(tmp_path, source) as snapshot:
        catalog = extract_sqlite_catalog(snapshot)

    assert len(catalog.entities) == 3
    endpoints = {
        (seed.definition.child.columns, seed.definition.parent.columns)
        for seed in catalog.declared_relationships
    }
    assert (("tenant_id", "customer_id"), ("tenant_id", "id")) in endpoints
    assert (("tenant_id", "manager_id"), ("tenant_id", "id")) in endpoints
    assert all(seed.provenance == "declared" for seed in catalog.declared_relationships)


def test_catalog_deduplicates_repeated_sqlite_foreign_keys(tmp_path: Path) -> None:
    database = tmp_path / "duplicate.sqlite"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE parents(id INTEGER PRIMARY KEY);
        CREATE TABLE children(
          id INTEGER PRIMARY KEY,
          parent_id INTEGER,
          FOREIGN KEY(parent_id) REFERENCES parents,
          FOREIGN KEY(parent_id) REFERENCES parents
        );
        """
    )
    connection.close()
    source = locate_sqlite_sources(tmp_path, ("duplicate.sqlite",))[0]

    with snapshot_sqlite(tmp_path, source) as snapshot:
        catalog = extract_sqlite_catalog(snapshot)

    assert len(catalog.declared_relationships) == 1
    relationship = catalog.declared_relationships[0].definition
    assert relationship.child.columns == ("parent_id",)
    assert relationship.parent.columns == ("id",)
