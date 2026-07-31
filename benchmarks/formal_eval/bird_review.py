from __future__ import annotations

import argparse
import json
import hashlib
import os
import shutil
import sqlite3
import uuid
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import rfc8785

from benchmarks.agent_join.sql_edges import canonical_edge, extract_join_edges
from benchmarks.agent_join.execution import execute_readonly
from benchmarks.formal_eval.bird_dataset import (
    _check_sqlite,
    _copy_zip_member,
    _file_record,
    _required_member,
    _validated_infos,
    verify_bird_subset,
)
from benchmarks.formal_eval.manifest import semantic_fingerprint


DEV_SOURCE_URL = "https://bird-bench.oss-cn-beijing.aliyuncs.com/dev.zip"
DEV_SOURCE_SIZE = 346_207_293
DEV_SOURCE_SHA256 = "cdd6d19faeb45a23970b98d3ef6c40a87987c95459c2cf12076897a60cf5a630"
DEV_DATABASES_SHA256 = "8132aacfe61ecec7198a62e93cb8512dac89b1d6b9922fe3c7bb94b13f317d8f"
TRAIN_CONFIRMATORY_DATABASE = "european_football_1"
SELECTION_SEED = "joinlint-bird-natural-v1"


def build_review_bundle(
    dev_archive: Path,
    train_subset: Path,
    output: Path,
    *,
    tasks_per_database: int = 5,
) -> dict[str, Any]:
    if tasks_per_database < 1:
        raise ValueError("task count must be positive")
    if output.exists() or output.is_symlink():
        raise ValueError("review output already exists")
    if not dev_archive.is_file() or dev_archive.is_symlink():
        raise ValueError("BIRD Dev archive must be one regular file")
    if _file_record(dev_archive) != {"size": DEV_SOURCE_SIZE, "sha256": DEV_SOURCE_SHA256}:
        raise ValueError("BIRD Dev archive does not match the frozen source")
    train_manifest = verify_bird_subset(train_subset)

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.with_name(f".{output.name}.staging-{uuid.uuid4().hex}")
    staging.mkdir()
    nested_path = staging / ".dev_databases.zip"
    try:
        with zipfile.ZipFile(dev_archive) as outer:
            infos = _validated_infos(outer)
            dev_tasks = _read_json(
                outer,
                _required_member(infos, "dev_20240627/dev.json"),
            )
            dev_tables = _read_json(
                outer,
                _required_member(infos, "dev_20240627/dev_tables.json"),
            )
            nested_info = _required_member(infos, "dev_20240627/dev_databases.zip")
            _copy_zip_member(outer, nested_info, nested_path, nested_info.file_size)
        if _file_record(nested_path)["sha256"] != DEV_DATABASES_SHA256:
            raise ValueError("BIRD Dev database archive hash changed")
        if not isinstance(dev_tasks, list) or not isinstance(dev_tables, list):
            raise ValueError("BIRD Dev metadata must contain arrays")

        table_rows = {row["db_id"]: row for row in dev_tables if isinstance(row, dict)}
        if len(table_rows) != len(dev_tables):
            raise ValueError("BIRD Dev table metadata contains duplicate or invalid rows")
        dev_ids = tuple(sorted(table_rows))
        database_dir = staging / "databases"
        database_dir.mkdir()
        database_records = _extract_dev_databases(nested_path, database_dir, dev_ids)
        nested_path.unlink()

        train_tables = json.loads((train_subset / "selected-tables.json").read_bytes())
        train_tasks = json.loads((train_subset / "candidate-tasks.json").read_bytes())
        train_rows = {
            row["db_id"]: row for row in train_tables if isinstance(row, dict)
        }
        train_source = train_subset / "databases" / f"{TRAIN_CONFIRMATORY_DATABASE}.sqlite"
        train_destination = database_dir / f"{TRAIN_CONFIRMATORY_DATABASE}.sqlite"
        shutil.copyfile(train_source, train_destination)
        _check_sqlite(train_destination)
        database_records.append(
            {
                "database_id": TRAIN_CONFIRMATORY_DATABASE,
                "source_split": "train",
                "path": f"databases/{TRAIN_CONFIRMATORY_DATABASE}.sqlite",
                **_file_record(train_destination),
            }
        )

        candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for source_split, tasks, rows in (
            ("dev", dev_tasks, table_rows),
            ("train", train_tasks, train_rows),
        ):
            for source_index, task in enumerate(tasks):
                if not isinstance(task, dict):
                    continue
                original_index = task.get("task_index", source_index)
                if not isinstance(original_index, int) or isinstance(original_index, bool):
                    continue
                database_id = task.get("db_id")
                if source_split == "train" and database_id != TRAIN_CONFIRMATORY_DATABASE:
                    continue
                metadata = rows.get(database_id)
                if metadata is None:
                    continue
                candidate = _candidate(task, original_index, source_split, metadata)
                if candidate is not None:
                    candidates[database_id].append(candidate)

        database_paths = {
            record["database_id"]: staging / record["path"]
            for record in database_records
        }
        mixed_selection = select_executable_candidates(
            candidates,
            database_ids=(*dev_ids, TRAIN_CONFIRMATORY_DATABASE),
            tasks_per_database=tasks_per_database,
            database_paths=database_paths,
        )
        curated_diagnostics = [
            task for task in mixed_selection if not task["all_edges_declared_fk"]
        ]
        declared_candidates = {
            database_id: [
                task for task in values if task["all_edges_declared_fk"]
            ]
            for database_id, values in candidates.items()
        }
        selected = select_executable_candidates(
            declared_candidates,
            database_ids=(*dev_ids, TRAIN_CONFIRMATORY_DATABASE),
            tasks_per_database=tasks_per_database,
            database_paths=database_paths,
            require_grain_provable=True,
        )
        if not all(task["all_edges_declared_fk"] for task in selected):
            raise ValueError("confirmatory selection contains an undeclared edge")
        packet = {
            "schema_version": 1,
            "status": "pending_freeze_validation",
            "dataset_release": "BIRD dev-20240627 + train-2023-07-11",
            "selection": {
                "database_count": len(dev_ids) + 1,
                "task_count": len(selected),
                "tasks_per_database": tasks_per_database,
                "uses_joinlint_output": False,
                "train_confirmatory_database": TRAIN_CONFIRMATORY_DATABASE,
                "reserved_pilot_databases": ["citeseer", "genes", "trains"],
                "gold_execution_deadline_seconds": 5,
                "gold_execution_max_rows": 10_000,
                "selection_seed": SELECTION_SEED,
                "selection_strategy": "one-per-available-depth-then-seeded-fill",
                "relationship_scope": "declared_fk_only",
                "oracle_source": "bird_gold_sql_plus_declared_fk",
                "curated_edges_in_confirmatory": 0,
            },
            "review_contract": {
                "arm_labels_present": False,
                "joinlint_outputs_present": False,
                "physical_graph_preserves_source_case": True,
                "allowed_graph_uses_sqlite_casefold": True,
                "independent_ground_truth_review_required": False,
                "required_reviewers": 0,
                "adjudicator_required_on_disagreement": False,
                "sampled_human_qa_optional": True,
                "freeze_requires_completed_validation": True,
                "claim_boundary": "join_graph_only_not_query_correctness",
            },
            "tasks": selected,
        }
        packet_path = staging / "review-packet.json"
        _write_canonical(packet_path, packet)
        reviewer_template_path = staging / "reviewer-template.json"
        _write_canonical(reviewer_template_path, reviewer_template(selected))
        diagnostic_path = staging / "curated-diagnostic-tasks.json"
        _write_canonical(
            diagnostic_path,
            {
                "schema_version": 1,
                "status": "diagnostic_only_not_confirmatory",
                "uses_joinlint_output": False,
                "selection_strategy": "undeclared_edges_from_legacy_mixed_selection",
                "task_count": len(curated_diagnostics),
                "curation_required_edge_count": sum(
                    len(task["curation_required_edges"])
                    for task in curated_diagnostics
                ),
                "tasks": curated_diagnostics,
            },
        )
        sources = {
            "schema_version": 1,
            "dev_source": {
                "url": DEV_SOURCE_URL,
                **_file_record(dev_archive),
                "nested_databases_sha256": DEV_DATABASES_SHA256,
            },
            "train_source": train_manifest["source"],
            "databases": sorted(database_records, key=lambda row: row["database_id"]),
            "review_packet": _file_record(packet_path),
            "reviewer_template": _file_record(reviewer_template_path),
            "curated_diagnostics": _file_record(diagnostic_path),
        }
        _write_canonical(staging / "source-manifest.json", sources)
        os.replace(staging, output)
        return packet
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def select_review_candidates(
    candidates: dict[str, list[dict[str, Any]]],
    *,
    database_ids: Iterable[str],
    tasks_per_database: int,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    used_near_pairs: set[tuple[str, str]] = set()
    for database_id in sorted(database_ids):
        values = candidates.get(database_id, [])
        values = sorted(
            values,
            key=lambda task: (
                task["join_depth"],
                _selection_rank(task["task_id"]),
                task["source_index"],
            ),
        )
        chosen: list[dict[str, Any]] = []
        represented_depths: set[int] = set()
        for prefer_new_depth in (True, False):
            for task in values:
                if len(chosen) >= tasks_per_database:
                    break
                pair = (task["question_template_id"], task["sql_structure_id"])
                if pair in used_near_pairs or task in chosen:
                    continue
                if prefer_new_depth and task["join_depth"] in represented_depths:
                    continue
                chosen.append(task)
                represented_depths.add(task["join_depth"])
                used_near_pairs.add(pair)
        if len(chosen) < tasks_per_database:
            raise ValueError(f"insufficient unique review candidates: {database_id}")
        selected.extend(sorted(chosen, key=lambda task: task["task_id"]))
    return sorted(selected, key=lambda task: task["task_id"])


def reviewer_template(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "optional_sampled_qa",
        "instructions": {
            "work_independently": True,
            "do_not_use_joinlint_output": True,
            "derive_graph_from_gold_sql_and_schema": True,
            "record_ambiguity_instead_of_guessing": True,
        },
        "tasks": [
            {
                "task_id": task["task_id"],
                "database_id": task["database_id"],
                "source_split": task["source_split"],
                "source_index": task["source_index"],
                "question": task["question"],
                "gold_sql": task["gold_sql"],
                "schema_text": task["schema_text"],
                "schema": task["schema"],
                "annotation": {
                    "decision": None,
                    "physical_join_graph": [],
                    "equivalent_allowed_graphs": [],
                    "ambiguous_reason": None,
                },
            }
            for task in tasks
        ],
    }


def _selection_rank(task_id: str) -> str:
    return hashlib.sha256(f"{SELECTION_SEED}\0{task_id}".encode("utf-8")).hexdigest()


def select_executable_candidates(
    candidates: dict[str, list[dict[str, Any]]],
    *,
    database_ids: Iterable[str],
    tasks_per_database: int,
    database_paths: dict[str, Path],
    require_grain_provable: bool = False,
) -> list[dict[str, Any]]:
    remaining = {database_id: list(values) for database_id, values in candidates.items()}
    while True:
        selected = select_review_candidates(
            remaining,
            database_ids=database_ids,
            tasks_per_database=tasks_per_database,
        )
        failed: set[str] = set()
        for task in selected:
            if require_grain_provable and not grain_provable(
                database_paths[task["database_id"]],
                task["physical_join_graph"],
            ):
                failed.add(task["task_id"])
                continue
            result = execute_readonly(
                database_paths[task["database_id"]],
                task["gold_sql"],
                deadline_seconds=5,
                max_rows=10_000,
            )
            if not result.executed:
                failed.add(task["task_id"])
        if not failed:
            return selected
        remaining = {
            database_id: [task for task in values if task["task_id"] not in failed]
            for database_id, values in remaining.items()
        }


def _candidate(
    task: dict[str, Any],
    source_index: int,
    source_split: str,
    metadata: dict[str, Any],
) -> dict[str, Any] | None:
    question = task.get("question")
    evidence = task.get("evidence", "")
    sql = task.get("SQL")
    if not isinstance(question, str) or not isinstance(evidence, str) or not isinstance(sql, str):
        return None
    schema = _schema_map(metadata)
    try:
        extracted = extract_join_edges(sql, schema)
    except ValueError:
        return None
    if not 1 <= len(extracted) <= 4:
        return None
    canonical = _canonicalize_edges(extracted, metadata)
    if canonical is None or len(canonical) != len(extracted):
        return None
    declared = _foreign_key_edges(metadata)
    unsupported = sorted(canonical - declared)
    graph = sorted(canonical)
    scorer_graph = sorted(
        canonical_edge(left.casefold(), right.casefold()) for left, right in graph
    )
    entities = sorted(
        {endpoint.rsplit(".", 1)[0] for edge in graph for endpoint in edge}
    )
    schema_text = _schema_text(metadata)
    sql_shape = " | ".join(f"{left}={right}" for left, right in graph)
    task_id = f"bird-{source_split}-{metadata['db_id']}-{source_index:05d}"
    return {
        "task_id": task_id,
        "database_id": metadata["db_id"],
        "source_split": source_split,
        "source_index": source_index,
        "question": question,
        "evidence": evidence,
        "gold_sql": sql,
        "schema_text": schema_text,
        "schema": schema,
        "sql_shape": sql_shape,
        "expected_entities": entities,
        "physical_join_graph": graph,
        "proposed_allowed_graphs": [scorer_graph],
        "join_depth": len(graph),
        "declared_fk_edges": sorted(canonical & declared),
        "curation_required_edges": unsupported,
        "all_edges_declared_fk": not unsupported,
        "question_template_id": semantic_fingerprint("question", question),
        "sql_structure_id": semantic_fingerprint("sql", sql),
        "review": {
            "status": "pending",
            "reviewer_1": None,
            "reviewer_2": None,
            "adjudication": None,
        },
    }


def _schema_map(metadata: dict[str, Any]) -> dict[str, dict[str, str]]:
    tables = metadata["table_names_original"]
    schema = {table: {} for table in tables}
    for (table_index, column), column_type in zip(
        metadata["column_names_original"], metadata["column_types"], strict=True
    ):
        if table_index >= 0:
            schema[tables[table_index]][column] = column_type
    return schema


def _canonicalize_edges(
    edges: Iterable[tuple[str, str]], metadata: dict[str, Any]
) -> frozenset[tuple[str, str]] | None:
    lookup = {
        (table.casefold(), column.casefold()): f"{table}.{column}"
        for table, columns in _schema_map(metadata).items()
        for column in columns
    }
    normalized: set[tuple[str, str]] = set()
    for edge in edges:
        endpoints: list[str] = []
        for endpoint in edge:
            table, separator, column = endpoint.rpartition(".")
            if not separator:
                return None
            value = lookup.get((table.casefold(), column.casefold()))
            if value is None:
                return None
            endpoints.append(value)
        normalized.add(canonical_edge(*endpoints))
    return frozenset(normalized)


def _foreign_key_edges(metadata: dict[str, Any]) -> frozenset[tuple[str, str]]:
    return frozenset(
        canonical_edge(_column_endpoint(metadata, left), _column_endpoint(metadata, right))
        for left, right in metadata["foreign_keys"]
    )


def grain_provable(database_path: Path, graph: Iterable[tuple[str, str]]) -> bool:
    """Return whether a Stage-1 style grain-preserving Join Proof could exist.

    The check mirrors JoinLint's planner conservatively: there must be an
    entity with a stable unique key that can span every requested table while
    every traversal step away from that entity preserves the grain.
    It is computed directly from the frozen database and declared foreign keys,
    not from JoinLint output.
    """
    edges = [canonical_edge(*edge) for edge in graph]
    if not edges:
        return False
    tables = sorted({endpoint.rsplit(".", 1)[0] for edge in edges for endpoint in edge})
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        unique_keys = {
            table: _stable_unique_key(connection, table)
            for table in tables
        }
        relationships = [
            relationship
            for table in tables
            for relationship in _foreign_key_relationships(connection, table)
            if relationship["child_table"] in tables
            and relationship["parent_table"] in tables
        ]
        if not relationships:
            return False
        for expected in tables:
            if not unique_keys[expected]:
                continue
            if _grain_spanning_tree(
                expected,
                tables,
                relationships,
                child_matches=_group_max_count(
                    connection, relationships, table_field="child_table", column_field="child_column"
                ),
                parent_duplicates=_group_max_count(
                    connection, relationships, table_field="parent_table", column_field="parent_column"
                ),
                child_to_parent_unique=_unique_parent_map(connection, relationships),
            ):
                return True
        return False
    finally:
        connection.close()


def _stable_unique_key(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    quoted = f'"{table}"'
    info = connection.execute(f"PRAGMA table_info({quoted})").fetchall()
    nullables = {row[1] for row in info if not row[3]}
    pk_columns = tuple(
        column
        for column, _ in sorted(
            ((row[1], row[5]) for row in info if row[5]),
            key=lambda pair: pair[1],
        )
    )
    candidate: tuple[str, ...] = ()
    if pk_columns and not (set(pk_columns) & nullables):
        candidate = pk_columns
    for index in connection.execute(f"PRAGMA index_list({quoted})").fetchall():
        if not index[2]:
            continue
        columns = tuple(
            row[2]
            for row in connection.execute(
                f"PRAGMA index_info(\"{index[1]}\")"
            ).fetchall()
            if row[2] is not None
        )
        if not columns:
            continue
        if set(columns) & nullables:
            continue
        if not candidate or len(columns) < len(candidate):
            candidate = columns
    return candidate


def _foreign_key_relationships(
    connection: sqlite3.Connection,
    table: str,
) -> list[dict[str, str]]:
    relationships: list[dict[str, str]] = []
    for row in connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall():
        if row[2] is None or row[3] is None or row[4] is None:
            continue
        relationships.append(
            {
                "child_table": table,
                "child_column": row[3],
                "parent_table": row[2],
                "parent_column": row[4],
            }
        )
    return relationships


def _unique_parent_map(
    connection: sqlite3.Connection,
    relationships: list[dict[str, str]],
) -> dict[tuple[str, str, str, str], bool]:
    result: dict[tuple[str, str, str, str], bool] = {}
    for relationship in relationships:
        parent = relationship["parent_table"]
        key = _stable_unique_key(connection, parent)
        result[_relationship_key(relationship)] = key == (relationship["parent_column"],)
    return result


def _group_max_count(
    connection: sqlite3.Connection,
    relationships: list[dict[str, str]],
    *,
    table_field: str,
    column_field: str,
) -> dict[tuple[str, str, str, str], int]:
    result: dict[tuple[str, str, str, str], int] = {}
    for relationship in relationships:
        table = relationship[table_field]
        column = relationship[column_field]
        row = connection.execute(
            f'SELECT MAX(cnt) FROM (SELECT "{column}" AS key, COUNT(*) AS cnt '
            f'FROM "{table}" WHERE "{column}" IS NOT NULL GROUP BY key)'
        ).fetchone()
        result[_relationship_key(relationship)] = int(row[0] or 0)
    return result


def _relationship_key(
    relationship: dict[str, str],
) -> tuple[str, str, str, str]:
    return (
        relationship["child_table"],
        relationship["child_column"],
        relationship["parent_table"],
        relationship["parent_column"],
    )


def _grain_spanning_tree(
    expected: str,
    tables: list[str],
    relationships: list[dict[str, str]],
    *,
    child_matches: dict[tuple[str, str, str, str], int],
    parent_duplicates: dict[tuple[str, str, str, str], int],
    child_to_parent_unique: dict[tuple[str, str, str, str], bool],
) -> bool:
    adjacency: dict[str, list[tuple[str, dict[str, str], int]]] = {
        table: [] for table in tables
    }
    for relationship in relationships:
        key = _relationship_key(relationship)
        parent_to_child = child_matches[key]
        child_to_parent = (
            1
            if child_to_parent_unique.get(key, False)
            else parent_duplicates[key]
        )
        adjacency[relationship["child_table"]].append(
            (relationship["parent_table"], relationship, child_to_parent)
        )
        adjacency[relationship["parent_table"]].append(
            (relationship["child_table"], relationship, parent_to_child)
        )

    def visit(current: str, connected: set[str]) -> bool:
        if len(connected) == len(tables):
            return True
        for neighbor, _relationship, matches in adjacency[current]:
            if neighbor in connected:
                continue
            if matches > 1:
                continue
            if visit(neighbor, connected | {neighbor}):
                return True
        return False

    return visit(expected, {expected})


def _column_endpoint(metadata: dict[str, Any], column_index: int) -> str:
    table_index, column = metadata["column_names_original"][column_index]
    return f"{metadata['table_names_original'][table_index]}.{column}"


def _schema_text(metadata: dict[str, Any]) -> str:
    primary_indices: set[int] = set()
    for value in metadata["primary_keys"]:
        primary_indices.update(value if isinstance(value, list) else [value])
    columns_by_table: dict[int, list[str]] = defaultdict(list)
    for index, ((table_index, column), column_type) in enumerate(
        zip(metadata["column_names_original"], metadata["column_types"], strict=True)
    ):
        if table_index < 0:
            continue
        suffix = " PRIMARY KEY" if index in primary_indices else ""
        columns_by_table[table_index].append(f"  {column} {column_type.upper()}{suffix}")
    blocks = []
    for table_index, table in enumerate(metadata["table_names_original"]):
        blocks.append(f"TABLE {table} (\n" + ",\n".join(columns_by_table[table_index]) + "\n)")
    relationships = sorted(_foreign_key_edges(metadata))
    if relationships:
        blocks.append(
            "DECLARED RELATIONSHIPS\n"
            + "\n".join(f"  {left} = {right}" for left, right in relationships)
        )
    return "\n\n".join(blocks)


def _extract_dev_databases(
    nested_path: Path,
    destination: Path,
    database_ids: tuple[str, ...],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with zipfile.ZipFile(nested_path) as nested:
        infos = _validated_infos(nested)
        for database_id in database_ids:
            name = f"dev_databases/{database_id}/{database_id}.sqlite"
            info = _required_member(infos, name)
            target = destination / f"{database_id}.sqlite"
            _copy_zip_member(nested, info, target, 2 * 1024 * 1024 * 1024)
            _check_sqlite(target)
            records.append(
                {
                    "database_id": database_id,
                    "source_split": "dev",
                    "path": f"databases/{database_id}.sqlite",
                    **_file_record(target),
                }
            )
    return records


def _read_json(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> Any:
    if info.file_size > 16 * 1024 * 1024:
        raise ValueError("BIRD metadata member exceeds its size limit")
    try:
        return json.loads(archive.read(info))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("BIRD metadata member is invalid") from error


def _write_canonical(path: Path, value: Any) -> None:
    with path.open("xb") as output:
        output.write(rfc8785.dumps(value))
        output.write(b"\n")
        output.flush()
        os.fsync(output.fileno())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a declared-FK-only BIRD packet with optional QA"
    )
    parser.add_argument("--dev-archive", type=Path, required=True)
    parser.add_argument("--train-subset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    packet = build_review_bundle(arguments.dev_archive, arguments.train_subset, arguments.output)
    print(
        json.dumps(
            {
                "status": packet["status"],
                "database_count": packet["selection"]["database_count"],
                "task_count": packet["selection"]["task_count"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
