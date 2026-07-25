from __future__ import annotations

import sqlite3
import json
from pathlib import Path


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
