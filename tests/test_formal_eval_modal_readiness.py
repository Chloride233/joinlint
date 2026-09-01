from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

pytest.importorskip("inspect_ai")

from benchmarks.formal_eval.modal_readiness import (
    MODAL_READINESS_BUDGET_CNY,
    MODAL_READINESS_COMPUTE_UPPER_CNY,
    verify_modal_readiness,
)
from benchmarks.formal_eval.public_artifacts import validate_public_artifacts


def test_host_context_task_uses_real_hosts_but_short_circuits_provider(
    tmp_path: Path,
) -> None:
    pytest.importorskip("inspect_swe")
    pytest.importorskip("inspect_sandboxes")
    from benchmarks.formal_eval.inspect_task import formal_host_context_eval

    database = tmp_path / "database.sqlite"
    database.write_bytes(b"SQLite fixture")
    task = formal_host_context_eval.__wrapped__(
        database=str(database),
        host="codex",
        agent_version="0.144.1",
        dockerfile="Dockerfile.formal-pilot",
    )

    assert task.name == "jl-context"
    assert task.config.max_tokens == 1
    assert task.config.cache is False
    assert task.sandbox.type == "modal"
    assert task.sandbox.config.extensions == {"x-modal": {"timeout": 120}}
    assert task.dataset[0].metadata == {"host": "codex", "agent_version": "0.144.1"}


def test_modal_readiness_requires_two_short_circuited_host_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def sample(host: str, version: str, digest: str) -> SimpleNamespace:
        score = SimpleNamespace(
            value=1,
            metadata={
                "readiness_attested": True,
                "provider_short_circuited": True,
                "host": host,
                "agent_version": version,
                "host_binary_sha256": digest,
                "infrastructure_preparation_duration_seconds": 3.5,
                "tool_names": sorted(
                    ["execute_sql", "submit_sql", "get_join_plan", "validate_sql"]
                ),
            },
        )
        return SimpleNamespace(
            error=None,
            model_usage={},
            scores={"formal_host_context_scorer": score},
        )

    samples = {
        "claude": sample("claude_code", "2.1.212", "a" * 64),
        "codex": sample("codex", "0.144.1", "b" * 64),
    }
    monkeypatch.setattr(
        "inspect_ai.log.list_eval_logs",
        lambda *args, **kwargs: [
            SimpleNamespace(name="claude"),
            SimpleNamespace(name="codex"),
        ],
    )
    monkeypatch.setattr(
        "inspect_ai.log.read_eval_log",
        lambda name, **kwargs: SimpleNamespace(samples=[samples[name]]),
    )
    monkeypatch.setattr(
        "benchmarks.formal_eval.modal_readiness.dependency_versions",
        lambda: {
            "inspect-ai": "0.3.249",
            "inspect-sandboxes": "0.4.0",
            "inspect-swe": "0.2.66",
            "modal": "1.5.3",
        },
    )

    report = verify_modal_readiness(
        tmp_path / "logs",
        tmp_path / "output",
        workflow_run_id=123,
        joinlint_commit="b" * 40,
    )

    assert report.provider_token_count == 0
    assert MODAL_READINESS_BUDGET_CNY == 2.10
    assert MODAL_READINESS_COMPUTE_UPPER_CNY == pytest.approx(0.063456)
    assert report.modal_image_builder_version == "2025.06 Stable"
    assert report.total_cost_upper_cny == pytest.approx(2.063456)
    assert [cell.host for cell in report.cells] == ["claude_code", "codex"]
    assert (tmp_path / "output" / "modal-readiness.json").is_file()
    assert validate_public_artifacts(tmp_path / "output", "modal-readiness") == (
        "modal-readiness.json",
    )

    samples["codex"].model_usage = {
        "unexpected": SimpleNamespace(
            input_tokens=1,
            input_tokens_cache_read=0,
            input_tokens_cache_write=0,
            output_tokens=0,
        )
    }
    with pytest.raises(RuntimeError, match="provider tokens"):
        verify_modal_readiness(
            tmp_path / "logs",
            tmp_path / "output",
            workflow_run_id=123,
            joinlint_commit="b" * 40,
        )


