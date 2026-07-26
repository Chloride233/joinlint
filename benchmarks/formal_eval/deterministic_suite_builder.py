from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import rfc8785

from benchmarks.agent_join.sql_edges import canonical_edge
from benchmarks.formal_eval.bird_dataset import _check_sqlite, _file_record
from benchmarks.formal_eval.deterministic import (
    DeterministicSuite,
    PerformanceSpec,
    RelationshipEvidenceCase,
    SQLValidationCase,
)


DECLARED_CASES = (
    ("california_schools", "education", "benchmarks/formal_eval/sealed/bird-natural-review", "databases/california_schools.sqlite", ("frpm", "schools"), (("frpm.CDSCode", "schools.CDSCode"),)),
    ("card_games", "gaming", "benchmarks/formal_eval/sealed/bird-natural-review", "databases/card_games.sqlite", ("rulings", "cards"), (("rulings.uuid", "cards.uuid"),)),
    ("codebase_community", "software", "benchmarks/formal_eval/sealed/bird-natural-review", "databases/codebase_community.sqlite", ("posts", "users"), (("posts.OwnerUserId", "users.Id"),)),
    ("debit_card_specializing", "finance", "benchmarks/formal_eval/sealed/bird-natural-review", "databases/debit_card_specializing.sqlite", ("yearmonth", "customers"), (("yearmonth.CustomerID", "customers.CustomerID"),)),
    ("citeseer", "research", "benchmarks/formal_eval/sealed/bird-train-subset", "databases/citeseer.sqlite", ("content", "paper"), (("content.paper_id", "paper.paper_id"),)),
    ("european_football_2", "sports", "benchmarks/formal_eval/sealed/bird-natural-review", "databases/european_football_2.sqlite", ("Player_Attributes", "Player"), (("Player_Attributes.player_api_id", "Player.player_api_id"),)),
    ("financial", "finance", "benchmarks/formal_eval/sealed/bird-natural-review", "databases/financial.sqlite", ("disp", "account"), (("disp.account_id", "account.account_id"),)),
    ("formula_1", "sports", "benchmarks/formal_eval/sealed/bird-natural-review", "databases/formula_1.sqlite", ("constructorStandings", "constructors"), (("constructorStandings.constructorId", "constructors.constructorId"),)),
    ("student_club", "education", "benchmarks/formal_eval/sealed/bird-natural-review", "databases/student_club.sqlite", ("attendance", "event"), (("attendance.link_to_event", "event.event_id"),)),
    ("superhero", "entertainment", "benchmarks/formal_eval/sealed/bird-natural-review", "databases/superhero.sqlite", ("superhero", "race"), (("superhero.race_id", "race.id"),)),
    ("thrombosis_prediction", "healthcare", "benchmarks/formal_eval/sealed/bird-natural-review", "databases/thrombosis_prediction.sqlite", ("Laboratory", "Patient"), (("Laboratory.ID", "Patient.ID"),)),
    ("toxicology", "science", "benchmarks/formal_eval/sealed/bird-natural-review", "databases/toxicology.sqlite", ("connected", "atom"), (("connected.atom_id2", "atom.atom_id"),)),
)
ADVERSARIAL_TYPES = (
    "same_name_id",
    "integer_domain_collision",
    "small_domain_overlap",
    "multiple_parents",
    "non_unique_parent",
    "orphan",
    "drift",
)


