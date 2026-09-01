from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks.formal_eval import phoenix_review


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_run(tmp_path: Path) -> tuple[Path, Path]:
    run_dir = tmp_path / "review-run"
    tasks_path = tmp_path / "agent-tasks.json"
    tasks = []
    selected = []
    for index in range(2):
        task_id = f"task-{index}"
        question = f"Question {index}"
        visible_schema = "TABLE child (\n  id INTEGER,\n  parent_id INTEGER\n)\n\nTABLE parent (\n  id INTEGER\n)"
        tasks.append(
            {
                "task_id": task_id,
                "database_id": f"database-{index}",
                "database_path": f"SECRET_DATABASE_{index}",
                "question": question,
                "expected_entities": ["child", "parent"],
                "schema_text": (
                    visible_schema
                    + "\n\nDECLARED RELATIONSHIPS\n  child.parent_id = parent.id"
                ),
                "gold_sql": f"SECRET_GOLD_{index}",
                "allowed_graphs": [[[]]],
            }
        )
        selected.append(
            {
                "task_id": task_id,
                "database_id": f"database-{index}",
                "join_depth": 1,
                "required_entities": ["child", "parent"],
                "question_sha256": phoenix_review._canonical_sha256(question),
                "visible_schema_sha256": phoenix_review._canonical_sha256(visible_schema),
            }
        )
    tasks_path.write_text(json.dumps(tasks), encoding="utf-8")

    input_lock = {
        "tasks_path": str(tasks_path),
        "tasks_sha256": _sha256(tasks_path),
        "selected_tasks": selected,
    }
    run_dir.mkdir()
    (run_dir / "input-lock.json").write_text(json.dumps(input_lock), encoding="utf-8")
    input_lock_sha256 = _sha256(run_dir / "input-lock.json")
    run_order = [
        {"task_id": task_id, "condition": condition}
        for task_id in ("task-0", "task-1")
        for condition in phoenix_review.CONDITIONS
    ]
    preregistration = {
        "evaluation_id": "evaluation-1",
        "input_lock_sha256": input_lock_sha256,
        "run_count": len(run_order),
        "run_order": run_order,
    }
    effect = {"input_lock_sha256": input_lock_sha256}
    (run_dir / "preregistration.json").write_text(
        json.dumps(preregistration), encoding="utf-8"
    )
    (run_dir / "effect.json").write_text(json.dumps(effect), encoding="utf-8")
    (run_dir / "checkpoints").mkdir()
    (run_dir / "raw").mkdir()

    rows = []
    for order_index, item in enumerate(run_order):
        task_id = item["task_id"]
        condition = item["condition"]
        stem = f"{task_id}--{condition}"
        passed = condition == "control"
        row = {
            "schema_version": 1,
            "evaluation_id": "evaluation-1",
            "input_lock_sha256": input_lock_sha256,
            "task_id": task_id,
            "database_id": f"database-{task_id[-1]}",
            "condition": condition,
            "submitted_sql": "SELECT 1" if passed else "",
            "submitted_warning": "" if passed else "NO_VERIFIED_PATH",
            "final_join_graph_correct": passed,
            "execution_equivalent": passed,
            "infrastructure_ok": True,
            "plan_called": condition == "treatment",
            "validate_called": False,
            "validated_final_sql": False,
            "join_score": (
                {
                    "f1": 1.0,
                    "matched_graph": [["SECRET_ORACLE_LEFT", "SECRET_ORACLE_RIGHT"]],
                    "predicted_edges": [["child.parent_id", "parent.id"]],
                }
                if passed
                else None
            ),
            "tool_calls": ["EvaluationDatabase.submit_sql"],
            "usage": {"input_tokens": 100 + order_index, "output_tokens": 10},
            "prompt_sha256": hashlib.sha256(f"prompt-{stem}".encode()).hexdigest(),
            "command_sha256": hashlib.sha256(f"command-{stem}".encode()).hexdigest(),
            "returncode": 0,
        }
        rows.append(row)
        (run_dir / "checkpoints" / f"{stem}.json").write_text(
            json.dumps(row), encoding="utf-8"
        )
        stdout = run_dir / "raw" / f"{stem}.stdout.jsonl"
        stderr = run_dir / "raw" / f"{stem}.stderr.txt"
        stdout.write_text("{}\n", encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        process = {
            "task_id": task_id,
            "condition": condition,
            "input_lock_sha256": input_lock_sha256,
            "prompt_sha256": row["prompt_sha256"],
            "command_sha256": row["command_sha256"],
            "returncode": 0,
            "state": "completed",
            "elapsed_seconds": float(order_index + 1),
            "stdout_sha256": _sha256(stdout),
            "stderr_sha256": _sha256(stderr),
        }
        (run_dir / "raw" / f"{stem}.process.json").write_text(
            json.dumps(process), encoding="utf-8"
        )
    (run_dir / "results.jsonl").write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    return run_dir, tasks_path


def test_projection_is_paired_and_excludes_hidden_oracle_data(tmp_path: Path) -> None:
    run_dir, tasks_path = _write_run(tmp_path)

    projection = phoenix_review.load_projection(run_dir, tasks_path=tasks_path)

    assert len(projection.cases) == 2
    assert sum(len(rows) for rows in projection.cells.values()) == 4
    serialized = json.dumps(
        {"cases": projection.cases, "cells": projection.cells}, ensure_ascii=False
    )
    assert "SECRET_GOLD" not in serialized
    assert "SECRET_DATABASE" not in serialized
    assert "SECRET_ORACLE" not in serialized
    assert "DECLARED RELATIONSHIPS" not in serialized
    assert projection.cells["control"]["task-0"]["join_score"] == {"f1": 1.0}
    assert set(projection.cells) == {"control", "treatment"}


def test_projection_rejects_changed_raw_evidence(tmp_path: Path) -> None:
    run_dir, tasks_path = _write_run(tmp_path)
    raw = run_dir / "raw" / "task-0--control.stdout.jsonl"
    raw.write_text('{"changed":true}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="raw stdout.jsonl hash mismatch"):
        phoenix_review.load_projection(run_dir, tasks_path=tasks_path)


def test_import_rejects_non_loopback_endpoint(tmp_path: Path) -> None:
    run_dir, tasks_path = _write_run(tmp_path)
    projection = phoenix_review.load_projection(run_dir, tasks_path=tasks_path)

    with pytest.raises(ValueError, match="loopback"):
        phoenix_review.import_projection(projection, endpoint="https://phoenix.example.com")


class _FakeDataset:
    def __init__(self, examples: tuple[dict[str, object], ...]) -> None:
        self.id = "dataset-id"
        self.version_id = "version-id"
        self.examples = examples

    def __len__(self) -> int:
        return len(self.examples)


class _FakeDatasets:
    def create_dataset(self, **kwargs: object) -> _FakeDataset:
        return _FakeDataset(kwargs["examples"])


class _FakeExperiments:
    def __init__(self, existing: list[object] | None = None) -> None:
        self.created: list[dict[str, object]] = []
        self.existing = existing or []

    def list(self, *, dataset_id: str) -> list[object]:
        assert dataset_id == "dataset-id"
        return self.existing

    def run_experiment(self, **kwargs: object) -> SimpleNamespace:
        dataset = kwargs["dataset"]
        task = kwargs["task"]
        evaluators = kwargs["evaluators"]
        outputs = [task(example["input"]) for example in dataset.examples]
        for output in outputs:
            assert all(evaluator(output)["label"] in {"pass", "fail"} for evaluator in evaluators)
        self.created.append(kwargs)
        return SimpleNamespace(
            experiment_id=f"experiment-{len(self.created)}",
            task_runs=[object() for _ in outputs],
        )

    def get_dataset_experiments_url(self, dataset_id: str) -> str:
        return f"http://127.0.0.1:6006/datasets/{dataset_id}/experiments"


def test_import_creates_two_replay_experiments_without_model_calls(tmp_path: Path) -> None:
    run_dir, tasks_path = _write_run(tmp_path)
    projection = phoenix_review.load_projection(run_dir, tasks_path=tasks_path)
    experiments = _FakeExperiments()
    client = SimpleNamespace(datasets=_FakeDatasets(), experiments=experiments)

    result = phoenix_review.import_projection(projection, client=client)

    assert result["paired_case_count"] == 2
    assert result["cell_count"] == 4
    assert len(experiments.created) == 2
    assert {item["experiment_metadata"]["condition"] for item in experiments.created} == {
        "control",
        "treatment",
    }


def _existing_experiment(
    projection: phoenix_review.ReviewProjection,
    condition: str,
    *,
    dataset_version_id: str = "version-id",
) -> dict[str, object]:
    return {
        "id": f"experiment-{condition}",
        "name": projection.experiment_names[condition],
        "dataset_version_id": dataset_version_id,
        "metadata": {
            "source": "derived_read_only_projection",
            "run_id": projection.run_id,
            "condition": condition,
            "projection_sha256": projection.projection_sha256,
            "results_sha256": projection.source_hashes["results.jsonl"],
        },
        "successful_run_count": len(projection.cases),
        "failed_run_count": 0,
        "missing_run_count": 0,
    }


def test_import_reuses_complete_matching_experiments(tmp_path: Path) -> None:
    run_dir, tasks_path = _write_run(tmp_path)
    projection = phoenix_review.load_projection(run_dir, tasks_path=tasks_path)
    experiments = _FakeExperiments(
        [_existing_experiment(projection, condition) for condition in phoenix_review.CONDITIONS]
    )
    client = SimpleNamespace(datasets=_FakeDatasets(), experiments=experiments)

    result = phoenix_review.import_projection(projection, client=client)

    assert not experiments.created
    assert {item["status"] for item in result["experiments"].values()} == {"reused"}


def test_import_rejects_experiment_bound_to_another_dataset_version(
    tmp_path: Path,
) -> None:
    run_dir, tasks_path = _write_run(tmp_path)
    projection = phoenix_review.load_projection(run_dir, tasks_path=tasks_path)
    experiments = _FakeExperiments(
        [_existing_experiment(projection, "control", dataset_version_id="old-version")]
    )
    client = SimpleNamespace(datasets=_FakeDatasets(), experiments=experiments)

    with pytest.raises(RuntimeError, match="dataset version drift"):
        phoenix_review.import_projection(projection, client=client)
