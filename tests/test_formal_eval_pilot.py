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
    build_pilot_calibration_spec,
    build_pilot_run_plan,
    frozen_pilot_registration,
    model_batch_upper_costs,
    pilot_budget_checkpoint,
    pilot_budget_report,
)
from benchmarks.formal_eval.pilot_calibration import (
    CALIBRATION_BUDGET_CNY,
    InfrastructureAttestation,
    attest_calibration_samples,
    build_calibration_commands,
    calibration_budget_envelope,
)
from benchmarks.formal_eval.pilot_dispatch import (
    build_pilot_commands,
    model_usage_cost_cny,
    pilot_campaign_budget,
    require_sample_batch_health,
)
from benchmarks.formal_eval.pilot_canary import (
    CANARY_BUDGET_CNY,
    CANARY_HOST,
    CANARY_SANDBOX_TIMEOUT_SECONDS,
    CANARY_TOKEN_LIMIT,
    CANARY_TOKEN_LIMIT_TYPE,
    PilotCanaryReport,
    build_canary_command,
    canary_budget_envelope,
    require_canary_artifacts,
    verify_canary_attestation_values,
)


COMMIT = "a" * 40
LINEAGE_ID = "b" * 64


def test_pilot_budget_envelope_is_below_the_approved_hard_limit() -> None:
    registration = frozen_pilot_registration(COMMIT)

    envelope = budget_envelope(registration)

    assert envelope.run_count == 80
    assert envelope.model_cost_upper_cny == pytest.approx(11.2)
    assert envelope.modal_compute_upper_cny == pytest.approx(3.1728)
    assert envelope.modal_image_build_reserve_cny == 2.0
    assert envelope.total_upper_cny == pytest.approx(16.3728)
    assert envelope.total_upper_cny < registration.budget_cny == 20.0
    assert registration.modal_image_builder_version == "2025.06 Stable"
    assert {model.family for model in registration.models} == {"deepseek-v4"}
    assert {model.tier for model in registration.models} == {
        "high_capability",
        "cost_efficient",
    }
    assert model_batch_upper_costs(registration) == pytest.approx(
        (2.1, 2.1, 2.1, 2.1, 0.7, 0.7, 0.7, 0.7)
    )


def test_pilot_canary_is_one_bounded_treatment_run(tmp_path: Path) -> None:
    registration = frozen_pilot_registration(COMMIT)

    envelope = canary_budget_envelope(registration)
    command = build_canary_command(
        inspect="/usr/bin/inspect",
        registration=registration,
        root=tmp_path / "sealed",
        log_dir=tmp_path / "logs",
        lineage_id=LINEAGE_ID,
    )

    assert CANARY_BUDGET_CNY == 2.25
    assert CANARY_SANDBOX_TIMEOUT_SECONDS == 150
    assert CANARY_TOKEN_LIMIT == 60_000
    assert CANARY_TOKEN_LIMIT_TYPE == "(input*0.5)+output"
    assert envelope.run_count == 1
    assert envelope.model_cost_upper_cny == pytest.approx(0.12)
    assert envelope.modal_compute_upper_cny == pytest.approx(0.03966)
    assert envelope.modal_image_build_reserve_cny == 2.0
    assert envelope.total_upper_cny == pytest.approx(2.15966)
    assert envelope.total_upper_cny < CANARY_BUDGET_CNY
    assert command[command.index("--limit") + 1] == "1"
    assert CANARY_HOST == "claude_code"
    assert "host=claude_code" in command
    assert "condition=treatment" in command
    assert "token_limit=60000" in command
    assert "token_limit=20000" not in command
    assert "token_limit_type=(input*0.5)+output" in command
    assert "sandbox_timeout=150" in command
    assert "sandbox_timeout=120" not in command
    assert registration.models[1].id in command


def test_pilot_calibration_freezes_two_highest_depth_tasks() -> None:
    manifest = _manifest(4)
    depths = (1, 4, 2, 4)
    manifest = manifest.model_copy(
        update={
            "tasks": tuple(
                task.model_copy(update={"join_depth": depth})
                for task, depth in zip(manifest.tasks, depths, strict=True)
            )
        }
    )

    specification = build_pilot_calibration_spec(manifest)

    assert specification.task_ids == ("task-1", "task-3")