def build_deterministic_suite(sealed_root: Path, output: Path) -> dict[str, Any]:
    sealed = _real_directory(sealed_root, "sealed root")
    parent = _real_directory(output.parent, "deterministic output parent")
    try:
        parent.relative_to(sealed)
        project_identifier = output.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError as error:
        raise ValueError("deterministic output must stay inside the repository sealed root") from error
    output = parent / output.name
    if output.exists() or output.is_symlink():
        raise ValueError("deterministic output already exists")

    staging = output.with_name(f".{output.name}.staging-{uuid.uuid4().hex}")
    (staging / "sql").mkdir(parents=True)
    (staging / "adversarial").mkdir()
    try:
        for index in range(20):
            _create_safety_database(staging / "sql" / f"v{index:02d}.sqlite", index)
        for trap in ADVERSARIAL_TYPES:
            _create_adversarial_database(
                staging / "adversarial" / f"{trap}.sqlite",
                trap,
            )
        _create_million_row_database(staging / "million.sqlite")

        relationships = _relationship_cases(project_identifier)
        sql_cases = tuple(
            case
            for index in range(20)
            for case in _sql_cases(project_identifier, index)
        )
        suite = DeterministicSuite(
            relationship_scope="declared_only",
            relationship_cases=relationships,
            sql_validation_cases=sql_cases,
            performance=PerformanceSpec(
                project_path=project_identifier,
                source="sql/v00.sqlite",
                entities=("v00_orders", "v00_customers"),
                safe_sql=(
                    "SELECT * FROM v00_orders o JOIN v00_customers c "
                    "ON o.customer_id = c.id"
                ),
                million_row_project_path=project_identifier,
                million_row_source="million.sqlite",
                million_row_sql=(
                    "SELECT * FROM million_children c JOIN million_parents p "
                    "ON c.parent_id = p.id"
                ),
                repeats=20,
            ),
        )
        suite_path = staging / "deterministic-suite.json"
        _write_canonical(suite_path, suite.model_dump(mode="json"))
        files = [
            {
                "path": path.relative_to(staging).as_posix(),
                **_file_record(path),
            }
            for path in sorted(staging.rglob("*.sqlite"))
        ]
        report = {
            "schema_version": 1,
            "status": "candidate_deterministic_suite",
            "relationship_scope": "declared_only",
            "relationship_case_count": len(relationships),
            "declared_relationship_case_count": 12,
            "adversarial_abstention_case_count": len(ADVERSARIAL_TYPES),
            "sql_safe_count": sum(case.sql_class == "safe" for case in sql_cases),
            "sql_dangerous_count": sum(case.sql_class == "dangerous" for case in sql_cases),
            "sql_schema_variant_count": 20,
            "sql_shape_counts": dict(sorted(Counter(case.sql_shape for case in sql_cases).items())),
            "danger_type_counts": dict(
                sorted(
                    Counter(
                        case.danger_type
                        for case in sql_cases
                        if case.danger_type is not None
                    ).items()
                )
            ),
            "suite": _file_record(suite_path),
            "files": files,
            "claim_boundary": "stage1_declared_only",
        }
        _write_canonical(staging / "source-manifest.json", report)
        os.replace(staging, output)
        return report
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _relationship_cases(project_identifier: str) -> tuple[RelationshipEvidenceCase, ...]:
    cases = [
        RelationshipEvidenceCase(
            case_id=f"declared-{database_id}",
            database_id=database_id,
            variant_group=f"bird-{database_id}",
            project_path=project,
            source=source,
            entities=entities,
            expected_edges=edges,
            corpus="natural",
            source_type="sqlite",
            domain=domain,
            split="confirmatory",
            provenance="declared",
            eligible=True,
            expected_relation=True,
            fk_state="visible",
        )
        for database_id, domain, project, source, entities, edges in DECLARED_CASES
    ]
    cases.extend(
        RelationshipEvidenceCase(
            case_id=f"adversarial-{trap}",
            database_id=f"adversarial-{trap}",
            variant_group=f"stage1-adversarial-{trap}",
            project_path=project_identifier,
            source=f"adversarial/{trap}.sqlite",
            entities=("children", "parents"),
            expected_edges=(),
            corpus="adversarial",
            source_type="sqlite",
            domain="adversarial",
            split="confirmatory",
            provenance="declared",
            eligible=True,
            expected_relation=False,
            adversarial_type=trap,  # type: ignore[arg-type]
        )
        for trap in ADVERSARIAL_TYPES
    )
    return tuple(sorted(cases, key=lambda case: case.case_id.encode("utf-8")))


