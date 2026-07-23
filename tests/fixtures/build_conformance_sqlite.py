from __future__ import annotations

import csv
import sqlite3
from pathlib import Path


SCHEMAS = {
    "customers": "customer_id INTEGER NOT NULL, name TEXT NOT NULL",
    "order_items": "order_item_id INTEGER NOT NULL, order_id INTEGER NOT NULL, sku TEXT NOT NULL",
    "orders": "order_id INTEGER NOT NULL, customer_id INTEGER NOT NULL, total REAL NOT NULL",
    "payments": "payment_id INTEGER NOT NULL, order_id INTEGER NOT NULL, amount REAL NOT NULL",
}


def build_conformance_sqlite(csv_directory: Path, database: Path) -> None:
    connection = sqlite3.connect(database)
    try:
        for table_name, schema in SCHEMAS.items():
            with (csv_directory / f"{table_name}.csv").open(encoding="utf-8", newline="") as source:
                reader = csv.reader(source)
                headers = next(reader)
                rows = list(reader)
            connection.execute(f'CREATE TABLE "{table_name}" ({schema})')
            placeholders = ", ".join("?" for _ in headers)
            connection.executemany(f'INSERT INTO "{table_name}" VALUES ({placeholders})', rows)
        connection.commit()
    finally:
        connection.close()