def test_pilot_calibration_uses_exact_formal_resource_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import benchmarks.formal_eval.pilot_calibration as calibration

    registration = frozen_pilot_registration(COMMIT)
    manifest = _manifest(20)
    specification = build_pilot_calibration_spec(manifest)
    run_plan = SimpleNamespace(lineage_id=LINEAGE_ID)
    monkeypatch.setattr(
        calibration,
        "verify_pilot_inputs",
        lambda root: (registration, manifest, run_plan),
    )
    monkeypatch.setattr(
        calibration,
        "load_pilot_calibration_spec",
        lambda root, current_manifest: specification,
    )

    commands = build_calibration_commands(
        inspect="/usr/bin/inspect",
        registration=registration,
        root=tmp_path / "sealed",
        log_dir=tmp_path / "logs",
        lineage_id=LINEAGE_ID,
    )
    envelope = calibration_budget_envelope(registration)

    assert CALIBRATION_BUDGET_CNY == 4.0
    assert envelope.run_count == 8
    assert envelope.model_cost_upper_cny == pytest.approx(1.12)
    assert envelope.modal_compute_upper_cny == pytest.approx(0.31728)
    assert envelope.total_upper_cny == pytest.approx(3.43728)
    assert len(commands) == 4
    assert all("condition=treatment" in command for command in commands)
    assert all("token_limit=35000" in command for command in commands)
    assert all("message_limit=12" in command for command in commands)
    assert all("time_limit=90" in command for command in commands)
    assert all("sandbox_timeout=150" in command for command in commands)
    assert all(not any(value.startswith("task_partition=") for value in command) for command in commands)
    expected_task_ids = f"task_ids={','.join(specification.task_ids)}"
    assert all(expected_task_ids in command for command in commands)


def test_pilot_task_ids_accept_inspect_list_normalization() -> None:
    from benchmarks.formal_eval.inspect_task import _normalized_pilot_task_ids

    assert _normalized_pilot_task_ids("task-a,task-b") == ("task-a", "task-b")
    assert _normalized_pilot_task_ids(["task-a", "task-b"]) == ("task-a", "task-b")


def test_pilot_calibration_attests_infrastructure_harness_and_scoring() -> None:
    from benchmarks.formal_eval.lifecycle import (
        allow_scoring,
        complete_evaluation,
        infrastructure_prepared,
        new_lifecycle,
        readiness_passed,
        start_evaluation,
    )

    registration = frozen_pilot_registration(COMMIT)
    task_ids = ("task-a", "task-b")
    lifecycle = new_lifecycle("codex", "0.144.1")
    lifecycle = infrastructure_prepared(
        lifecycle,
        duration_seconds=1,
        host_binary_sha256="c" * 64,
    )
    lifecycle = readiness_passed(lifecycle, duration_seconds=1)
    lifecycle = start_evaluation(lifecycle)
    lifecycle = complete_evaluation(lifecycle, duration_seconds=1)
    lifecycle = allow_scoring(lifecycle)
    trace = {
        "plan_called": True,
        "plan_usable": True,
        "complete_entity_planning": True,
        "final_sql_validated": True,
        "validation_passed": True,
        "mcp_grounded": True,
        "tool_error": False,
    }
    samples = []
    for model in registration.models:
        for host in registration.hosts:
            for task_id in task_ids:
                host_lifecycle = lifecycle.model_copy(
                    update={"host": host, "agent_version": registration.host_versions[host]}
                )
                samples.append(
                    SimpleNamespace(
                        metadata={"host": host, "task_id": task_id},
                        model_usage={model.returned_id: object()},
                        store={
                            "joinlint.formal_eval.lifecycle.v1": host_lifecycle.model_dump(
                                mode="json"
                            )
                        },
                        scores={
                            "formal_join_scorer": SimpleNamespace(
                                metadata={"failure_code": None, "trace": trace}
                            ),
                            "formal_execution_scorer": SimpleNamespace(
                                metadata={"error_code": None}
                            ),
                        },
                    )
                )

    infrastructure, harness, scoring = attest_calibration_samples(
        samples,
        registration=registration,
        task_ids=task_ids,
    )

    assert infrastructure.status == "passed"
    assert harness.status == "passed"
    assert scoring.status == "passed"
    assert len(infrastructure.cells) == len(harness.cells) == len(scoring.cells) == 8

    samples[0].scores["formal_join_scorer"].metadata["failure_code"] = "SQL_PARSE_FAILED"
    _, _, failed_scoring = attest_calibration_samples(
        samples,
        registration=registration,
        task_ids=task_ids,
    )
    assert failed_scoring.status == "failed"

    with pytest.raises(ValueError, match="status is inconsistent"):
        InfrastructureAttestation(status="passed", cells=())


