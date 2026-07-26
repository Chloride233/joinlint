from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from joinlint.contracts import canonical_json


def make_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE customers(id INTEGER PRIMARY KEY);
        CREATE TABLE orders(
          id INTEGER PRIMARY KEY,
          customer_id INTEGER NOT NULL,
          FOREIGN KEY(customer_id) REFERENCES customers(id)
        );
        INSERT INTO customers VALUES (1), (2);
        INSERT INTO orders VALUES (10, 1), (11, 1), (12, 2);
        """
    )
    connection.commit()
    connection.close()


def test_determinism_gate_across_process_cache_and_equivalent_inputs(tmp_path: Path) -> None:
    make_database(tmp_path / "data.sqlite")

    first = asyncio.run(_run_process(tmp_path, tmp_path / "cache-a"))
    second = asyncio.run(_run_process(tmp_path, tmp_path / "cache-b"))

    assert len(first) == len(second) == 10
    assert len(set(first)) == 1
    assert len(set(second)) == 1
    assert first[0] == second[0]


async def _run_process(project: Path, cache_root: Path) -> list[bytes]:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "joinlint",
            "serve-mcp",
            "--project",
            str(project),
            "--source",
            "data.sqlite",
        ],
        cwd=project,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
            "XDG_CACHE_HOME": str(cache_root),
        },
    )
    stable: list[bytes] = []
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for index in range(10):
                refs = [
                    {"ref": "orders", "entity": "orders"},
                    {"ref": "customers", "entity": "customers"},
                ]
                if index % 2:
                    refs.reverse()
                plan = await session.call_tool(
                    "get_join_plan",
                    {
                        "entity_refs": refs,
                        "start_ref": "orders",
                        "expected_grain_ref": "orders",
                    },
                )
                plan_body = _structured(plan.structuredContent)
                plan_id = plan_body["data"]["proof"]["plan_id"]
                sql = (
                    "SELECT * FROM orders o JOIN customers c ON o.customer_id = c.id"
                    if index % 2 == 0
                    else "SELECT * FROM customers x JOIN orders y ON x.id = y.customer_id"
                )
                validation = await session.call_tool(
                    "validate_sql",
                    {"sql": sql, "plan_id": plan_id},
                )
                validation_body = _structured(validation.structuredContent)
                stable.append(
                    canonical_json(
                        {
                            "plan": _without_observation_fields(plan_body),
                            "validation": _without_observation_fields(validation_body),
                        }
                    )
                )
    return stable


def _structured(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError("MCP response must be structured JSON")
    return value


def _without_observation_fields(value: object) -> object:
    document = json.loads(json.dumps(value))

    def scrub(item: object) -> None:
        if isinstance(item, dict):
            for key in list(item):
                if key in {"verified_at", "freshness_checked_at", "latency_ms"}:
                    item.pop(key)
                else:
                    scrub(item[key])
        elif isinstance(item, list):
            for child in item:
                scrub(child)

    scrub(document)
    return document
