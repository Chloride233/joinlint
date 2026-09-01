from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = REPOSITORY_ROOT / "benchmarks/formal_eval/results"
SEALED_ROOT = REPOSITORY_ROOT / "benchmarks/formal_eval/sealed"
DEFAULT_ENDPOINT = "http://127.0.0.1:6006"
CONDITIONS = ("control", "treatment")
FORBIDDEN_SCHEMA = (
    re.compile(r"DECLARED\s+RELATIONSHIPS", re.I),
    re.compile(r"\bFOREIGN\s+KEY\b", re.I),
    re.compile(r"\bREFERENCES\b", re.I),
)


@dataclass(frozen=True)
class ReviewProjection:
    run_id: str
    dataset_name: str
    projection_sha256: str
    source_hashes: dict[str, str]
    cases: tuple[dict[str, Any], ...]
    cells: dict[str, dict[str, dict[str, Any]]]
    experiment_names: dict[str, str]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path.name}")
    return value


def _read_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"expected an object at {path.name}:{line_number}")
        rows.append(value)
    return rows


def _visible_schema(schema_text: str) -> str:
    marker = re.search(r"(?:\r?\n)+DECLARED\s+RELATIONSHIPS\b", schema_text, re.I)
    visible = schema_text[: marker.start()].rstrip() if marker else schema_text.rstrip()
    if not visible or any(pattern.search(visible) for pattern in FORBIDDEN_SCHEMA):
        raise ValueError("visible schema is empty or leaks relationship metadata")
    return visible


def _safe_run_id(run_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id):
        raise ValueError("unsafe run ID")
    return run_id


def _local_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Phoenix review imports only support a loopback HTTP endpoint")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Phoenix endpoint must not contain credentials, query, or fragment")
    return endpoint.rstrip("/")


def _tasks_path(input_lock: Mapping[str, Any], override: Path | None) -> Path:
    candidate = override if override is not None else Path(str(input_lock["tasks_path"]))
    if candidate.is_symlink():
        raise ValueError("sealed task input cannot be a symlink")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("sealed task input must be a regular file")
    if override is None:
        resolved.relative_to(SEALED_ROOT.resolve())
    if _file_sha256(resolved) != input_lock["tasks_sha256"]:
        raise ValueError("sealed task input does not match input lock")
    return resolved


def _validate_raw_cell(
    run_dir: Path,
    row: Mapping[str, Any],
) -> dict[str, Any]:
    stem = f"{row['task_id']}--{row['condition']}"
    checkpoint = _read_object(run_dir / "checkpoints" / f"{stem}.json")
    if checkpoint != row:
        raise ValueError(f"checkpoint differs from results row: {stem}")

    process = _read_object(run_dir / "raw" / f"{stem}.process.json")
    for key in ("task_id", "condition", "input_lock_sha256", "prompt_sha256", "returncode"):
        if process.get(key) != row.get(key):
            raise ValueError(f"raw process {key} differs from results row: {stem}")
    if process.get("command_sha256") != row.get("command_sha256"):
        raise ValueError(f"raw process command differs from results row: {stem}")
    if process.get("state") != "completed":
        raise ValueError(f"raw process is not complete: {stem}")

    for suffix in ("stdout.jsonl", "stderr.txt"):
        path = run_dir / "raw" / f"{stem}.{suffix}"
        expected = process[f"{suffix.split('.')[0]}_sha256"]
        if _file_sha256(path) != expected:
            raise ValueError(f"raw {suffix} hash mismatch: {stem}")
    return process


