from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from benchmarks.formal_eval.contracts import (
    AgentResultBundle,
    FormalManifestV2,
    FormalTask,
    SealedAgentTask,
)
from benchmarks.formal_eval.lineage import digest_value
from benchmarks.formal_eval.manifest import verify_input_lock
from benchmarks.formal_eval.pilot import (
    _input_lock,
    budget_envelope,
    build_pilot_run_plan,
    frozen_pilot_registration,
    model_batch_upper_costs,
    pilot_budget_checkpoint,
    pilot_budget_report,
)
from benchmarks.formal_eval.pilot_dispatch import build_pilot_commands


COMMIT = "a" * 40
LINEAGE_ID = "b" * 64


def test_pilot_budget_envelope_is_below_the_approved_hard_limit() -> None:
    registration = frozen_pilot_registration(COMMIT)

    envelope = budget_envelope(registration)

    assert envelope.run_count == 160
    assert envelope.model_cost_upper_cny == pytest.approx(12.8)
    assert envelope.modal_compute_upper_cny == pytest.approx(5.07648)
    assert envelope.modal_image_build_reserve_cny == 2.0
    assert envelope.total_upper_cny == pytest.approx(19.87648)
    assert envelope.total_upper_cny < registration.budget_cny == 20.0
    assert {model.family for model in registration.models} == {"deepseek-v4"}
    assert {model.tier for model in registration.models} == {
        "high_capability",
        "cost_efficient",
    }
    assert model_batch_upper_costs(registration) == pytest.approx(
        (2.4, 2.4, 2.4, 2.4, 0.8, 0.8, 0.8, 0.8)
    )


def test_pilot_budget_checkpoint_stops_before_an_unsafe_next_batch() -> None:
    registration = frozen_pilot_registration(COMMIT)

    safe = pilot_budget_checkpoint(
        registration,
        completed_batches=1,
        actual_model_cost_cny=2.4,
    )
    unsafe = pilot_budget_checkpoint(
        registration,
        completed_batches=1,
        actual_model_cost_cny=4.0,
    )

    assert safe.safe_to_continue is True
    assert safe.projected_total_upper_cny == pytest.approx(19.87648)
    assert unsafe.safe_to_continue is False
    assert unsafe.projected_total_upper_cny == pytest.approx(21.47648)


def test_pilot_run_plan_is_exactly_twenty_tasks_and_160_runs() -> None:
    registration = frozen_pilot_registration(COMMIT)
    plan = build_pilot_run_plan(_manifest(20), registration, LINEAGE_ID)

    assert len({run.task_id for run in plan.runs}) == 20
    assert len(plan.runs) == 160
    assert {run.repetition for run in plan.runs} == {0}
    assert {run.condition for run in plan.runs} == {"control", "treatment"}
    assert {run.host for run in plan.runs} == {"codex", "claude_code"}
    assert {run.model_id for run in plan.runs} == {
        model.returned_id for model in registration.models
    }
    assert not any(run.confirmatory for run in plan.runs)


def test_pilot_input_lock_detects_tampering(tmp_path: Path) -> None:
    locked = tmp_path / "manifest.json"
    locked.write_text("frozen", encoding="utf-8")
    lock = _input_lock(tmp_path)
    verify_input_lock(lock, tmp_path)

    locked.write_text("tampered", encoding="utf-8")

    with pytest.raises(ValueError, match="locked input hash mismatch"):
        verify_input_lock(lock, tmp_path)


def test_pilot_dispatch_has_eight_non_retrying_bounded_batches(tmp_path: Path) -> None:
    registration = frozen_pilot_registration(COMMIT)

    commands = build_pilot_commands(
        inspect="/usr/bin/inspect",
        registration=registration,
        root=tmp_path / "sealed",
        log_dir=tmp_path / "logs",
        lineage_id=LINEAGE_ID,
    )

    assert len(commands) == 8
    assert {command[command.index("--epochs") + 1] for command in commands} == {"1"}
    assert {command[command.index("--max-retries") + 1] for command in commands} == {"0"}
    assert {command[command.index("--time-limit") + 1] for command in commands} == {"90"}
    assert {command[command.index("--max-sandboxes") + 1] for command in commands} == {"2"}
    assert all("token_limit=20000" in command for command in commands)
    assert all("time_limit=90" in command for command in commands)
    assert all("sandbox_timeout=120" in command for command in commands)
    assert all("cpu=0.5" in command for command in commands)
    assert all("memory_mib=2048" in command for command in commands)


def test_pilot_budget_report_fails_when_observed_total_exceeds_ceiling() -> None:
    registration = frozen_pilot_registration(COMMIT)
    plan = build_pilot_run_plan(_manifest(20), registration, LINEAGE_ID)
    cost_per_row = 15.0 / len(plan.runs)
    results = AgentResultBundle.model_construct(
        lineage_id=LINEAGE_ID,
        run_plan_sha256=digest_value(plan.model_dump(mode="json")),
        rows=tuple(
            SimpleNamespace(sample_id=run.sample_id, calculated_cost_cny=cost_per_row)
            for run in plan.runs
        ),
    )

    report = pilot_budget_report(registration, plan, results)

    assert report.run_count == 160
    assert report.total_cost_upper_cny > report.approved_budget_cny
    assert report.passed is False


def test_pilot_task_resolves_database_relative_to_sealed_root(tmp_path: Path) -> None:
    pytest.importorskip("inspect_ai")
    from benchmarks.formal_eval.inspect_task import _samples

    sealed_root = tmp_path / "sealed"
    database = sealed_root / "databases" / "case.sqlite"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"SQLite fixture")
    task = _sealed_task()
    manifest = _manifest(1, task=task)

    samples = _samples(
        manifest,
        {task.task_id: task},
        "control",
        "codex",
        LINEAGE_ID,
        sealed_root=sealed_root,
    )

    assert samples[0].files == {"/workspace/data/database.sqlite": str(database.resolve())}
    assert samples[0].metadata["database_path"] == str(database.resolve())


