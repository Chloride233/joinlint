from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import rfc8785

from benchmarks.agent_join.execution import execute_readonly, execution_matches
from benchmarks.agent_join.sql_edges import extract_join_edges
from benchmarks.formal_eval.bird_dataset import _check_sqlite, _file_record
from benchmarks.formal_eval.contracts import FormalManifestV2, FormalTask, SealedAgentTask
from benchmarks.formal_eval.manifest import semantic_fingerprint, verify_sealed_manifest


DATASET_RELEASE = "semantic-join-failure-v1"


@dataclass(frozen=True)
class TaskSpec:
    name: str
    question: str
    gold_sql: str
    dangerous_sql: str
    allowed_graph: tuple[tuple[str, str], ...]
    expected_entities: tuple[str, ...]
    trap_type: str
    fanout_type: Literal["none", "one_to_many", "many_to_many", "compound"]


@dataclass(frozen=True)
class DatabaseSpec:
    database_id: str
    domain: str
    schema_sql: str
    tasks: tuple[TaskSpec, ...]


def build_semantic_failure_v1(sealed_root: Path, output: Path) -> dict[str, Any]:
    resolved_sealed = _real_directory(sealed_root, "sealed root")
    resolved_parent = _real_directory(output.parent, "diagnostic output parent")
    try:
        resolved_parent.relative_to(resolved_sealed)
    except ValueError as error:
        raise ValueError("diagnostic output must stay inside the sealed root") from error
    output = resolved_parent / output.name
    if output.exists() or output.is_symlink():
        raise ValueError("semantic failure output already exists")

    staging = output.with_name(f".{output.name}.staging-{uuid.uuid4().hex}")
    database_directory = staging / "databases"
    database_directory.mkdir(parents=True)
    sealed_tasks: list[SealedAgentTask] = []
    manifest_tasks: list[FormalTask] = []
    catalog: list[dict[str, Any]] = []
    try:
        for database_spec in _database_specs():
            database_path = database_directory / f"{database_spec.database_id}.sqlite"
            _create_database(database_path, database_spec.schema_sql)
            schema_map, schema_text = _visible_schema(database_path)
            relative_database = (
                output / "databases" / database_path.name
            ).relative_to(resolved_sealed).as_posix()
            for task_spec in database_spec.tasks:
                task_id = f"sjf-{database_spec.database_id}-{task_spec.name}"
                gold_edges = tuple(sorted(extract_join_edges(task_spec.gold_sql, schema_map)))
                dangerous_edges = tuple(
                    sorted(extract_join_edges(task_spec.dangerous_sql, schema_map))
                )
                if gold_edges != task_spec.allowed_graph or dangerous_edges == gold_edges:
                    raise ValueError(f"diagnostic graph contract mismatch: {task_id}")
                gold_execution = execute_readonly(
                    database_path,
                    task_spec.gold_sql,
                    deadline_seconds=5,
                    max_rows=10_000,
                )
                comparison = execution_matches(
                    database_path,
                    task_id,
                    task_spec.gold_sql,
                    task_spec.dangerous_sql,
                    deadline_seconds=5,
                    max_rows=10_000,
                )
                if not gold_execution.executed or not comparison.executed or comparison.equivalent:
                    raise ValueError(f"diagnostic SQL is not a proven semantic failure: {task_id}")
                sql_shape = " | ".join(f"{left}={right}" for left, right in gold_edges)
                sealed_task = SealedAgentTask(
                    task_id=task_id,
                    database_id=database_spec.database_id,
                    question=task_spec.question,
                    schema_text=schema_text,
                    schema=schema_map,
                    sql_shape=sql_shape,
                    gold_sql=task_spec.gold_sql,
                    database_path=relative_database,
                    expected_entities=task_spec.expected_entities,
                    allowed_graphs=(gold_edges,),
                    oracle_has_safe_path=True,
                )
                formal_task = FormalTask(
                    task_id=task_id,
                    database_id=database_spec.database_id,
                    database_variant_group=f"semantic-failure-{database_spec.database_id}",
                    corpus="semantic_join_failure",
                    split="diagnostic",
                    domain=database_spec.domain,
                    source_type="sqlite",
                    database_scale="small",
                    ambiguity="high",
                    fanout_type=task_spec.fanout_type,
                    question_sha256=_digest(sealed_task.question),
                    schema_sha256=_digest(sealed_task.schema_text),
                    sql_shape_sha256=_digest(sealed_task.sql_shape),
                    schema_family_id=semantic_fingerprint("schema", sealed_task.schema_text),
                    question_template_id=semantic_fingerprint(
                        "question", sealed_task.question
                    ),
                    sql_structure_id=semantic_fingerprint("sql", sealed_task.gold_sql),
                    allowed_graphs=sealed_task.allowed_graphs,
                    oracle_has_safe_path=True,
                    join_depth=len(gold_edges),
                    ambiguous_ground_truth=False,
                )
                sealed_tasks.append(sealed_task)
                manifest_tasks.append(formal_task)
                catalog.append(
                    {
                        "task_id": task_id,
                        "trap_type": task_spec.trap_type,
                        "schema_fk_visibility": "omitted_for_stress",
                        "dangerous_sql": task_spec.dangerous_sql,
                        "dangerous_graph": dangerous_edges,
                        "dangerous_result_differs_from_gold": True,
                    }
                )

        sealed_tasks.sort(key=lambda task: task.task_id.encode("utf-8"))
        manifest = FormalManifestV2(
            dataset_release=DATASET_RELEASE,
            tasks=tuple(sorted(manifest_tasks, key=lambda task: task.task_id.encode("utf-8"))),
        )
        verify_sealed_manifest(manifest, sealed_tasks)
        if len(sealed_tasks) != 20 or len({task.database_id for task in sealed_tasks}) != 4:
            raise ValueError("diagnostic allocation must contain 20 tasks across 4 databases")

        tasks_path = staging / "agent-tasks.json"
        manifest_path = staging / "manifest.json"
        catalog_path = staging / "diagnostic-catalog.json"
        _write_canonical(
            tasks_path,
            [task.model_dump(mode="json", by_alias=True) for task in sealed_tasks],
        )
        _write_canonical(manifest_path, manifest.model_dump(mode="json"))
        _write_canonical(catalog_path, sorted(catalog, key=lambda row: row["task_id"]))
        database_records = [
            {
                "database_id": path.stem,
                "path": path.relative_to(staging).as_posix(),
                **_file_record(path),
            }
            for path in sorted(database_directory.glob("*.sqlite"))
        ]
        report = {
            "schema_version": 1,
            "status": "candidate_diagnostic_not_formally_frozen",
            "dataset_release": DATASET_RELEASE,
            "task_count": len(sealed_tasks),
            "database_count": len(database_records),
            "trap_counts": dict(sorted(Counter(row["trap_type"] for row in catalog).items())),
            "agent_tasks": _file_record(tasks_path),
            "manifest": _file_record(manifest_path),
            "diagnostic_catalog": _file_record(catalog_path),
            "databases": database_records,
            "natural_error_rate_eligible": False,
            "claim_boundary": "safety_stress_only_not_natural_error_rate",
        }
        _write_canonical(staging / "source-manifest.json", report)
        os.replace(staging, output)
        return report
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _create_database(path: Path, schema_sql: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA page_size = 4096")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(schema_sql)
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise ValueError(f"diagnostic database violates declared FKs: {path.stem}")
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    _check_sqlite(path)
    if path.with_name(path.name + "-wal").exists() or path.with_name(path.name + "-shm").exists():
        raise ValueError("diagnostic database creation left WAL state")


def _visible_schema(path: Path) -> tuple[dict[str, dict[str, str]], str]:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
        ]
        schema: dict[str, dict[str, str]] = {}
        blocks: list[str] = []
        for table in tables:
            columns: dict[str, str] = {}
            lines: list[str] = []
            for row in connection.execute(f"PRAGMA table_info({_quote(table)})"):
                name = str(row[1])
                column_type = str(row[2]).lower() or "text"
                columns[name] = column_type
                suffix = " PRIMARY KEY" if row[5] else ""
                lines.append(f"  {name} {column_type.upper()}{suffix}")
            schema[table] = columns
            blocks.append(f"TABLE {table} (\n" + ",\n".join(lines) + "\n)")
        return schema, "\n\n".join(blocks)
    finally:
        connection.close()