def _sql_cases(project: str, index: int) -> tuple[SQLValidationCase, ...]:
    prefix = f"v{index:02d}_"
    source = f"sql/v{index:02d}.sqlite"
    customers = f"{prefix}customers"
    orders = f"{prefix}orders"
    items = f"{prefix}order_items"
    products = f"{prefix}products"
    stores = f"{prefix}stores"
    regions = f"{prefix}regions"
    tenants = f"{prefix}tenant_customers"
    tenant_orders = f"{prefix}tenant_orders"
    bad_parents = f"{prefix}bad_parents"
    bad_children = f"{prefix}bad_children"
    orphan_parents = f"{prefix}orphan_parents"
    orphan_children = f"{prefix}orphan_children"
    edge = canonical_edge
    valid_edges = (
        edge(f"{orders}.customer_id", f"{customers}.id"),
        edge(f"{items}.order_id", f"{orders}.id"),
        edge(f"{items}.product_id", f"{products}.id"),
        edge(f"{stores}.region_id", f"{regions}.region_id"),
        edge(f"{customers}.manager_id", f"{customers}.id"),
        edge(f"{tenant_orders}.tenant_id", f"{tenants}.tenant_id"),
        edge(f"{tenant_orders}.customer_id", f"{tenants}.id"),
    )

    def case(
        name: str,
        sql: str,
        sql_class: str,
        shape: str,
        expected: tuple[tuple[str, str], ...],
        *,
        danger: str | None = None,
        supported: bool = True,
        grain: str | None = None,
        plan_entities: tuple[str, ...] = (),
    ) -> SQLValidationCase:
        return SQLValidationCase(
            case_id=f"sql-v{index:02d}-{name}",
            project_path=project,
            source=source,
            sql=sql,
            sql_class=sql_class,  # type: ignore[arg-type]
            supported_shape=supported,
            sql_shape=shape,  # type: ignore[arg-type]
            danger_type=danger,  # type: ignore[arg-type]
            expected_edges=expected,
            auto_usable_edges=valid_edges,
            expected_grain_ref=grain,
            plan_entities=plan_entities,
        )

    safe = (
        case("safe-alias", f"SELECT {index} FROM {orders} o JOIN {customers} c ON o.customer_id=c.id", "safe", "alias", (edge(f"{orders}.customer_id", f"{customers}.id"),)),
        case("safe-cte", f"WITH o AS (SELECT customer_id FROM {orders}) SELECT * FROM o JOIN {customers} c ON o.customer_id=c.id", "safe", "cte", (edge(f"{orders}.customer_id", f"{customers}.id"),)),
        case("safe-subquery", f"SELECT * FROM (SELECT customer_id FROM {orders}) o JOIN {customers} c ON o.customer_id=c.id", "safe", "subquery", (edge(f"{orders}.customer_id", f"{customers}.id"),)),
        case("safe-where", f"SELECT * FROM {orders} o, {customers} c WHERE o.customer_id=c.id", "safe", "where_equality", (edge(f"{orders}.customer_id", f"{customers}.id"),)),
        case("safe-using", f"SELECT * FROM {stores} s JOIN {regions} r USING (region_id)", "safe", "using", (edge(f"{stores}.region_id", f"{regions}.region_id"),)),
        case("safe-self", f"SELECT * FROM {customers} e JOIN {customers} m ON e.manager_id=m.id", "safe", "self_join", (edge(f"{customers}.manager_id", f"{customers}.id"),)),
        case("safe-composite", f"SELECT * FROM {tenant_orders} o JOIN {tenants} c ON o.tenant_id=c.tenant_id AND o.customer_id=c.id", "safe", "composite", (edge(f"{tenant_orders}.customer_id", f"{tenants}.id"), edge(f"{tenant_orders}.tenant_id", f"{tenants}.tenant_id"))),
        case("safe-left", f"SELECT * FROM {orders} o LEFT JOIN {customers} c ON o.customer_id=c.id", "safe", "alias", (edge(f"{orders}.customer_id", f"{customers}.id"),), grain=orders),
        case("safe-multihop", f"SELECT * FROM {items} i JOIN {orders} o ON i.order_id=o.id JOIN {products} p ON i.product_id=p.id", "safe", "alias", (edge(f"{items}.order_id", f"{orders}.id"), edge(f"{items}.product_id", f"{products}.id")), grain=items),
        case("safe-reverse", f"SELECT * FROM {customers} c JOIN {orders} o ON c.id=o.customer_id", "safe", "alias", (edge(f"{orders}.customer_id", f"{customers}.id"),)),
    )
    dangerous = (
        case("danger-wrong", f"SELECT * FROM {orders} o JOIN {customers} c ON o.id=c.id", "dangerous", "wrong_edge", (edge(f"{orders}.id", f"{customers}.id"),), danger="unsupported_edge"),
        case("danger-missing", f"SELECT * FROM {orders}", "dangerous", "wrong_edge", (), danger="missing_required_edge", plan_entities=(orders, customers)),
        case("danger-unique", f"SELECT * FROM {bad_children} c JOIN {bad_parents} p ON c.parent_code=p.code", "dangerous", "wrong_edge", (edge(f"{bad_children}.parent_code", f"{bad_parents}.code"),), danger="uniqueness"),
        case("danger-orphan", f"SELECT * FROM {orphan_children} c JOIN {orphan_parents} p ON c.parent_id=p.id", "dangerous", "wrong_edge", (edge(f"{orphan_children}.parent_id", f"{orphan_parents}.id"),), danger="orphan"),
        case("danger-cardinality", f"SELECT * FROM {customers} c LEFT JOIN {orders} o ON c.id=o.customer_id", "dangerous", "grain_change", (edge(f"{orders}.customer_id", f"{customers}.id"),), danger="cardinality", grain=customers),
        case("danger-grain", f"SELECT * FROM {customers} c JOIN {orders} o ON c.id=o.customer_id", "dangerous", "grain_change", (edge(f"{orders}.customer_id", f"{customers}.id"),), danger="grain", grain=customers),
        case("danger-many", f"SELECT * FROM {products} p JOIN {items} i ON p.id=i.product_id JOIN {orders} o ON i.order_id=o.id", "dangerous", "many_to_many", (edge(f"{items}.product_id", f"{products}.id"), edge(f"{items}.order_id", f"{orders}.id")), danger="many_to_many", grain=products),
        case("danger-compound", f"SELECT * FROM {customers} c JOIN {orders} o ON c.id=o.customer_id JOIN {items} i ON o.id=i.order_id", "dangerous", "compound_fanout", (edge(f"{orders}.customer_id", f"{customers}.id"), edge(f"{items}.order_id", f"{orders}.id")), danger="compound_fanout", grain=customers),
        case("danger-dml", f"DELETE FROM {orders}", "dangerous", "ddl_dml", (), danger="unsafe_statement", supported=False),
        case("danger-multi", f"SELECT * FROM {orders}; SELECT * FROM {customers}", "dangerous", "multi_statement", (), danger="multi_statement", supported=False),
    )
    return safe + dangerous