def test_pilot_canary_requires_model_usage_and_both_scorers() -> None:
    expected_model = "openai-api/deepseek/deepseek-v4-pro"
    sample = SimpleNamespace(
        error=None,
        scores={
            "formal_join_scorer": SimpleNamespace(metadata={"scoring_eligible": True}),
            "formal_execution_scorer": SimpleNamespace(metadata={"scoring_eligible": True}),
        },
        model_usage={expected_model: object()},
    )

    model_id, scorers = require_canary_artifacts(
        log_model_id=expected_model,
        samples=[sample],
        expected_model_id=expected_model,
    )

    assert model_id == expected_model
    assert scorers == ("formal_execution_scorer", "formal_join_scorer")

    with pytest.raises(RuntimeError, match="provider model identity"):
        require_canary_artifacts(
            log_model_id=expected_model,
            samples=[SimpleNamespace(**{**vars(sample), "model_usage": {}})],
            expected_model_id=expected_model,
        )

    ineligible = SimpleNamespace(
        **{
            **vars(sample),
            "scores": {
                "formal_join_scorer": SimpleNamespace(
                    metadata={"scoring_eligible": False}
                ),
                "formal_execution_scorer": SimpleNamespace(metadata={}),
            },
        }
    )
    with pytest.raises(RuntimeError, match="lifecycle was not scoring eligible"):
        require_canary_artifacts(
            log_model_id=expected_model,
            samples=[ineligible],
            expected_model_id=expected_model,
        )


def test_observed_cost_treats_cache_read_as_additional_input_usage(
) -> None:
    registration = frozen_pilot_registration(COMMIT)
    usage = SimpleNamespace(
        input_tokens=984,
        input_tokens_cache_read=43_008,
        input_tokens_cache_write=None,
        output_tokens=482,
    )
    cost = model_usage_cost_cny(usage, registration.models[0].pricing_cny)

    assert cost == pytest.approx(0.0069192)


def test_pilot_canary_attestation_binds_run_commit_input_and_dependencies() -> None:
    dependencies = {
        "anthropic": "0.120.2",
        "inspect-ai": "0.3.249",
        "inspect-sandboxes": "0.4.0",
        "inspect-swe": "0.2.66",
        "modal": "1.5.3",
    }
    report = PilotCanaryReport(
        model_id="openai-api/deepseek/deepseek-v4-pro",
        scorer_artifacts=("formal_execution_scorer", "formal_join_scorer"),
        actual_model_cost_cny=0.01,
        modal_compute_upper_cny=0.031728,
        modal_image_build_reserve_cny=2.0,
        total_cost_upper_cny=2.041728,
        workflow_run_id=123,
        joinlint_commit=COMMIT,
        input_lock_sha256="c" * 64,
        dependency_versions=dependencies,
    )
    run_metadata = {
        "id": 123,
        "name": "formal-pilot-canary",
        "path": ".github/workflows/formal-pilot-canary.yml",
        "event": "workflow_dispatch",
        "conclusion": "success",
        "head_sha": COMMIT,
        "head_repository": {"full_name": "Chloride233/joinlint"},
    }

    verify_canary_attestation_values(
        report,
        expected_run_id=123,
        current_commit=COMMIT,
        input_lock_sha256="c" * 64,
        dependency_versions=dependencies,
        run_metadata=run_metadata,
        repository="Chloride233/joinlint",
    )

    with pytest.raises(ValueError, match="commit"):
        verify_canary_attestation_values(
            report,
            expected_run_id=123,
            current_commit="d" * 40,
            input_lock_sha256="c" * 64,
            dependency_versions=dependencies,
            run_metadata=run_metadata,
            repository="Chloride233/joinlint",
        )


def test_pilot_budget_checkpoint_stops_before_an_unsafe_next_batch() -> None:
    registration = frozen_pilot_registration(COMMIT)

    safe = pilot_budget_checkpoint(
        registration,
        completed_batches=1,
        actual_model_cost_cny=2.1,
    )
    unsafe = pilot_budget_checkpoint(
        registration,
        completed_batches=1,
        actual_model_cost_cny=6.0,
    )

    assert safe.safe_to_continue is True
    assert safe.projected_total_upper_cny == pytest.approx(16.3728)
    assert unsafe.safe_to_continue is False
    assert unsafe.projected_total_upper_cny == pytest.approx(20.2728)


