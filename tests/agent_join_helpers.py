from __future__ import annotations

import sqlite3
import json
import shutil
from pathlib import Path

from benchmarks.agent_join.projects import (
    apply_review_sheet,
    build_oracle_project,
    build_review_project,
)
from benchmarks.agent_join.prepare_spider import select_pilot
from joinlint.contracts import canonical_json


def build_orders_database(root: Path) -> Path:
    database = root / "orders.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE customers (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL,
                total REAL NOT NULL
            );
            INSERT INTO customers VALUES (1, 'Ada'), (2, 'Grace');
            INSERT INTO orders VALUES (10, 1, 12.5), (11, 1, 8.0), (12, 2, 20.0);
            """
        )
        connection.commit()
    finally:
        connection.close()
    return database


def orders_spider_metadata(db_id: str) -> dict[str, object]:
    return {
        "db_id": db_id,
        "table_names": ["customers", "orders"],
        "table_names_original": ["customers", "orders"],
        "column_names": [
            [-1, "*"],
            [0, "id"],
            [0, "name"],
            [1, "id"],
            [1, "customer_id"],
            [1, "total"],
        ],
        "column_names_original": [
            [-1, "*"],
            [0, "id"],
            [0, "name"],
            [1, "id"],
            [1, "customer_id"],
            [1, "total"],
        ],
        "column_types": ["text", "number", "text", "number", "number", "number"],
        "primary_keys": [1, 3],
        "foreign_keys": [[4, 1]],
    }


def build_mini_spider(root: Path) -> Path:
    spider = root / "spider"
    database_root = spider / "database"
    database_root.mkdir(parents=True)
    tables: list[dict[str, object]] = []
    dev: list[dict[str, object]] = []
    queries = [
        "SELECT c.name FROM customers c JOIN orders o ON c.id = o.customer_id",
        "SELECT o.total FROM orders o JOIN customers c ON o.customer_id = c.id",
        "SELECT count(*) FROM orders o, customers c WHERE c.id = o.customer_id",
        "SELECT c.id FROM customers c JOIN orders o ON o.customer_id = c.id WHERE o.total > 0",
    ]
    for database_index in range(5):
        db_id = f"mini_{database_index}"
        db_dir = database_root / db_id
        db_dir.mkdir()
        source = build_orders_database(db_dir)
        source.rename(db_dir / f"{db_id}.sqlite")
        tables.append(orders_spider_metadata(db_id))
        dev.extend(
            {
                "db_id": db_id,
                "question": f"Question {db_id} {query_index}",
                "query": query,
            }
            for query_index, query in enumerate(queries)
        )

    (spider / "tables.json").write_text(json.dumps(tables), encoding="utf-8")
    (spider / "dev.json").write_text(json.dumps(dev), encoding="utf-8")
    return spider


def oracle_schema() -> dict[str, object]:
    return orders_spider_metadata("orders")


def load_review_sheet(review_project: Path) -> dict[str, object]:
    document = json.loads((review_project / "review-decisions.json").read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def accept_only_order_customer(sheet: dict[str, object]) -> None:
    entities = sheet["entities"]
    assert isinstance(entities, dict)
    for table in ("customers", "orders"):
        row = entities[table]
        assert isinstance(row, dict)
        row["decision"] = "id"

    relationships = sheet["relationships"]
    assert isinstance(relationships, list)
    accepted = 0
    for row in relationships:
        assert isinstance(row, dict)
        if row["from"] == "orders.customer_id" and row["to"] == "customers.id":
            row["decision"] = "accept"
            accepted += 1
        else:
            row["decision"] = "reject"
    assert accepted == 1
    sheet["reviewer"] = "fixture-reviewer"
    sheet["reviewed_at"] = "2026-07-25T00:00:00Z"


def build_frozen_inputs(root: Path) -> Path:
    spider = build_mini_spider(root / "source")
    tasks = select_pilot(
        spider,
        seed=20260725,
        database_count=4,
        tasks_per_database=4,
    )
    evaluation = root / "evaluation"
    sealed = evaluation / "sealed" / "spider-pilot.json"
    sealed.parent.mkdir(parents=True)
    sealed.write_bytes(
        canonical_json([task.model_dump(mode="json", by_alias=True) for task in tasks])
    )
    metadata_by_db = {
        str(document["db_id"]): document
        for document in json.loads((spider / "tables.json").read_text(encoding="utf-8"))
    }
    for db_id in sorted({task.db_id for task in tasks}):
        source = spider / "database" / db_id / f"{db_id}.sqlite"
        build_oracle_project(
            source,
            metadata_by_db[db_id],
            evaluation / "projects" / "oracle" / db_id,
        )
        review = build_review_project(
            source,
            evaluation / "projects" / "joinlint" / db_id,
        )
        sheet = load_review_sheet(review)
        accept_only_order_customer(sheet)
        apply_review_sheet(review, sheet)

    first_db = sorted({task.db_id for task in tasks})[0]
    for case_id in ("safe_direct", "cardinality_mismatch", "compound_fanout", "stale_evidence"):
        shutil.copytree(
            evaluation / "projects" / "joinlint" / first_db,
            evaluation / "projects" / "safety" / case_id,
        )
    return evaluation