def _review_join_score(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {
        key: value[key]
        for key in ("precision", "recall", "f1", "wrong_join", "error_code")
        if key in value
    }


def load_projection(run_dir: Path, *, tasks_path: Path | None = None) -> ReviewProjection:
    run_dir = run_dir.resolve(strict=True)
    run_id = _safe_run_id(run_dir.name)
    paths = {
        name: run_dir / name
        for name in ("input-lock.json", "preregistration.json", "results.jsonl", "effect.json")
    }
    source_hashes = {name: _file_sha256(path) for name, path in paths.items()}
    input_lock = _read_object(paths["input-lock.json"])
    preregistration = _read_object(paths["preregistration.json"])
    effect = _read_object(paths["effect.json"])
    rows = _read_rows(paths["results.jsonl"])

    input_lock_sha256 = source_hashes["input-lock.json"]
    if preregistration.get("input_lock_sha256") != input_lock_sha256:
        raise ValueError("preregistration is not bound to this input lock")
    if effect.get("input_lock_sha256") != input_lock_sha256:
        raise ValueError("effect is not bound to this input lock")
    if len(rows) != preregistration.get("run_count"):
        raise ValueError("results row count differs from preregistration")

    expected_order = [
        (item["task_id"], item["condition"]) for item in preregistration["run_order"]
    ]
    actual_order = [(row.get("task_id"), row.get("condition")) for row in rows]
    if actual_order != expected_order:
        raise ValueError("results do not follow the frozen run order")

    selected = {item["task_id"]: item for item in input_lock["selected_tasks"]}
    cells: dict[str, dict[str, dict[str, Any]]] = {condition: {} for condition in CONDITIONS}
    for row in rows:
        task_id = str(row["task_id"])
        condition = str(row["condition"])
        if condition not in CONDITIONS or task_id not in selected:
            raise ValueError("results contain an unexpected task or condition")
        if row.get("input_lock_sha256") != input_lock_sha256:
            raise ValueError(f"result is not bound to the input lock: {task_id}--{condition}")
        if row.get("evaluation_id") != preregistration.get("evaluation_id"):
            raise ValueError(f"result evaluation ID mismatch: {task_id}--{condition}")
        if task_id in cells[condition]:
            raise ValueError(f"duplicate result cell: {task_id}--{condition}")

        process = _validate_raw_cell(run_dir, row)
        cells[condition][task_id] = {
            "task_id": task_id,
            "database_id": row["database_id"],
            "condition": condition,
            "answer": {
                "sql": row["submitted_sql"],
                "warning": row["submitted_warning"],
            },
            "outcomes": {
                "final_join_graph_correct": row["final_join_graph_correct"],
                "execution_equivalent": row["execution_equivalent"],
                "infrastructure_ok": row["infrastructure_ok"],
            },
            "mechanism": {
                "plan_called": row["plan_called"],
                "validate_called": row["validate_called"],
                "validated_final_sql": row["validated_final_sql"],
            },
            "join_score": _review_join_score(row.get("join_score")),
            "tool_calls": row["tool_calls"],
            "usage": row["usage"],
            "elapsed_seconds": process["elapsed_seconds"],
            "evidence": {
                "input_lock_sha256": input_lock_sha256,
                "result_row_sha256": _canonical_sha256(row),
                "prompt_sha256": row["prompt_sha256"],
                "command_sha256": row["command_sha256"],
                "timing_source": "frozen_process_manifest",
            },
        }

    task_ids = set(selected)
    if any(set(cells[condition]) != task_ids for condition in CONDITIONS):
        raise ValueError("every selected task must have one control and one treatment cell")

    sealed_path = _tasks_path(input_lock, tasks_path)
    sealed_tasks = json.loads(sealed_path.read_text(encoding="utf-8"))
    if not isinstance(sealed_tasks, list):
        raise ValueError("sealed task input must be a JSON array")
    tasks = {task["task_id"]: task for task in sealed_tasks if task.get("task_id") in task_ids}
    if set(tasks) != task_ids:
        raise ValueError("sealed task input is missing a selected task")

    cases: list[dict[str, Any]] = []
    for task_id in sorted(task_ids):
        task = tasks[task_id]
        locked = selected[task_id]
        visible_schema = _visible_schema(str(task["schema_text"]))
        if _canonical_sha256(task["question"]) != locked["question_sha256"]:
            raise ValueError(f"question differs from input lock: {task_id}")
        if _canonical_sha256(visible_schema) != locked["visible_schema_sha256"]:
            raise ValueError(f"visible schema differs from input lock: {task_id}")
        if task["expected_entities"] != locked["required_entities"]:
            raise ValueError(f"required entities differ from input lock: {task_id}")
        cases.append(
            {
                "id": task_id,
                "input": {
                    "task_id": task_id,
                    "question": task["question"],
                    "required_entities": task["expected_entities"],
                    "visible_schema": visible_schema,
                },
                "output": {},
                "metadata": {
                    "database_id": locked["database_id"],
                    "join_depth": locked["join_depth"],
                    "input_lock_sha256": input_lock_sha256,
                    "question_sha256": locked["question_sha256"],
                    "visible_schema_sha256": locked["visible_schema_sha256"],
                },
            }
        )

    projection_body = {
        "run_id": run_id,
        "source_hashes": source_hashes,
        "cases": cases,
        "cells": cells,
    }
    projection_sha256 = _canonical_sha256(projection_body)
    dataset_name = f"joinlint-{run_id}-{input_lock_sha256[:12]}"
    experiment_names = {
        condition: f"{run_id}-{condition}-{projection_sha256[:12]}"
        for condition in CONDITIONS
    }
    return ReviewProjection(
        run_id=run_id,
        dataset_name=dataset_name,
        projection_sha256=projection_sha256,
        source_hashes=source_hashes,
        cases=tuple(cases),
        cells=cells,
        experiment_names=experiment_names,
    )


def _field(value: object, name: str) -> Any:
    if isinstance(value, Mapping):
        return value[name]
    return getattr(value, name)


def _boolean_evaluation(output: Mapping[str, Any], field: str) -> dict[str, object]:
    passed = bool(output["outcomes"][field])
    return {"score": float(passed), "label": "pass" if passed else "fail"}


def final_join_graph_correct(output: Mapping[str, Any]) -> dict[str, object]:
    return _boolean_evaluation(output, "final_join_graph_correct")


def execution_equivalent(output: Mapping[str, Any]) -> dict[str, object]:
    return _boolean_evaluation(output, "execution_equivalent")


def infrastructure_ok(output: Mapping[str, Any]) -> dict[str, object]:
    return _boolean_evaluation(output, "infrastructure_ok")


def import_projection(
    projection: ReviewProjection,
    *,
    endpoint: str = DEFAULT_ENDPOINT,
    client: object | None = None,
) -> dict[str, Any]:
    endpoint = _local_endpoint(endpoint)
    if client is None:
        try:
            from phoenix.client import Client
        except ImportError as exc:
            raise RuntimeError(
                "Phoenix client is missing; run with arize-phoenix-client==3.3.0"
            ) from exc
        client = Client(base_url=endpoint)

    datasets = _field(client, "datasets")
    experiments = _field(client, "experiments")
    dataset = datasets.create_dataset(
        name=projection.dataset_name,
        examples=projection.cases,
        dataset_description=(
            "Read-only JoinLint review projection. Frozen source artifacts remain authoritative; "
            "gold SQL, relationship oracles, database paths, and query result rows are excluded."
        ),
    )
    dataset_id = _field(dataset, "id")
    dataset_version_id = _field(dataset, "version_id")
    if len(dataset) != len(projection.cases):
        raise RuntimeError("Phoenix dataset example count differs from frozen projection")

    imported: dict[str, dict[str, Any]] = {}
    existing_experiments = experiments.list(dataset_id=dataset_id)
    for condition in CONDITIONS:
        experiment_name = projection.experiment_names[condition]
        matching = [
            item for item in existing_experiments if _field(item, "name") == experiment_name
        ]
        if len(matching) > 1:
            raise RuntimeError(f"duplicate Phoenix experiment name: {experiment_name}")
        expected_metadata = {
            "source": "derived_read_only_projection",
            "run_id": projection.run_id,
            "condition": condition,
            "projection_sha256": projection.projection_sha256,
            "results_sha256": projection.source_hashes["results.jsonl"],
        }
        if matching:
            item = matching[0]
            metadata = _field(item, "metadata")
            if any(metadata.get(key) != value for key, value in expected_metadata.items()):
                raise RuntimeError(f"existing Phoenix experiment metadata drift: {experiment_name}")
            if _field(item, "dataset_version_id") != dataset_version_id:
                raise RuntimeError(
                    f"existing Phoenix experiment dataset version drift: {experiment_name}"
                )
            if (
                _field(item, "successful_run_count") != len(projection.cases)
                or _field(item, "failed_run_count") != 0
                or _field(item, "missing_run_count") != 0
            ):
                raise RuntimeError(f"existing Phoenix experiment is incomplete: {experiment_name}")
            imported[condition] = {
                "experiment_id": _field(item, "id"),
                "name": experiment_name,
                "status": "reused",
            }
            continue

        condition_cells = projection.cells[condition]

        def replay(input: Mapping[str, Any]) -> dict[str, Any]:
            return condition_cells[str(input["task_id"])]

        replay.__name__ = f"replay_{condition}_frozen_result"
        experiment = experiments.run_experiment(
            dataset=dataset,
            task=replay,
            evaluators=[final_join_graph_correct, execution_equivalent, infrastructure_ok],
            experiment_name=experiment_name,
            experiment_description=(
                f"Derived {condition} review projection for {projection.run_id}; no model calls."
            ),
            experiment_metadata=expected_metadata,
            print_summary=False,
            retries=0,
        )
        task_runs = _field(experiment, "task_runs")
        if len(task_runs) != len(projection.cases):
            raise RuntimeError(f"Phoenix imported an incomplete experiment: {experiment_name}")
        imported[condition] = {
            "experiment_id": _field(experiment, "experiment_id"),
            "name": experiment_name,
            "status": "created",
        }

    return {
        "endpoint": endpoint,
        "dataset_id": dataset_id,
        "dataset_version_id": dataset_version_id,
        "dataset_name": projection.dataset_name,
        "paired_case_count": len(projection.cases),
        "cell_count": sum(len(items) for items in projection.cells.values()),
        "projection_sha256": projection.projection_sha256,
        "experiments": imported,
        "experiments_url": experiments.get_dataset_experiments_url(dataset_id),
    }


def _summary(projection: ReviewProjection) -> dict[str, Any]:
    return {
        "run_id": projection.run_id,
        "dataset_name": projection.dataset_name,
        "paired_case_count": len(projection.cases),
        "cell_count": sum(len(items) for items in projection.cells.values()),
        "projection_sha256": projection.projection_sha256,
        "source_hashes": projection.source_hashes,
        "experiment_names": projection.experiment_names,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Project a frozen JoinLint run into local Phoenix")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("verify", "import"):
        child = subparsers.add_parser(command)
        child.add_argument("--run-id", required=True)
        child.add_argument("--tasks-path", type=Path)
        if command == "import":
            child.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    arguments = parser.parse_args(argv)
    run_dir = RESULTS_ROOT / _safe_run_id(arguments.run_id)
    projection = load_projection(run_dir, tasks_path=arguments.tasks_path)
    result = (
        _summary(projection)
        if arguments.command == "verify"
        else import_projection(projection, endpoint=arguments.endpoint)
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