def test_pilot_campaign_budget_includes_prior_investigation_spend() -> None:
    safe = pilot_campaign_budget(
        campaign_budget_cny=30,
        campaign_spend_before_cny=8,
        pilot_cost_upper_cny=20,
    )
    unsafe = pilot_campaign_budget(
        campaign_budget_cny=27,
        campaign_spend_before_cny=8,
        pilot_cost_upper_cny=20,
    )

    assert safe.campaign_total_upper_cny == 28
    assert safe.passed is True
    assert unsafe.passed is False


def test_pilot_run_plan_is_twenty_tasks_and_80_balanced_crossover_runs() -> None:
    registration = frozen_pilot_registration(COMMIT)
    plan = build_pilot_run_plan(_manifest(20), registration, LINEAGE_ID)

    assert len({run.task_id for run in plan.runs}) == 20
    assert len(plan.runs) == 80
    assert {run.repetition for run in plan.runs} == {0}
    assert {run.condition for run in plan.runs} == {"control", "treatment"}
    assert {run.host for run in plan.runs} == {"codex", "claude_code"}
    assert {run.model_id for run in plan.runs} == {
        model.returned_id for model in registration.models
    }
    assert {
        sum(run.task_id == task_id for run in plan.runs)
        for task_id in {run.task_id for run in plan.runs}
    } == {4}
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
    assert all("--time-limit" not in command for command in commands)
    assert {command[command.index("--max-sandboxes") + 1] for command in commands} == {"2"}
    assert all("token_limit=35000" in command for command in commands)
    assert all("token_limit_type=(input*0.5)+output" in command for command in commands)
    assert all("message_limit=12" in command for command in commands)
    assert all("time_limit=90" in command for command in commands)
    assert all("sandbox_timeout=150" in command for command in commands)
    assert {
        next(value for value in command if value.startswith("task_partition="))
        for command in commands
    } == {"task_partition=even", "task_partition=odd"}
    assert all("cpu=0.5" in command for command in commands)
    assert all("memory_mib=2048" in command for command in commands)
    assert all(
        f"sealed_tasks={(tmp_path / 'sealed' / 'agent-tasks.json').resolve()}" in command
        for command in commands
    )
    assert all(
        f"manifest={(tmp_path / 'sealed' / 'manifest.json').resolve()}" in command
        for command in commands
    )
    assert all(
        f"dockerfile={Path('Dockerfile.formal-pilot').resolve()}" in command
        for command in commands
    )


def test_pilot_dispatch_stops_on_systemic_batch_failure() -> None:
    failed_samples = [
        SimpleNamespace(error=RuntimeError("sandbox failed"), scores={}, model_usage={})
        for _ in range(10)
    ]

    with pytest.raises(RuntimeError, match="systemic infrastructure failure"):
        require_sample_batch_health(failed_samples, expected_sample_count=10)

    one_scored_sample = SimpleNamespace(error=None, scores={"join": 1}, model_usage={})
    require_sample_batch_health(
        failed_samples[:-1] + [one_scored_sample],
        expected_sample_count=10,
    )


def test_pilot_dispatch_stops_on_any_lifecycle_infrastructure_failure() -> None:
    healthy = SimpleNamespace(error=None, scores={"join": 1}, model_usage={}, store={})
    failed = SimpleNamespace(
        error=None,
        scores={"join": 0},
        model_usage={},
        store={
            "joinlint.formal_eval.lifecycle.v1": {"infrastructure_status": "failed"}
        },
    )

    with pytest.raises(RuntimeError, match="contains an infrastructure failure"):
        require_sample_batch_health([healthy] * 9 + [failed], expected_sample_count=10)


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

    assert report.run_count == 80
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
        task_partition="even",
    )

    sandbox = inspect_task.sandbox
    service = sandbox.config.services["default"]
    assert sandbox.type == "modal"
    assert service.build.context == "."
    assert service.build.dockerfile == "Dockerfile.formal-pilot"
    assert service.cpus == 0.5
    assert service.mem_limit == "2048m"
    assert inspect_task.token_limit == 35_000
    assert inspect_task.token_limit_type == "(input*0.5)+output"
    assert inspect_task.message_limit == 12
    assert inspect_task.time_limit is None
    assert sandbox.config.extensions == {"x-modal": {"timeout": 150}}

    from inspect_sandboxes._util.naming import make_sandbox_name

    sandbox_name = make_sandbox_name(
        inspect_task.name,
        {"__sample_id__": "x" * 100},
    )
    assert inspect_task.name == "jl"
    assert len(sandbox_name) < 64

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
    assert params.kwargs["timeout"] == 150