def _create_safety_database(path: Path, index: int) -> None:
    p = f"v{index:02d}_"
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript(
            f"""
            CREATE TABLE {p}customers(id INTEGER PRIMARY KEY, manager_id INTEGER REFERENCES {p}customers(id));
            CREATE TABLE {p}orders(id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES {p}customers(id));
            CREATE TABLE {p}products(id INTEGER PRIMARY KEY);
            CREATE TABLE {p}order_items(id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES {p}orders(id), product_id INTEGER NOT NULL REFERENCES {p}products(id));
            CREATE TABLE {p}regions(region_id INTEGER PRIMARY KEY);
            CREATE TABLE {p}stores(id INTEGER PRIMARY KEY, region_id INTEGER NOT NULL REFERENCES {p}regions(region_id));
            CREATE TABLE {p}tenant_customers(tenant_id INTEGER NOT NULL, id INTEGER NOT NULL, PRIMARY KEY(tenant_id,id));
            CREATE TABLE {p}tenant_orders(id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, customer_id INTEGER NOT NULL, FOREIGN KEY(tenant_id,customer_id) REFERENCES {p}tenant_customers(tenant_id,id));
            CREATE TABLE {p}bad_parents(id INTEGER PRIMARY KEY, code TEXT NOT NULL);
            CREATE TABLE {p}bad_children(id INTEGER PRIMARY KEY, parent_code TEXT REFERENCES {p}bad_parents(code));
            CREATE TABLE {p}orphan_parents(id INTEGER PRIMARY KEY);
            CREATE TABLE {p}orphan_children(id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES {p}orphan_parents(id));
            INSERT INTO {p}customers VALUES (1,NULL),(2,1),(3,1);
            INSERT INTO {p}orders VALUES (10,1),(11,1),(12,2);
            INSERT INTO {p}products VALUES (100),(200);
            INSERT INTO {p}order_items VALUES (1000,10,100),(1001,10,200),(1002,11,100),(1003,12,200);
            INSERT INTO {p}regions VALUES (7),(8);
            INSERT INTO {p}stores VALUES (70,7),(80,8);
            INSERT INTO {p}tenant_customers VALUES (1,10),(1,11);
            INSERT INTO {p}tenant_orders VALUES (500,1,10),(501,1,11);
            INSERT INTO {p}bad_parents VALUES (1,'x'),(2,'x');
            INSERT INTO {p}bad_children VALUES (1,'x');
            INSERT INTO {p}orphan_parents VALUES (1);
            INSERT INTO {p}orphan_children VALUES (1,999);
            """
        )
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    _check_sqlite(path)


