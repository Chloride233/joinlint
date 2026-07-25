from __future__ import annotations

import copy
import json
import stat
import zipfile
from collections import Counter
from pathlib import Path

import pytest

pytest.importorskip("inspect_ai")
pytest.importorskip("sqlglot")

from benchmarks.agent_join.prepare_spider import (  # noqa: E402
    classify_eligibility,
    find_spider_root,
    select_pilot,
    write_pilot_lock,
)
from tests.agent_join_helpers import build_mini_spider, orders_spider_metadata  # noqa: E402


def test_selection_is_seeded_balanced_and_independent_of_joinlint(tmp_path: Path) -> None:
    spider = build_mini_spider(tmp_path)
    first = select_pilot(
        spider,
        seed=20260725,
        database_count=4,
        tasks_per_database=4,
    )
    private = spider / ".joinlint" / "generated"
    private.mkdir(parents=True)
    (private / "candidates.json").write_text('{"changed":true}', encoding="utf-8")
    second = select_pilot(
        spider,
        seed=20260725,
        database_count=4,
        tasks_per_database=4,
    )
    assert first == second
    assert len(first) == 16
    assert set(Counter(task.db_id for task in first).values()) == {4}


def test_rendered_schema_contains_primary_keys_but_no_foreign_keys(tmp_path: Path) -> None:
    spider = build_mini_spider(tmp_path)
    task = select_pilot(
        spider,
        seed=20260725,
        database_count=4,
        tasks_per_database=4,
    )[0]
    assert "PRIMARY KEY" in task.schema_text
    assert "FOREIGN KEY" not in task.schema_text
    assert "REFERENCES" not in task.schema_text


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (
            "SELECT * FROM orders a JOIN orders b ON a.id = b.customer_id",
            "SELF_JOIN",
        ),
        (
            "SELECT * FROM orders o JOIN customers c "
            "ON o.customer_id = c.id AND o.id = c.id",
            "COMPOSITE_JOIN",
        ),
        (
            "SELECT * FROM orders o JOIN customers c ON o.customer_id > c.id",
            "NON_EQUALITY_JOIN",
        ),
        (
            "SELECT * FROM orders o JOIN customers c ON o.id = c.id",
            "MISSING_DECLARED_FK",
        ),
    ],
)
def test_ineligible_join_shapes_have_stable_reason_codes(query: str, expected: str) -> None:
    metadata = orders_spider_metadata("fixture")
    assert classify_eligibility(query, metadata) == expected


def test_joined_table_requires_exactly_one_primary_key() -> None:
    metadata = orders_spider_metadata("fixture")
    metadata["primary_keys"] = [1]
    query = "SELECT * FROM orders o JOIN customers c ON o.customer_id = c.id"
    assert classify_eligibility(query, metadata) == "UNSUPPORTED_GRAIN"


def test_dotted_table_name_is_ineligible() -> None:
    metadata = orders_spider_metadata("fixture")
    metadata["table_names_original"] = ["customer.data", "orders"]
    query = 'SELECT * FROM orders o JOIN "customer.data" c ON o.customer_id = c.id'
    assert classify_eligibility(query, metadata) == "DOTTED_TABLE_NAME"


def test_more_than_three_edges_is_ineligible() -> None:
    metadata = _chain_metadata()
    query = (
        "SELECT * FROM a JOIN b ON b.a_id = a.id "
        "JOIN c ON c.b_id = b.id JOIN d ON d.c_id = c.id JOIN e ON e.d_id = d.id"
    )
    assert classify_eligibility(query, metadata) == "JOIN_EDGE_COUNT"


def test_public_lock_contains_only_hashes_and_sealed_file_contains_sources(
    tmp_path: Path,
) -> None:
    spider = build_mini_spider(tmp_path)
    tasks = select_pilot(
        spider,
        seed=20260725,
        database_count=4,
        tasks_per_database=4,
    )
    archive = tmp_path / "spider.zip"
    archive.write_bytes(b"fixture archive identity")
    manifest = tmp_path / "tracked" / "manifest.json"
    hashes = tmp_path / "tracked" / "hashes.json"
    work = tmp_path / "work"
    write_pilot_lock(
        tasks,
        spider_root=spider,
        archive=archive,
        work_dir=work,
        manifest_path=manifest,
        hashes_path=hashes,
    )
    public_text = manifest.read_text(encoding="utf-8")
    assert tasks[0].question not in public_text
    assert tasks[0].gold_sql not in public_text
    assert len(json.loads(public_text)["tasks"]) == 16
    sealed = json.loads((work / "sealed" / "spider-pilot.json").read_text(encoding="utf-8"))
    assert len(sealed) == 16
    assert sealed[0]["question"]
    assert json.loads(hashes.read_text(encoding="utf-8"))["sources"]["archive"]


def test_archive_discovery_accepts_one_safe_spider_root(tmp_path: Path) -> None:
    spider = build_mini_spider(tmp_path / "source-fixture")
    archive = tmp_path / "spider.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for path in spider.rglob("*"):
            if path.is_file():
                bundle.write(path, Path("nested") / "spider" / path.relative_to(spider))
    discovered = find_spider_root(archive, tmp_path / "extracted")
    assert discovered.name == "spider"
    assert (discovered / "dev.json").is_file()


@pytest.mark.parametrize("unsafe_name", ["../escape", "/absolute", "C:/windows"])
def test_archive_discovery_rejects_escaping_paths(tmp_path: Path, unsafe_name: str) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(unsafe_name, "unsafe")
    with pytest.raises(ValueError, match="unsafe archive member"):
        find_spider_root(archive, tmp_path / "work")


def test_archive_discovery_rejects_symlinks(tmp_path: Path) -> None:
    archive = tmp_path / "symlink.zip"
    member = zipfile.ZipInfo("link")
    member.create_system = 3
    member.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(member, "target")
    with pytest.raises(ValueError, match="unsafe archive member"):
        find_spider_root(archive, tmp_path / "work")


def _chain_metadata() -> dict[str, object]:
    tables = ["a", "b", "c", "d", "e"]
    columns: list[list[object]] = [[-1, "*"]]
    types = ["text"]
    primary_keys = []
    foreign_keys = []
    id_indexes: list[int] = []
    previous_fk: int | None = None
    for table_index, table in enumerate(tables):
        del table
        id_indexes.append(len(columns))
        columns.append([table_index, "id"])
        types.append("number")
        primary_keys.append(len(columns) - 1)
        if table_index:
            previous_fk = len(columns)
            columns.append([table_index, f"{tables[table_index - 1]}_id"])
            types.append("number")
            foreign_keys.append([previous_fk, id_indexes[table_index - 1]])
    return {
        "db_id": "chain",
        "table_names": copy.copy(tables),
        "table_names_original": tables,
        "column_names": copy.deepcopy(columns),
        "column_names_original": columns,
        "column_types": types,
        "primary_keys": primary_keys,
        "foreign_keys": foreign_keys,
    }
