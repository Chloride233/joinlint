from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import yaml

from benchmarks.formal_eval.dispatch import REPOSITORY_ROOT, build_dispatch_commands, inspect_subprocess_environment
import pytest

from benchmarks.formal_eval.export import (
    _row,
    _runtime_failure_code,
    calculate_cost_cny,
    require_expected_model_identity,
)
from benchmarks.formal_eval.contracts import ModelPricingCNY
from benchmarks.formal_eval.fake import fake_preregistration


def test_dispatch_builds_only_the_frozen_remote_matrix(tmp_path: Path) -> None:
    preregistration = fake_preregistration()

    confirmatory = build_dispatch_commands(
        inspect="/usr/bin/inspect",
        preregistration=preregistration,
        manifest=tmp_path / "manifest.json",
        sealed_tasks=tmp_path / "tasks.json",
        log_dir=tmp_path / "logs",
        phase="confirmatory",
        lineage_id="1" * 64,
    )
    diagnostic = build_dispatch_commands(
        inspect="/usr/bin/inspect",
        preregistration=preregistration,
        manifest=tmp_path / "manifest.json",
        sealed_tasks=tmp_path / "tasks.json",
        log_dir=tmp_path / "logs",
        phase="diagnostic",
        lineage_id="1" * 64,
    )

    assert len(confirmatory) == 8
    assert len(diagnostic) == 12
    assert {command[command.index("--epochs") + 1] for command in confirmatory} == {"3"}
    assert {command[command.index("--epochs") + 1] for command in diagnostic} == {"1"}
    assert all("--max-retries" in command for command in confirmatory + diagnostic)
    assert all(
        f"image_reference={preregistration.image_reference}" in command
        for command in confirmatory + diagnostic
    )
    assert all(
        f"manifest={(tmp_path / 'manifest.json').resolve()}" in command
        for command in confirmatory + diagnostic
    )
    assert all(
        f"sealed_tasks={(tmp_path / 'tasks.json').resolve()}" in command
        for command in confirmatory + diagnostic
    )


def test_inspect_subprocess_environment_includes_the_repository_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTHONPATH", "existing-path")

    environment = inspect_subprocess_environment()

    assert environment["PYTHONPATH"].split(":") == [str(REPOSITORY_ROOT), "existing-path"]


def test_exported_row_drops_sealed_inputs_and_transcripts() -> None:
    trace = {
        "plan_called": True,
        "plan_usable": True,
        "complete_entity_planning": True,
        "final_sql_validated": True,
        "validation_passed": True,
        "mcp_grounded": True,
        "blocking_applicable": False,
        "blocking_compliant": None,
        "bypassed": False,
        "tool_error": False,
        "returned_edges": [["customers.id", "orders.customer_id"]],
        "failure_code": None,
    }
    sample = SimpleNamespace(
        metadata={
            "task_id": "task-1",
            "database_id": "database-1",
            "condition": "treatment",
            "host": "codex",
            "oracle_has_safe_path": True,
            "domain": "commerce",
            "source_type": "sqlite",
            "database_scale": "small",
            "join_depth": 1,
            "ambiguity": "none",
            "fanout_type": "one_to_many",
            "gold_sql": "SECRET GOLD SQL",
            "database_path": "/private/secret.sqlite",
            "schema_text": "SECRET SCHEMA",
        },
        epoch=1,
        output=SimpleNamespace(completion='{"sql":"SELECT 1","warning":""}'),
        error=None,
        scores={
            "formal_join_scorer": SimpleNamespace(
                value=1,
                metadata={
                    "submitted_sql": True,
                    "safe_abstention": False,
                    "join_graph_correct": True,
                    "evaluator_validation_passed": True,
                    "join_correct_task_completion": True,
                    "dangerous_sql_submitted": False,
                    "failure_code": None,
                    "trace": trace,
                },
            ),
            "formal_execution_scorer": SimpleNamespace(value=1, metadata={}),
        },
        model_usage={
            "model": SimpleNamespace(
                input_tokens=120,
                input_tokens_cache_read=20,
                input_tokens_cache_write=10,
                output_tokens=30,
                total_cost=0.01,
            )
        },
        total_time=2.5,
    )
    log = SimpleNamespace(eval=SimpleNamespace(model="fake/high-capability-v1"))

    pricing = ModelPricingCNY(
        observed_at=date(2026, 7, 26),
        input_cache_hit_per_million_cny=0.025,
        input_cache_miss_per_million_cny=3.0,
        output_per_million_cny=6.0,
    )
    payload = _row(
        log,
        sample,
        model_pricing={"fake/high-capability-v1": pricing},
    ).model_dump(mode="json")
    serialized = json.dumps(payload)

    assert payload["join_correct_task_completion"] is True
    assert payload["input_tokens"] == 150
    assert payload["input_cache_read_tokens"] == 20
    assert payload["input_cache_write_tokens"] == 10
    assert payload["calculated_cost_cny"] == pytest.approx(0.0005705)
    assert "SECRET" not in serialized
    assert "gold_sql" not in payload
    assert "database_path" not in payload
    assert "schema_text" not in payload