def test_modal_readiness_workflow_scopes_secrets_and_budget() -> None:
    path = Path(".github/workflows/formal-pilot-canary.yml")
    workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    text = path.read_text(encoding="utf-8")
    job = workflow["jobs"]["readiness"]
    canary = workflow["jobs"]["canary"]
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    run_steps = [
        step
        for step in job["steps"]
        if step.get("name")
        in {
            "Run no-model Codex host-context sample",
            "Run no-model Claude host-context sample",
        }
    ]

    assert job["environment"] == "formal-evaluation"
    assert job["permissions"] == {"contents": "read"}
    assert job["if"] == "inputs.readiness_only == true && inputs.calibration != true"
    assert canary["if"] == (
        "inputs.readiness_only != true || inputs.calibration == true"
    )
    assert job["steps"][0]["name"] == (
        "Block paid evaluation pending atomic campaign reservation"
    )
    exact_gate = next(
        step
        for step in job["steps"]
        if step.get("name") == "Require the exact approved no-model diagnostic"
    )
    assert "github.run_attempt != 1" in exact_gate["if"]
    assert inputs["campaign_budget_cny"]["required"] == "true"
    assert inputs["campaign_spend_before_cny"]["required"] == "true"
    checkout_index = next(
        index
        for index, step in enumerate(job["steps"])
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert job["steps"][checkout_index]["with"]["ref"] == "${{ github.sha }}"
    assert job["steps"][checkout_index]["with"]["persist-credentials"] == "false"
    assert "inputs.confirm_paid != true || inputs.budget_cny != '2.10'" in text
    assert len(run_steps) == 2
    preflight = next(
        step
        for step in job["steps"]
        if step.get("name") == "Require cumulative campaign budget for readiness"
    )
    step_names = [step.get("name") for step in job["steps"]]
    assert step_names.index(preflight["name"]) < step_names.index(run_steps[0]["name"])
    assert step_names.index(preflight["name"]) < checkout_index
    assert "benchmarks.formal_eval" not in preflight["run"]
    assert preflight["env"]["CAMPAIGN_COST_UPPER_CNY"] == "2.10"
    sanitized_upload = next(
        step
        for step in job["steps"]
        if (step.get("with") or {}).get("name")
        == "joinlint-formal-modal-readiness-sanitized"
    )
    assert sanitized_upload["with"]["if-no-files-found"] == "error"
    assert all(
        set(step["env"])
        == {"MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "PYTHONPATH"}
        for step in run_steps
    )
    assert all(step["env"]["PYTHONPATH"] == "${{ github.workspace }}" for step in run_steps)
    assert all("mockllm/model" in step["run"] for step in run_steps)
    assert all("formal_host_context_eval" in step["run"] for step in run_steps)
    assert all("readiness_time_limit=60" in step["run"] for step in run_steps)
    assert all("evaluation_time_limit=30" in step["run"] for step in run_steps)
    assert any("host=codex" in step["run"] for step in run_steps)
    assert any("host=claude_code" in step["run"] for step in run_steps)
    assert all("DEEPSEEK_API_KEY" not in step["run"] for step in run_steps)
    assert all("OPENAI_API_KEY" not in step["run"] for step in run_steps)
    sanitized_scan = next(
        step
        for step in job["steps"]
        if step.get("name") == "Scan sanitized readiness artifacts"
    )
    sanitized_upload = next(
        step
        for step in job["steps"]
        if (step.get("with") or {}).get("name")
        == "joinlint-formal-modal-readiness-sanitized"
    )
    assert sanitized_scan["id"] == "scan_readiness_sanitized"
    assert sanitized_upload["with"]["path"] == "modal-readiness-artifacts"
    assert (
        "steps.scan_readiness_sanitized.outcome == 'success'"
        in sanitized_upload["if"]
    )