def _create_adversarial_database(path: Path, trap: str) -> None:
    scripts = {
        "same_name_id": """
            CREATE TABLE parents(id INTEGER PRIMARY KEY, label TEXT);
            CREATE TABLE children(id INTEGER PRIMARY KEY, parent_id INTEGER);
            INSERT INTO parents VALUES (1,'p1'),(2,'p2');
            INSERT INTO children VALUES (1,2),(2,1);
        """,
        "integer_domain_collision": """
            CREATE TABLE parents(id INTEGER PRIMARY KEY, account_number INTEGER);
            CREATE TABLE children(id INTEGER PRIMARY KEY, salesperson_id INTEGER, parent_id INTEGER);
            INSERT INTO parents VALUES (10,100),(20,200);
            INSERT INTO children VALUES (100,10,20),(200,20,10);
        """,
        "small_domain_overlap": """
            CREATE TABLE parents(id INTEGER PRIMARY KEY, status_code INTEGER);
            CREATE TABLE children(id INTEGER PRIMARY KEY, priority_code INTEGER);
            INSERT INTO parents VALUES (1,1),(2,2),(3,3);
            INSERT INTO children VALUES (10,1),(20,2),(30,3);
        """,
        "multiple_parents": """
            CREATE TABLE parents(id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE alternate_parents(id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE children(id INTEGER PRIMARY KEY, owner_id INTEGER);
            INSERT INTO parents VALUES (1,'customer'),(2,'customer-2');
            INSERT INTO alternate_parents VALUES (1,'employee'),(2,'employee-2');
            INSERT INTO children VALUES (10,1),(20,2);
        """,
        "non_unique_parent": """
            CREATE TABLE parents(id INTEGER PRIMARY KEY, code TEXT);
            CREATE TABLE children(id INTEGER PRIMARY KEY, parent_code TEXT);
            INSERT INTO parents VALUES (1,'x'),(2,'x'),(3,'y');
            INSERT INTO children VALUES (10,'x'),(20,'y');
        """,
        "orphan": """
            CREATE TABLE parents(id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE children(id INTEGER PRIMARY KEY, parent_id INTEGER);
            INSERT INTO parents VALUES (1,'one'),(2,'two');
            INSERT INTO children VALUES (10,1),(20,999);
        """,
        "drift": """
            CREATE TABLE parents(id INTEGER PRIMARY KEY, legacy_id INTEGER UNIQUE);
            CREATE TABLE children(id INTEGER PRIMARY KEY, old_parent_id INTEGER, current_parent_id INTEGER);
            INSERT INTO parents VALUES (100,1),(200,2);
            INSERT INTO children VALUES (10,1,200),(20,2,100);
        """,
    }
    script = scripts.get(trap)
    if script is None:
        raise ValueError(f"unknown adversarial trap: {trap}")
    connection = sqlite3.connect(path)
    try:
        connection.executescript(script)
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    _check_sqlite(path)


def _create_million_row_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE million_parents(id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE million_children(id INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL REFERENCES million_parents(id))"
        )
        connection.executemany(
            "INSERT INTO million_parents(id) VALUES (?)",
            ((value,) for value in range(1, 1_001)),
        )
        for start in range(1, 1_000_001, 10_000):
            connection.executemany(
                "INSERT INTO million_children(id,parent_id) VALUES (?,?)",
                ((value, (value - 1) % 1_000 + 1) for value in range(start, start + 10_000)),
            )
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    _check_sqlite(path)


def _real_directory(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"{label} must be one real directory")
    return path.resolve(strict=True)


def _write_canonical(path: Path, value: Any) -> None:
    with path.open("xb") as output:
        output.write(rfc8785.dumps(value))
        output.write(b"\n")
        output.flush()
        os.fsync(output.fileno())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Stage 1 deterministic suite")
    parser.add_argument("--sealed-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    report = build_deterministic_suite(arguments.sealed_root, arguments.output)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