def test_export_rejects_changed_returned_model_identity() -> None:
    require_expected_model_identity("model/snapshot-1", {"model/snapshot-1"})

    with pytest.raises(ValueError, match="unexpected returned model identity"):
        require_expected_model_identity("model/latest", {"model/snapshot-1"})


def test_deepseek_pricing_uses_cache_hit_and_miss_rates() -> None:
    pricing = ModelPricingCNY(
        observed_at=date(2026, 7, 26),
        input_cache_hit_per_million_cny=0.02,
        input_cache_miss_per_million_cny=1.0,
        output_per_million_cny=2.0,
    )

    assert calculate_cost_cny(
        input_tokens=20_000,
        input_cache_read_tokens=12_000,
        output_tokens=2_000,
        pricing=pricing,
    ) == pytest.approx(0.01224)


def test_export_distinguishes_model_timeout_from_infrastructure_failure() -> None:
    assert _runtime_failure_code("sample time limit exceeded") == "MODEL_TIMEOUT"
    assert _runtime_failure_code("sandbox vanished") == "INFRASTRUCTURE_FAILURE"
    assert _runtime_failure_code(None) is None


def test_remote_workflow_is_paid_gated_and_never_uses_a_local_runner() -> None:
    workflow_path = Path(".github/workflows/formal-evaluation.yml")
    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    text = workflow_path.read_text(encoding="utf-8")

    assert workflow["jobs"]["formal"]["environment"] == "formal-evaluation"
    assert workflow["jobs"]["formal"]["runs-on"] == "ubuntu-latest"
    assert "confirm_paid" in workflow["on"]["workflow_dispatch"]["inputs"]
    assert "sealed_artifact_run_id" in workflow["on"]["workflow_dispatch"]["inputs"]
    assert "actions/download-artifact@v4" in text
    assert "diagnostic-canary" in text
    assert "--phase confirmatory" in text
    assert "DEEPSEEK_BASE_URL: https://api.deepseek.com" in text
    assert "--preregistration" in text
    assert "--summary formal-artifacts/cleaning.json" in text

    report_text = Path(
        ".github/workflows/formal-evaluation-report.yml"
    ).read_text(encoding="utf-8")
    report_workflow = yaml.load(report_text, Loader=yaml.BaseLoader)
    assert report_workflow["jobs"]["report"]["runs-on"] == "ubuntu-latest"
    assert "--blind-review formal-review/blind-review.json" in report_text
    assert "--current-commit" in report_text
    for job in (workflow["jobs"]["formal"], report_workflow["jobs"]["report"]):
        for step in job["steps"]:
            assert "${{ inputs." not in step.get("run", "")
    assert "benchmarks.formal_eval.deterministic" in text
    assert "power-check" in text
    assert "OPENAI_API_KEY" not in workflow["jobs"]["formal"]["env"]
    assert "MODAL_TOKEN_SECRET" not in workflow["jobs"]["formal"]["env"]
    dispatch_steps = {
        step["name"]: step
        for step in workflow["jobs"]["formal"]["steps"]
        if step.get("name") in {
            "Run diagnostic-canary in Modal",
            "Run confirmatory batch in Modal",
        }
    }
    assert set(dispatch_steps) == {
        "Run diagnostic-canary in Modal",
        "Run confirmatory batch in Modal",
    }
    assert all(
        set(step["env"])
        == {
            "MODAL_TOKEN_ID",
            "MODAL_TOKEN_SECRET",
            "DEEPSEEK_API_KEY",
            "DEEPSEEK_BASE_URL",
        }
        for step in dispatch_steps.values()
    )


def test_docker_context_excludes_all_sealed_and_raw_result_boundaries() -> None:
    dockerfile = Path("benchmarks/formal_eval/Dockerfile").read_text(encoding="utf-8")
    ignored = set(Path(".dockerignore").read_text(encoding="utf-8").splitlines())

    assert "COPY . ." in dockerfile
    assert "benchmarks/formal_eval/sealed" in ignored
    assert "benchmarks/formal_eval/logs" in ignored
    assert "benchmarks/formal_eval/results" in ignored


def test_every_formal_data_mcp_uses_the_sanitized_environment() -> None:
    source = Path("benchmarks/formal_eval/inspect_task.py").read_text(encoding="utf-8")

    assert source.count("MCPServerConfigStdio(") == 3
    assert source.count("env=sanitized_mcp_environment()") == 3