def test_pilot_workflow_requires_exact_approval_and_scopes_paid_secrets() -> None:
    workflow_path = Path(".github/workflows/formal-pilot.yml")
    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    text = workflow_path.read_text(encoding="utf-8")
    job = workflow["jobs"]["pilot"]

    assert job["environment"] == "formal-evaluation"
    assert job["runs-on"] == "ubuntu-latest"
    assert workflow["permissions"]["contents"] == "write"
    assert "inputs.confirm_paid != true || inputs.budget_cny != '20'" in text
    assert workflow["on"]["workflow_dispatch"]["inputs"]["pilot_commit"]["default"] == (
        "05da3fb4b2fa8536caef7a28cd9994b8b84a98c9"
    )
    assert workflow["on"]["workflow_dispatch"]["inputs"]["calibration_run_id"]["required"] == (
        "true"
    )
    assert workflow["permissions"]["actions"] == "read"
    assert "ref: ${{ inputs.pilot_commit }}" in text
    assert "--current-commit \"${{ steps.pilot_commit.outputs.sha }}\"" in text
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
        "CAMPAIGN_BUDGET_CNY",
        "CAMPAIGN_SPEND_BEFORE_CNY",
    }
    assert "--campaign-budget-cny \"$CAMPAIGN_BUDGET_CNY\"" in run_step["run"]
    assert "--campaign-spend-before-cny \"$CAMPAIGN_SPEND_BEFORE_CNY\"" in run_step["run"]
    assert "OPENAI_API_KEY" not in text
    calibration_restore = next(
        step
        for step in job["steps"]
        if step.get("name") == "Restore successful calibration attestation"
    )
    assert set(calibration_restore["env"]) == {"GH_TOKEN", "CALIBRATION_RUN_ID"}
    assert "gh run download" in calibration_restore["run"]
    step_names = [step.get("name") for step in job["steps"]]
    assert step_names.index("Run no-model lifecycle gate") < step_names.index(
        "Restore private frozen pilot inputs"
    )
    assert step_names.index("Verify calibration attestation binding") < step_names.index(
        "Run the approved 20-task pilot"
    )


def test_pilot_canary_workflow_has_an_independent_spend_gate() -> None:
    workflow_path = Path(".github/workflows/formal-pilot-canary.yml")
    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    text = workflow_path.read_text(encoding="utf-8")
    job = workflow["jobs"]["canary"]

    assert job["environment"] == "formal-evaluation"
    assert workflow["permissions"]["contents"] == "write"
    assert "inputs.confirm_paid != true" in text
    assert "inputs.calibration != true && inputs.budget_cny != '2.25'" in text
    step_names = [step.get("name") for step in job["steps"]]
    assert step_names.index("Run no-model lifecycle gate") < step_names.index(
        "Run one-task canary"
    )
    assert "--limit" not in text
    assert "benchmarks.formal_eval.pilot_canary" in text
    assert '--workflow-run-id "${{ github.run_id }}"' in text
    run_step = next(
        step for step in job["steps"] if step.get("name") == "Run one-task canary"
    )
    assert set(run_step["env"]) == {
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
    }


def test_pilot_calibration_workflow_has_four_cell_and_campaign_budget_gates() -> None:
    workflow_path = Path(".github/workflows/formal-pilot-canary.yml")
    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    text = workflow_path.read_text(encoding="utf-8")
    job = workflow["jobs"]["canary"]

    assert job["environment"] == "formal-evaluation"
    assert workflow["on"]["workflow_dispatch"]["inputs"]["calibration"]["default"] == (
        "false"
    )
    assert "inputs.calibration == true && inputs.budget_cny != '4'" in text
    run_step = next(
        step for step in job["steps"] if step.get("name") == "Run sealed four-cell calibration"
    )
    assert run_step["if"] == "inputs.calibration == true"
    assert set(run_step["env"]) == {
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "CAMPAIGN_BUDGET_CNY",
        "CAMPAIGN_SPEND_BEFORE_CNY",
    }
    assert "--campaign-budget-cny \"$CAMPAIGN_BUDGET_CNY\"" in run_step["run"]
    assert "--campaign-spend-before-cny \"$CAMPAIGN_SPEND_BEFORE_CNY\"" in run_step["run"]


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
