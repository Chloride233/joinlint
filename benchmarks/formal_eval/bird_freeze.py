from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

import rfc8785

from benchmarks.formal_eval.bird_dataset import _file_record
from benchmarks.formal_eval.contracts import FormalManifestV2, FormalTask, SealedAgentTask
from benchmarks.formal_eval.manifest import semantic_fingerprint, verify_sealed_manifest


DATASET_RELEASE = "bird-dev-20240627-train-2023-07-11-declared-fk-v1"
SMALL_DATABASE_LIMIT = 64 * 1024 * 1024
LARGE_DATABASE_LIMIT = 512 * 1024 * 1024
DOMAIN_BY_DATABASE = {
    "california_schools": "education",
    "card_games": "gaming",
    "codebase_community": "software",
    "debit_card_specializing": "finance",
    "european_football_1": "sports",
    "european_football_2": "sports",
    "financial": "finance",
    "formula_1": "sports",
    "student_club": "education",
    "superhero": "entertainment",
    "thrombosis_prediction": "healthcare",
    "toxicology": "healthcare",
}


def build_natural_v2(review_root: Path, sealed_root: Path, output: Path) -> dict[str, Any]:
    if output.exists() or output.is_symlink():
        raise ValueError("natural v2 output already exists")
    resolved_sealed = _real_directory(sealed_root, "sealed root")
    resolved_review = _real_directory(review_root, "BIRD review root")
    resolved_output_parent = _real_directory(output.parent, "natural v2 output parent")
    try:
        resolved_review.relative_to(resolved_sealed)
        resolved_output_parent.relative_to(resolved_sealed)
    except ValueError as error:
        raise ValueError("BIRD inputs and output must stay inside the sealed root") from error
    output = resolved_output_parent / output.name

    packet_path = resolved_review / "review-packet.json"
    source_manifest_path = resolved_review / "source-manifest.json"
    packet = _load_json(packet_path)
    source_manifest = _load_json(source_manifest_path)
    _require_record(packet_path, source_manifest.get("review_packet"))
    if packet.get("status") != "pending_freeze_validation":
        raise ValueError("BIRD packet is not ready for freeze validation")
    selection = packet.get("selection")
    if not isinstance(selection, dict) or selection.get("relationship_scope") != "declared_fk_only":
        raise ValueError("BIRD packet is not declared-FK-only")

    database_records = {
        record["database_id"]: record
        for record in source_manifest.get("databases", [])
        if isinstance(record, dict) and isinstance(record.get("database_id"), str)
    }
    if len(database_records) != 12 or set(database_records) != set(DOMAIN_BY_DATABASE):
        raise ValueError("BIRD database allocation does not match the frozen domain map")

    sealed_tasks: list[SealedAgentTask] = []
    manifest_tasks: list[FormalTask] = []
    uniqueness: dict[str, tuple[frozenset[str], ...]] = {}
    for raw in packet.get("tasks", []):
        if not isinstance(raw, dict):
            raise ValueError("BIRD packet contains an invalid task")
        if raw.get("all_edges_declared_fk") is not True or raw.get("curation_required_edges"):
            raise ValueError("confirmatory task contains an undeclared relationship")
        database_id = raw.get("database_id")
        record = database_records.get(database_id)
        if record is None:
            raise ValueError("confirmatory task references an unknown database")
        database_path = _source_file(resolved_review, record["path"])
        _require_record(database_path, record)
        relative_database = database_path.relative_to(resolved_sealed).as_posix()
        allowed_graphs = tuple(
            tuple((str(edge[0]), str(edge[1])) for edge in graph)
            for graph in raw["proposed_allowed_graphs"]
        )
        expected_entities = tuple(str(value) for value in raw["expected_entities"])
        sealed_task = SealedAgentTask(
            task_id=raw["task_id"],
            database_id=database_id,
            question=raw["question"],
            schema_text=raw["schema_text"],
            schema=raw["schema"],
            sql_shape=raw["sql_shape"],
            gold_sql=raw["gold_sql"],
            database_path=relative_database,
            expected_entities=expected_entities,
            allowed_graphs=allowed_graphs,
            oracle_has_safe_path=True,
        )
        if database_id not in uniqueness:
            uniqueness[database_id] = _unique_column_sets(database_path)
        manifest_task = FormalTask(
            task_id=sealed_task.task_id,
            database_id=database_id,
            database_variant_group=f"bird-{database_id}",
            corpus="natural",
            split="confirmatory",
            domain=DOMAIN_BY_DATABASE[database_id],
            source_type="sqlite",
            database_scale=_database_scale(record["size"]),
            ambiguity="none",
            fanout_type=_fanout_type(allowed_graphs[0], uniqueness[database_id]),
            question_sha256=_text_digest(sealed_task.question),
            schema_sha256=_text_digest(sealed_task.schema_text),
            sql_shape_sha256=_text_digest(sealed_task.sql_shape),
            schema_family_id=semantic_fingerprint("schema", sealed_task.schema_text),
            question_template_id=semantic_fingerprint("question", sealed_task.question),
            sql_structure_id=semantic_fingerprint("sql", sealed_task.gold_sql),
            allowed_graphs=sealed_task.allowed_graphs,
            oracle_has_safe_path=True,
            join_depth=raw["join_depth"],
            ambiguous_ground_truth=False,
        )
        sealed_tasks.append(sealed_task)
        manifest_tasks.append(manifest_task)

    manifest = FormalManifestV2(
        dataset_release=DATASET_RELEASE,
        tasks=tuple(sorted(manifest_tasks, key=lambda task: task.task_id.encode("utf-8"))),
    )
    sealed_tasks.sort(key=lambda task: task.task_id.encode("utf-8"))
    verify_sealed_manifest(manifest, sealed_tasks)
    if len(sealed_tasks) != 60 or len({task.database_id for task in sealed_tasks}) != 12:
        raise ValueError("natural v2 allocation must contain 60 tasks across 12 databases")

    staging = output.with_name(f".{output.name}.staging-{uuid.uuid4().hex}")
    staging.mkdir(parents=True)
    try:
        tasks_path = staging / "agent-tasks.json"
        manifest_path = staging / "manifest.json"
        _write_canonical(
            tasks_path,
            [task.model_dump(mode="json", by_alias=True) for task in sealed_tasks],
        )
        _write_canonical(manifest_path, manifest.model_dump(mode="json"))
        report = {
            "schema_version": 1,
            "status": "candidate_not_formally_frozen",
            "dataset_release": DATASET_RELEASE,
            "confirmatory_task_count": len(sealed_tasks),
            "confirmatory_database_count": len({task.database_id for task in sealed_tasks}),
            "domain_count": len({task.domain for task in manifest.tasks}),
            "source_packet_sha256": _file_record(packet_path)["sha256"],
            "agent_tasks": _file_record(tasks_path),
            "manifest": _file_record(manifest_path),
            "formal_blockers": [
                "SEMANTIC_JOIN_FAILURE_DIAGNOSTICS_MISSING",
                "PREREGISTRATION_NOT_FROZEN",
                "DETERMINISTIC_SUITE_NOT_FROZEN",
                "INPUT_LOCK_NOT_GENERATED",
            ],
            "claim_boundary": "join_graph_only_not_query_correctness",
        }
        _write_canonical(staging / "conversion-report.json", report)
        os.replace(staging, output)
        return report
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _real_directory(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"{label} must be one real directory")
    return path.resolve(strict=True)


