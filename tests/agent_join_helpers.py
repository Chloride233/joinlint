from __future__ import annotations

import sqlite3
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