def test_pilot_task_uses_modal_compose_build_and_resource_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("inspect_ai")
    pytest.importorskip("inspect_sandboxes")
    from benchmarks.formal_eval.inspect_task import formal_pilot_eval

    sealed_root = tmp_path / "sealed"
    database = sealed_root / "databases" / "case.sqlite"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"SQLite fixture")
    task = _sealed_task()
    manifest = _manifest(1, task=task)
    tasks_path = sealed_root / "agent-tasks.json"
    manifest_path = sealed_root / "manifest.json"
    tasks_path.write_text(
        "[" + task.model_dump_json(by_alias=True) + "]",
        encoding="utf-8",
    )
    manifest_path.write_text(manifest.model_dump_json(), encoding="utf-8")

    inspect_task = formal_pilot_eval.__wrapped__(
        sealed_tasks=str(tasks_path),
        manifest=str(manifest_path),
        host="codex",
        condition="control",
        agent_version="0.144.1",
        dockerfile="Dockerfile.formal-pilot",
        lineage_id=LINEAGE_ID,
    )

    sandbox = inspect_task.sandbox
    service = sandbox.config.services["default"]
    assert sandbox.type == "modal"
    assert service.build.context == "."
    assert service.build.dockerfile == "Dockerfile.formal-pilot"
    assert service.cpus == 0.5
    assert service.mem_limit == "2048m"
    assert inspect_task.token_limit == 20_000
    assert inspect_task.time_limit == 90
    assert sandbox.config.extensions == {"x-modal": {"timeout": 120}}

    captured: dict[str, str] = {}

    def fake_image(dockerfile: str, *, context_dir: str) -> str:
        captured.update(dockerfile=dockerfile, context_dir=context_dir)
        return "test-image"

    from inspect_sandboxes.modal import _compose

    monkeypatch.setattr(_compose.modal.Image, "from_dockerfile", fake_image)
    params = _compose.convert_compose_to_modal_params(sandbox.config, None)
    assert Path(captured["dockerfile"]).resolve() == Path("Dockerfile.formal-pilot").resolve()
    assert Path(captured["context_dir"]).resolve() == Path.cwd()
    assert params.kwargs["image"] == "test-image"
    assert params.kwargs["cpu"] == 0.5
    assert params.kwargs["memory"] == 2048
    assert params.kwargs["timeout"] == 120


def test_pilot_workflow_requires_exact_approval_and_scopes_paid_secrets() -> None:
    workflow_path = Path(".github/workflows/formal-pilot.yml")
    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    text = workflow_path.read_text(encoding="utf-8")
    job = workflow["jobs"]["pilot"]

    assert job["environment"] == "formal-evaluation"
    assert job["runs-on"] == "ubuntu-latest"
    assert "inputs.confirm_paid != true || inputs.budget_cny != '20'" in text
    assert "--current-commit \"${{ github.sha }}\"" in text
    assert "pilot_volume" not in text
    assert "modal volume get" not in text
    assert "gh release download \"$PILOT_RELEASE_TAG\"" in text
    assert "tar -xzf \"pilot-release/$PILOT_RELEASE_ASSET\"" in text
    assert "${{ inputs." not in "\n".join(
        step.get("run", "") for step in job["steps"]
    )
    restore_step = next(
        step for step in job["steps"] if step.get("name") == "Restore private frozen pilot inputs"
    )
    assert set(restore_step["env"]) == {
        "GH_TOKEN",
        "PILOT_RELEASE_TAG",
        "PILOT_RELEASE_ASSET",
    }
    run_step = next(
        step for step in job["steps"] if step.get("name") == "Run the approved 20-task pilot"
    )
    assert set(run_step["env"]) == {
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
    }
    assert "OPENAI_API_KEY" not in text


def _manifest(count: int, *, task: SealedAgentTask | None = None) -> FormalManifestV2:
    tasks = []
    for index in range(count):
        current = task if task is not None else _sealed_task(str(index))
        tasks.append(
            FormalTask(
                task_id=current.task_id,
                database_id=current.database_id,
                database_variant_group=f"variant-{index}",
                corpus="natural",
                split="confirmatory",
                domain="test",
                source_type="sqlite",
                database_scale="small",
                ambiguity="none",
                fanout_type="none",
                question_sha256=_sha256(current.question),
                schema_sha256=_sha256(current.schema_text),
                sql_shape_sha256=_sha256(current.sql_shape),
                schema_family_id=f"schema-{index}",
                question_template_id=f"question-{index}",
                sql_structure_id=f"sql-{index}",
                allowed_graphs=current.allowed_graphs,
                oracle_has_safe_path=True,
                join_depth=1,
            )
        )
    return FormalManifestV2(dataset_release="pilot-test", tasks=tuple(tasks))


def _sealed_task(suffix: str = "0") -> SealedAgentTask:
    return SealedAgentTask(
        task_id=f"task-{suffix}",
        database_id=f"database-{suffix}",
        question="List each child and parent.",
        schema_text="child(parent_id), parent(id)",
        schema={"child": {"parent_id": "INTEGER"}, "parent": {"id": "INTEGER"}},
        sql_shape="select-join",
        gold_sql="SELECT * FROM child JOIN parent ON child.parent_id = parent.id",
        database_path="databases/case.sqlite",
        expected_entities=("child", "parent"),
        allowed_graphs=((('child.parent_id', 'parent.id'),),),
        oracle_has_safe_path=True,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
