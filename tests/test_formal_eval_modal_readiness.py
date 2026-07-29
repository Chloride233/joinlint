from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

pytest.importorskip("inspect_ai")

from benchmarks.formal_eval.modal_readiness import verify_modal_readiness


def test_modal_readiness_requires_zero_model_usage_and_attested_score(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    score = SimpleNamespace(
        value=1,
        metadata={
            "readiness_attested": True,
            "host_binary_sha256": "a" * 64,
            "infrastructure_preparation_duration_seconds": 3.5,
        },
    )
    sample = SimpleNamespace(
        error=None,
        model_usage={},
        scores={"formal_modal_readiness_scorer": score},
    )
    monkeypatch.setattr("inspect_ai.log.list_eval_logs", lambda *args, **kwargs: [SimpleNamespace(name="log")])
    monkeypatch.setattr(
        "inspect_ai.log.read_eval_log",
        lambda *args, **kwargs: SimpleNamespace(samples=[sample]),
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

    assert report.model_usage_count == 0
    assert report.modal_image_builder_version == "2025.06 Stable"
    assert report.total_cost_upper_cny == pytest.approx(2.031728)
    assert (tmp_path / "output" / "modal-readiness.json").is_file()

    sample.model_usage = {"unexpected": object()}
    with pytest.raises(RuntimeError, match="unexpectedly called a model"):
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
    run_step = next(
        step
        for step in job["steps"]
        if step.get("name") == "Run one no-model Modal readiness sample"
    )

    assert job["environment"] == "formal-evaluation"
    assert job["permissions"] == {"contents": "read"}
    assert job["if"] == "inputs.readiness_only == true"
    assert canary["if"] == "inputs.readiness_only != true"
    assert job["steps"][1]["with"]["ref"] == "${{ github.sha }}"
    assert "inputs.confirm_paid != true || inputs.budget_cny != '2.05'" in text
    assert set(run_step["env"]) == {
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
        "PYTHONPATH",
    }
    assert run_step["env"]["PYTHONPATH"] == "${{ github.workspace }}"
    assert "mockllm/model" in run_step["run"]
    assert "readiness_time_limit=60" in run_step["run"]
    assert "DEEPSEEK_API_KEY" not in run_step["run"]
    assert "OPENAI_API_KEY" not in run_step["run"]