def _load_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"required BIRD input is unavailable: {path.name}")
    try:
        return json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"required BIRD input is invalid: {path.name}") from error


def _source_file(root: Path, relative_name: Any) -> Path:
    if not isinstance(relative_name, str):
        raise ValueError("BIRD source path must be a relative string")
    relative = PurePosixPath(relative_name)
    if (
        relative_name.startswith("/")
        or "\\" in relative_name
        or ".." in relative.parts
    ):
        raise ValueError("BIRD source path escapes its root")
    candidate = root / relative_name
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError(f"BIRD source file is unavailable: {relative_name}")
    try:
        candidate.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as error:
        raise ValueError("BIRD source path escapes its root") from error
    return candidate


def _require_record(path: Path, record: Any) -> None:
    if not isinstance(record, dict):
        raise ValueError(f"missing source record: {path.name}")
    expected = {"size": record.get("size"), "sha256": record.get("sha256")}
    if _file_record(path) != expected:
        raise ValueError(f"source hash mismatch: {path.name}")


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _database_scale(size: int) -> str:
    if size < SMALL_DATABASE_LIMIT:
        return "small"
    if size < LARGE_DATABASE_LIMIT:
        return "medium"
    return "large"


def _unique_column_sets(database: Path) -> tuple[frozenset[str], ...]:
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        values: list[frozenset[str]] = []
        for table in tables:
            table_info = connection.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
            primary = [
                (row[5], str(row[1]).casefold()) for row in table_info if int(row[5]) > 0
            ]
            if primary:
                values.append(frozenset(f"{table.casefold()}.{column}" for _, column in sorted(primary)))
            for index in connection.execute(f"PRAGMA index_list({_quote(table)})"):
                if not bool(index[2]) or bool(index[4]):
                    continue
                columns = connection.execute(f"PRAGMA index_info({_quote(index[1])})").fetchall()
                if columns and all(row[2] is not None for row in columns):
                    values.append(
                        frozenset(
                            f"{table.casefold()}.{str(row[2]).casefold()}" for row in columns
                        )
                    )
        return tuple(sorted(set(values), key=lambda value: tuple(sorted(value))))
    finally:
        connection.close()


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _fanout_type(
    graph: tuple[tuple[str, str], ...],
    unique_sets: tuple[frozenset[str], ...],
) -> str:
    relationships: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for left, right in graph:
        left_table, _, left_column = left.rpartition(".")
        right_table, _, right_column = right.rpartition(".")
        pair = tuple(sorted((left_table.casefold(), right_table.casefold())))
        relationships[pair][left_table.casefold()].add(left_column.casefold())
        relationships[pair][right_table.casefold()].add(right_column.casefold())
    expanding = 0
    for columns_by_table in relationships.values():
        unique = [
            frozenset(f"{table}.{column}" for column in columns) in unique_sets
            for table, columns in sorted(columns_by_table.items())
        ]
        if unique == [False, False]:
            return "many_to_many"
        if not all(unique):
            expanding += 1
    if expanding > 1:
        return "compound"
    return "one_to_many" if expanding else "none"


def _write_canonical(path: Path, value: Any) -> None:
    with path.open("xb") as output:
        output.write(rfc8785.dumps(value))
        output.write(b"\n")
        output.flush()
        os.fsync(output.fileno())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the candidate natural BIRD v2 inputs")
    parser.add_argument("--review-root", type=Path, required=True)
    parser.add_argument("--sealed-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    report = build_natural_v2(arguments.review_root, arguments.sealed_root, arguments.output)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