def _database_specs() -> tuple[DatabaseSpec, ...]:
    return (
        DatabaseSpec("commerce", "commerce", _COMMERCE_SQL, _COMMERCE_TASKS),
        DatabaseSpec("workforce", "workforce", _WORKFORCE_SQL, _WORKFORCE_TASKS),
        DatabaseSpec("subscriptions", "subscriptions", _SUBSCRIPTIONS_SQL, _SUBSCRIPTION_TASKS),
        DatabaseSpec("healthcare", "healthcare", _HEALTHCARE_SQL, _HEALTHCARE_TASKS),
    )


def _real_directory(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"{label} must be one real directory")
    return path.resolve(strict=True)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _write_canonical(path: Path, value: Any) -> None:
    with path.open("xb") as output:
        output.write(rfc8785.dumps(value))
        output.write(b"\n")
        output.flush()
        os.fsync(output.fileno())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build semantic join failure diagnostics")
    parser.add_argument("--sealed-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    report = build_semantic_failure_v1(arguments.sealed_root, arguments.output)
    print(json.dumps(report, sort_keys=True))
    return 0


_COMMERCE_SQL = """
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE addresses (id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id), city TEXT NOT NULL);
CREATE TABLE order_statuses (id INTEGER PRIMARY KEY, label TEXT NOT NULL);
CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id), shipping_address_id INTEGER NOT NULL REFERENCES addresses(id), status_id INTEGER NOT NULL REFERENCES order_statuses(id), total REAL NOT NULL);
CREATE TABLE order_items (id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id), product_id INTEGER NOT NULL REFERENCES products(id), quantity INTEGER NOT NULL);
INSERT INTO customers VALUES (10,'Alice'),(20,'Bob'),(30,'Cara');
INSERT INTO addresses VALUES (10,10,'New York'),(20,20,'San Francisco'),(30,30,'Los Angeles');
INSERT INTO order_statuses VALUES (10,'pending'),(20,'paid'),(30,'shipped');
INSERT INTO products VALUES (10,'Keyboard'),(20,'Monitor'),(30,'Mouse');
INSERT INTO orders VALUES (10,20,10,30,80.0),(20,10,20,10,50.0),(40,20,20,20,20.0);
INSERT INTO order_items VALUES (10,20,30,1),(20,10,10,2),(30,10,20,1),(40,40,30,3);
"""

_COMMERCE_TASKS = (
    TaskSpec("customer_orders", "List each order total with the customer who placed it.", "SELECT o.id,c.name,o.total FROM orders o JOIN customers c ON o.customer_id=c.id ORDER BY o.id", "SELECT o.id,c.name,o.total FROM orders o JOIN customers c ON o.id=c.id ORDER BY o.id", (("customers.id", "orders.customer_id"),), ("customers", "orders"), "same_name_id", "one_to_many"),
    TaskSpec("ordered_products", "Show the product names recorded on every order item.", "SELECT o.id,p.name FROM orders o JOIN order_items i ON o.id=i.order_id JOIN products p ON i.product_id=p.id ORDER BY o.id,i.id", "SELECT o.id,p.name FROM orders o JOIN order_items i ON o.id=i.id JOIN products p ON i.id=p.id ORDER BY o.id,i.id", (("order_items.order_id", "orders.id"),("order_items.product_id", "products.id")), ("order_items", "orders", "products"), "integer_domain_collision", "one_to_many"),
    TaskSpec("shipping_city", "Report the shipping city selected for each order.", "SELECT o.id,a.city FROM orders o JOIN addresses a ON o.shipping_address_id=a.id ORDER BY o.id", "SELECT o.id,a.city FROM orders o JOIN addresses a ON o.customer_id=a.id ORDER BY o.id", (("addresses.id", "orders.shipping_address_id"),), ("addresses", "orders"), "multiple_parents", "one_to_many"),
    TaskSpec("order_status", "Display the current status label for every order.", "SELECT o.id,s.label FROM orders o JOIN order_statuses s ON o.status_id=s.id ORDER BY o.id", "SELECT o.id,s.label FROM orders o JOIN order_statuses s ON o.id=s.id ORDER BY o.id", (("order_statuses.id", "orders.status_id"),), ("order_statuses", "orders"), "same_name_id", "one_to_many"),
    TaskSpec("customer_products", "List each purchased product together with the ordering customer.", "SELECT c.name,p.name FROM order_items i JOIN orders o ON i.order_id=o.id JOIN customers c ON o.customer_id=c.id JOIN products p ON i.product_id=p.id ORDER BY i.id", "SELECT c.name,p.name FROM order_items i JOIN orders o ON i.order_id=o.id JOIN customers c ON o.id=c.id JOIN products p ON i.product_id=p.id ORDER BY i.id", (("customers.id", "orders.customer_id"),("order_items.order_id", "orders.id"),("order_items.product_id", "products.id")), ("customers", "order_items", "orders", "products"), "wrong_hierarchy", "compound"),
)

_WORKFORCE_SQL = """
CREATE TABLE offices (id INTEGER PRIMARY KEY, city TEXT NOT NULL);
CREATE TABLE departments (id INTEGER PRIMARY KEY, office_id INTEGER NOT NULL REFERENCES offices(id), name TEXT NOT NULL);
CREATE TABLE employees (id INTEGER PRIMARY KEY, department_id INTEGER NOT NULL REFERENCES departments(id), name TEXT NOT NULL);
CREATE TABLE projects (id INTEGER PRIMARY KEY, sponsor_department_id INTEGER NOT NULL REFERENCES departments(id), name TEXT NOT NULL);
CREATE TABLE roles (id INTEGER PRIMARY KEY, title TEXT NOT NULL);
CREATE TABLE assignments (id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id), project_id INTEGER NOT NULL REFERENCES projects(id), role_id INTEGER NOT NULL REFERENCES roles(id));
INSERT INTO offices VALUES (10,'Paris'),(20,'Berlin'),(30,'Tokyo');
INSERT INTO departments VALUES (10,20,'Engineering'),(20,10,'Sales'),(30,30,'Research');
INSERT INTO employees VALUES (10,20,'Eva'),(20,10,'Noah'),(40,10,'Mina');
INSERT INTO projects VALUES (10,30,'Atlas'),(20,10,'Beacon'),(30,20,'Comet');
INSERT INTO roles VALUES (10,'Lead'),(20,'Analyst'),(30,'Reviewer');
INSERT INTO assignments VALUES (10,20,30,10),(20,10,20,30),(30,40,10,20);
"""

_WORKFORCE_TASKS = (
    TaskSpec("employee_department", "Give each employee's department name.", "SELECT e.name,d.name FROM employees e JOIN departments d ON e.department_id=d.id ORDER BY e.id", "SELECT e.name,d.name FROM employees e JOIN departments d ON e.id=d.id ORDER BY e.id", (("departments.id", "employees.department_id"),), ("departments", "employees"), "same_name_id", "one_to_many"),
    TaskSpec("employee_office", "Show the office city reached through each employee's department.", "SELECT e.name,o.city FROM employees e JOIN departments d ON e.department_id=d.id JOIN offices o ON d.office_id=o.id ORDER BY e.id", "SELECT e.name,o.city FROM employees e JOIN offices o ON e.department_id=o.id ORDER BY e.id", (("departments.id", "employees.department_id"),("departments.office_id", "offices.id")), ("departments", "employees", "offices"), "wrong_hierarchy", "compound"),
    TaskSpec("assignment_employee", "List the employee responsible for every assignment.", "SELECT a.id,e.name FROM assignments a JOIN employees e ON a.employee_id=e.id ORDER BY a.id", "SELECT a.id,e.name FROM assignments a JOIN employees e ON a.id=e.id ORDER BY a.id", (("assignments.employee_id", "employees.id"),), ("assignments", "employees"), "integer_domain_collision", "one_to_many"),
    TaskSpec("assignment_project", "Attach the correct project name to each assignment.", "SELECT a.id,p.name FROM assignments a JOIN projects p ON a.project_id=p.id ORDER BY a.id", "SELECT a.id,p.name FROM assignments a JOIN projects p ON a.id=p.id ORDER BY a.id", (("assignments.project_id", "projects.id"),), ("assignments", "projects"), "same_name_id", "one_to_many"),
    TaskSpec("assignment_role", "Return the role title assigned to each project assignment.", "SELECT a.id,r.title FROM assignments a JOIN roles r ON a.role_id=r.id ORDER BY a.id", "SELECT a.id,r.title FROM assignments a JOIN roles r ON a.project_id=r.id ORDER BY a.id", (("assignments.role_id", "roles.id"),), ("assignments", "roles"), "multiple_parents", "one_to_many"),
)

_SUBSCRIPTIONS_SQL = """
CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL);
CREATE TABLE accounts (id INTEGER PRIMARY KEY, owner_user_id INTEGER NOT NULL REFERENCES users(id), name TEXT NOT NULL);
CREATE TABLE plans (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE subscriptions (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id), plan_id INTEGER NOT NULL REFERENCES plans(id));
CREATE TABLE invoices (id INTEGER PRIMARY KEY, subscription_id INTEGER NOT NULL REFERENCES subscriptions(id), total REAL NOT NULL);
CREATE TABLE processors (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE payments (id INTEGER PRIMARY KEY, invoice_id INTEGER NOT NULL REFERENCES invoices(id), processor_id INTEGER NOT NULL REFERENCES processors(id), amount REAL NOT NULL);
CREATE TABLE support_cases (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id), assignee_user_id INTEGER NOT NULL REFERENCES users(id), subject TEXT NOT NULL);
INSERT INTO users VALUES (10,'owner-a@example.com'),(20,'owner-b@example.com'),(30,'agent@example.com');
INSERT INTO accounts VALUES (10,20,'Acme'),(20,10,'Beta');
INSERT INTO plans VALUES (10,'Basic'),(20,'Pro'),(30,'Enterprise');
INSERT INTO subscriptions VALUES (10,20,30),(20,10,20),(30,20,10);
INSERT INTO invoices VALUES (10,20,100.0),(20,10,200.0),(30,30,50.0);
INSERT INTO processors VALUES (10,'Stripe'),(20,'Adyen'),(30,'PayPal');
INSERT INTO payments VALUES (10,20,30,200.0),(20,10,20,100.0),(30,30,10,50.0);
INSERT INTO support_cases VALUES (10,20,30,'Login'),(20,10,30,'Billing');
"""

_SUBSCRIPTION_TASKS = (
    TaskSpec("account_owner", "Show the owner email for each subscription account.", "SELECT a.name,u.email FROM accounts a JOIN users u ON a.owner_user_id=u.id ORDER BY a.id", "SELECT a.name,u.email FROM accounts a JOIN users u ON a.id=u.id ORDER BY a.id", (("accounts.owner_user_id", "users.id"),), ("accounts", "users"), "same_name_id", "one_to_many"),
    TaskSpec("account_plan", "List every account with each plan it subscribes to.", "SELECT a.name,p.name FROM subscriptions s JOIN accounts a ON s.account_id=a.id JOIN plans p ON s.plan_id=p.id ORDER BY s.id", "SELECT a.name,p.name FROM subscriptions s JOIN accounts a ON s.account_id=a.id JOIN plans p ON s.id=p.id ORDER BY s.id", (("accounts.id", "subscriptions.account_id"),("plans.id", "subscriptions.plan_id")), ("accounts", "plans", "subscriptions"), "integer_domain_collision", "one_to_many"),
    TaskSpec("account_invoice", "Report invoice totals under the account that owns the subscription.", "SELECT a.name,i.total FROM invoices i JOIN subscriptions s ON i.subscription_id=s.id JOIN accounts a ON s.account_id=a.id ORDER BY i.id", "SELECT a.name,i.total FROM invoices i JOIN accounts a ON i.id=a.id ORDER BY i.id", (("accounts.id", "subscriptions.account_id"),("invoices.subscription_id", "subscriptions.id")), ("accounts", "invoices", "subscriptions"), "wrong_hierarchy", "compound"),
    TaskSpec("payment_processor", "Identify the processor used for every payment.", "SELECT p.id,x.name FROM payments p JOIN processors x ON p.processor_id=x.id ORDER BY p.id", "SELECT p.id,x.name FROM payments p JOIN processors x ON p.id=x.id ORDER BY p.id", (("payments.processor_id", "processors.id"),), ("payments", "processors"), "same_name_id", "one_to_many"),
    TaskSpec("case_assignee", "Return the assignee email for each support case.", "SELECT c.subject,u.email FROM support_cases c JOIN users u ON c.assignee_user_id=u.id ORDER BY c.id", "SELECT c.subject,u.email FROM support_cases c JOIN users u ON c.account_id=u.id ORDER BY c.id", (("support_cases.assignee_user_id", "users.id"),), ("support_cases", "users"), "multiple_parents", "one_to_many"),
)

_HEALTHCARE_SQL = """
CREATE TABLE patients (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE providers (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE facilities (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE visits (id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL REFERENCES patients(id), provider_id INTEGER NOT NULL REFERENCES providers(id), facility_id INTEGER NOT NULL REFERENCES facilities(id));
CREATE TABLE diagnosis_codes (id INTEGER PRIMARY KEY, code TEXT NOT NULL);
CREATE TABLE diagnoses (id INTEGER PRIMARY KEY, visit_id INTEGER NOT NULL REFERENCES visits(id), diagnosis_code_id INTEGER NOT NULL REFERENCES diagnosis_codes(id));
CREATE TABLE drugs (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE prescriptions (id INTEGER PRIMARY KEY, visit_id INTEGER NOT NULL REFERENCES visits(id), drug_id INTEGER NOT NULL REFERENCES drugs(id));
CREATE TABLE lab_tests (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE lab_results (id INTEGER PRIMARY KEY, visit_id INTEGER NOT NULL REFERENCES visits(id), lab_test_id INTEGER NOT NULL REFERENCES lab_tests(id));
INSERT INTO patients VALUES (10,'Iris'),(20,'Jon'),(30,'Kai');
INSERT INTO providers VALUES (10,'Dr Lee'),(20,'Dr Rao'),(30,'Dr Chen');
INSERT INTO facilities VALUES (10,'North'),(20,'Central'),(30,'South');
INSERT INTO visits VALUES (10,20,30,10),(20,10,20,30),(30,20,10,20);
INSERT INTO diagnosis_codes VALUES (10,'A10'),(20,'B20'),(30,'C30');
INSERT INTO diagnoses VALUES (10,20,30),(20,10,20),(30,30,10);
INSERT INTO drugs VALUES (10,'Drug A'),(20,'Drug B'),(30,'Drug C');
INSERT INTO prescriptions VALUES (10,20,30),(20,10,20),(30,30,10);
INSERT INTO lab_tests VALUES (10,'CBC'),(20,'MRI'),(30,'Lipid');
INSERT INTO lab_results VALUES (10,20,30),(20,10,20),(30,30,10);
"""

_HEALTHCARE_TASKS = (
    TaskSpec("visit_patient", "List the patient name attached to each visit.", "SELECT v.id,p.name FROM visits v JOIN patients p ON v.patient_id=p.id ORDER BY v.id", "SELECT v.id,p.name FROM visits v JOIN patients p ON v.id=p.id ORDER BY v.id", (("patients.id", "visits.patient_id"),), ("patients", "visits"), "same_name_id", "one_to_many"),
    TaskSpec("visit_provider", "Show the treating provider for every visit.", "SELECT v.id,p.name FROM visits v JOIN providers p ON v.provider_id=p.id ORDER BY v.id", "SELECT v.id,p.name FROM visits v JOIN providers p ON v.patient_id=p.id ORDER BY v.id", (("providers.id", "visits.provider_id"),), ("providers", "visits"), "multiple_parents", "one_to_many"),
    TaskSpec("visit_diagnosis", "Display all diagnosis codes recorded for their visits.", "SELECT v.id,c.code FROM diagnoses d JOIN visits v ON d.visit_id=v.id JOIN diagnosis_codes c ON d.diagnosis_code_id=c.id ORDER BY d.id", "SELECT v.id,c.code FROM diagnoses d JOIN visits v ON d.visit_id=v.id JOIN diagnosis_codes c ON d.id=c.id ORDER BY d.id", (("diagnoses.diagnosis_code_id", "diagnosis_codes.id"),("diagnoses.visit_id", "visits.id")), ("diagnoses", "diagnosis_codes", "visits"), "integer_domain_collision", "one_to_many"),
    TaskSpec("visit_drug", "List the prescribed drug names under each visit.", "SELECT v.id,d.name FROM prescriptions p JOIN visits v ON p.visit_id=v.id JOIN drugs d ON p.drug_id=d.id ORDER BY p.id", "SELECT v.id,d.name FROM prescriptions p JOIN visits v ON p.visit_id=v.id JOIN drugs d ON p.id=d.id ORDER BY p.id", (("drugs.id", "prescriptions.drug_id"),("prescriptions.visit_id", "visits.id")), ("drugs", "prescriptions", "visits"), "same_name_id", "one_to_many"),
    TaskSpec("visit_lab", "Return the lab test name associated with every visit result.", "SELECT v.id,t.name FROM lab_results r JOIN visits v ON r.visit_id=v.id JOIN lab_tests t ON r.lab_test_id=t.id ORDER BY r.id", "SELECT v.id,t.name FROM lab_results r JOIN visits v ON r.visit_id=v.id JOIN lab_tests t ON r.id=t.id ORDER BY r.id", (("lab_results.lab_test_id", "lab_tests.id"),("lab_results.visit_id", "visits.id")), ("lab_results", "lab_tests", "visits"), "integer_domain_collision", "one_to_many"),
)


if __name__ == "__main__":
    raise SystemExit(main())
