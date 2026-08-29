from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from benchmarks.formal_eval.contracts import (
    AgentResultBundle,
    AgentResultRow,
    FormalManifestV2,
    FormalTask,
)
from benchmarks.formal_eval.export import ExportSummary
from benchmarks.formal_eval.lineage import digest_value
from benchmarks.formal_eval.pilot import (
    PilotBudgetCheckpoint,
    build_pilot_run_plan,
    frozen_contract_safety_pilot_registration,
    frozen_pilot_registration,
    frozen_safety_pilot_registration,
)
from benchmarks.formal_eval.pilot_stage import (
    CONTRACT_SAFETY_STAGE_ID,
    CONTRACT_SAFETY_RESUME_APPROVED_BUDGET_CNY,
    SAFETY_CONFIRMATION_APPROVED_BUDGET_CNY,
    SAFETY_CONFIRMATION_STAGE_ID,
    SAFETY_CONFIRMATION_TASK_IDS,
    SAFETY_STAGE_APPROVED_BUDGET_CNY,
    SAFETY_STAGE_ID,
    STAGE_APPROVED_BUDGET_CNY,
    _export_stage_rows,
    build_flash_stage_commands,
    build_safety_confirmation_commands,
    contract_safety_resume_budget_envelope,
    exact_two_sided_mcnemar_p_value,
    flash_stage_budget_envelope,
    flash_stage_run_plan,
    remaining_resume_commands,
    safety_confirmation_budget_envelope,
    safety_confirmation_run_plan,
    pilot_stage_preregistration,
    run_flash_stage,
    stage_effect_report,
    verify_contract_safety_resume_source,
)
from benchmarks.formal_eval.run_plan import RunSpec
from joinlint.contracts import canonical_json


COMMIT = "a" * 40
LINEAGE_ID = "b" * 64
INPUT_LOCK_SHA256 = "c" * 64
RESERVATION_ID = "d" * 64
LEDGER_COMMIT = "e" * 40
FLASH_MODEL_ID = "openai-api/deepseek/deepseek-v4-flash"


def test_flash_stage_freezes_all_tasks_twenty_pairs_and_bounded_cost() -> None:
    registration = frozen_pilot_registration(COMMIT)
    full_plan = build_pilot_run_plan(_manifest(), registration, LINEAGE_ID)

    stage_plan = flash_stage_run_plan(registration, full_plan)
    envelope = flash_stage_budget_envelope(registration)
    preregistration = pilot_stage_preregistration(
        registration,
        full_plan,
        stage_plan,
        input_lock_sha256=INPUT_LOCK_SHA256,
        workflow_run_id=123,
        campaign_reservation_id=RESERVATION_ID,
        campaign_ledger_commit_sha=LEDGER_COMMIT,
    )

    assert len(stage_plan.runs) == 40
    assert len({run.task_id for run in stage_plan.runs}) == 20
    assert {run.model_id for run in stage_plan.runs} == {FLASH_MODEL_ID}
    assert {run.host for run in stage_plan.runs} == {"codex", "claude_code"}
    assert {run.condition for run in stage_plan.runs} == {"control", "treatment"}
    assert envelope.model_cost_upper_cny == pytest.approx(3.6)
    assert envelope.modal_compute_upper_cny == pytest.approx(1.79792)
    assert envelope.modal_image_build_reserve_cny == 2
    assert envelope.total_upper_cny == pytest.approx(7.39792)
    assert envelope.total_upper_cny < STAGE_APPROVED_BUDGET_CNY == 7.4
    assert preregistration.stage_run_plan_sha256 == digest_value(
        stage_plan.model_dump(mode="json")
    )
    assert preregistration.full_run_plan_sha256 == digest_value(
        full_plan.model_dump(mode="json")
    )
    assert preregistration.workflow_run_id == 123
    assert preregistration.campaign_reservation_id == RESERVATION_ID
    assert preregistration.campaign_ledger_commit_sha == LEDGER_COMMIT


def test_safety_stage_has_separate_identity_claim_and_budget() -> None:
    registration = frozen_safety_pilot_registration(COMMIT)
    manifest = _manifest().model_copy(
        update={
            "dataset_release": registration.dataset_release,
            "tasks": tuple(
                task.model_copy(
                    update={
                        "task_id": (
                            "sjf-commerce-ordered_products",
                            "sjf-healthcare-visit_diagnosis",
                            "sjf-healthcare-visit_lab",
                            "sjf-subscriptions-account_owner",
                        )[index]
                        if index < 4
                        else task.task_id,
                        "corpus": "semantic_join_failure",
                    }
                )
                for index, task in enumerate(_manifest().tasks)
            ),
        }
    )
    full_plan = build_pilot_run_plan(manifest, registration, LINEAGE_ID)

    stage_plan = flash_stage_run_plan(registration, full_plan)
    envelope = flash_stage_budget_envelope(registration)
    preregistration = pilot_stage_preregistration(
        registration,
        full_plan,
        stage_plan,
        input_lock_sha256=INPUT_LOCK_SHA256,
        workflow_run_id=123,
        campaign_reservation_id=RESERVATION_ID,
        campaign_ledger_commit_sha=LEDGER_COMMIT,
    )

    assert preregistration.stage_id == SAFETY_STAGE_ID
    assert preregistration.claim_scope == "synthetic_join_safety_stress"
    assert preregistration.approved_budget_cny == SAFETY_STAGE_APPROVED_BUDGET_CNY
    assert stage_plan.evaluation_id.endswith(SAFETY_STAGE_ID)
    assert {run.corpus for run in stage_plan.runs} == {"semantic_join_failure"}
    assert envelope.model_cost_upper_cny == pytest.approx(4.0)
    assert envelope.total_upper_cny == pytest.approx(7.79792)
    assert envelope.total_upper_cny < SAFETY_STAGE_APPROVED_BUDGET_CNY == 8.0


def test_contract_safety_stage_has_separate_identity_and_claim() -> None:
    registration = frozen_contract_safety_pilot_registration(COMMIT)
    manifest = _manifest().model_copy(
        update={
            "dataset_release": registration.dataset_release,
            "tasks": tuple(
                task.model_copy(update={"corpus": "semantic_join_failure"})
                for task in _manifest().tasks
            ),
        }
    )
    full_plan = build_pilot_run_plan(manifest, registration, LINEAGE_ID)

    stage_plan = flash_stage_run_plan(registration, full_plan)
    preregistration = pilot_stage_preregistration(
        registration,
        full_plan,
        stage_plan,
        input_lock_sha256=INPUT_LOCK_SHA256,
        workflow_run_id=123,
        campaign_reservation_id=RESERVATION_ID,
        campaign_ledger_commit_sha=LEDGER_COMMIT,
    )

    assert preregistration.stage_id == CONTRACT_SAFETY_STAGE_ID
    assert preregistration.claim_scope == "trusted_query_contract_join_safety_stress"
    assert preregistration.approved_budget_cny == SAFETY_STAGE_APPROVED_BUDGET_CNY
    assert stage_plan.evaluation_id.endswith(CONTRACT_SAFETY_STAGE_ID)


def test_contract_safety_resume_keeps_one_batch_and_fits_the_existing_campaign() -> None:
    registration, _, stage_plan = _contract_stage()
    envelope = contract_safety_resume_budget_envelope(registration)
    commands = build_flash_stage_commands(
        inspect="/usr/bin/inspect",
        registration=registration,
        root=Path("/frozen"),
        log_dir=Path("/logs"),
        lineage_id=LINEAGE_ID,
    )
    source_rows = tuple(
        _row_for_run(run, success=True, cost=0.001)
        for run in stage_plan.runs
        if run.host == "codex" and run.condition == "control"
    )

    remaining = remaining_resume_commands(commands, stage_plan, source_rows)

    assert envelope.run_count == 30
    assert envelope.model_cost_upper_cny == pytest.approx(3.0)
    assert envelope.modal_compute_upper_cny == pytest.approx(1.34844)
    assert envelope.total_upper_cny == pytest.approx(6.34844)
    assert envelope.total_upper_cny < CONTRACT_SAFETY_RESUME_APPROVED_BUDGET_CNY
    assert len(remaining) == 3
    assert {
        (
            next(value for value in command if value.startswith("host=")),
            next(value for value in command if value.startswith("condition=")),
        )
        for command in remaining
    } == {
        ("host=codex", "condition=treatment"),
        ("host=claude_code", "condition=control"),
        ("host=claude_code", "condition=treatment"),
    }


def test_resume_source_is_bound_to_the_failed_run_and_exact_completed_batch(
    tmp_path: Path,
) -> None:
    registration, full_plan, stage_plan = _contract_stage()
    workflow_commit = "f" * 40
    artifact_sha256 = "1" * 64
    source_stage = pilot_stage_preregistration(
        registration,
        full_plan,
        stage_plan,
        input_lock_sha256=INPUT_LOCK_SHA256,
        workflow_run_id=123,
        campaign_reservation_id=RESERVATION_ID,
        campaign_ledger_commit_sha=LEDGER_COMMIT,
    )
    selected_runs = [
        run
        for run in stage_plan.runs
        if run.host == "codex" and run.condition == "control"
    ]
    source_rows = tuple(
        _row_for_run(run, success=index != 0, cost=0 if index == 0 else 0.01)
        for index, run in enumerate(selected_runs)
    )
    source_bundle = AgentResultBundle(
        lineage_id=LINEAGE_ID,
        run_plan_sha256=digest_value(stage_plan.model_dump(mode="json")),
        rows=tuple(sorted(source_rows, key=lambda row: row.sample_id.encode("utf-8"))),
    )
    source_summary = ExportSummary(
        log_count=1,
        row_count=10,
        artifact_incomplete_count=0,
        model_ids=(FLASH_MODEL_ID,),
        conditions=("control",),
    )
    envelope = flash_stage_budget_envelope(registration)
    observed_cost = sum(row.calculated_cost_cny for row in source_rows)
    checkpoint = PilotBudgetCheckpoint(
        approved_budget_cny=8,
        completed_batches=1,
        total_batches=4,
        actual_model_cost_cny=observed_cost,
        remaining_model_cost_upper_cny=envelope.model_cost_upper_cny * 3 / 4,
        modal_compute_upper_cny=envelope.modal_compute_upper_cny,
        modal_image_build_reserve_cny=envelope.modal_image_build_reserve_cny,
        projected_total_upper_cny=(
            observed_cost
            + envelope.model_cost_upper_cny * 3 / 4
            + envelope.modal_compute_upper_cny
            + envelope.modal_image_build_reserve_cny
        ),
        safe_to_continue=True,
    )
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    _write_json_artifact(artifact / "stage.json", source_stage, trailing_lf=True)
    _write_json_artifact(artifact / "stage-run-plan.json", stage_plan, trailing_lf=True)
    _write_json_artifact(
        artifact / "budget-checkpoint.json",
        checkpoint,
        trailing_lf=True,
    )
    _write_json_artifact(
        artifact / "agent-results-bundle.json",
        source_bundle,
        trailing_lf=False,
    )
    _write_json_artifact(
        artifact / "cleaning.json",
        source_summary,
        trailing_lf=False,
    )
    (artifact / "pilot-agent-results.json").write_bytes(
        canonical_json([row.model_dump(mode="json") for row in source_bundle.rows]) + b"\n"
    )
    run_metadata = tmp_path / "run.json"
    run_metadata.write_text(
        json.dumps(
            {
                "id": 123,
                "run_attempt": 1,
                "event": "workflow_dispatch",
                "status": "completed",
                "conclusion": "failure",
                "name": "formal-pilot",
                "path": ".github/workflows/formal-pilot.yml",
                "head_sha": workflow_commit,
                "repository": {"id": 1_311_654_200, "full_name": "Chloride233/joinlint"},
                "head_repository": {"id": 1_311_654_200},
            }
        ),
        encoding="utf-8",
    )
    artifact_metadata = tmp_path / "artifacts.json"
    artifact_metadata.write_text(
        json.dumps(
            {
                "artifacts": [
                    {
                        "id": 456,
                        "name": "joinlint-formal-pilot-stage-sanitized",
                        "size_in_bytes": 789,
                        "expired": False,
                        "digest": f"sha256:{artifact_sha256}",
                        "workflow_run": {
                            "id": 123,
                            "head_sha": workflow_commit,
                            "repository_id": 1_311_654_200,
                            "head_repository_id": 1_311_654_200,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    bundle, summary, artifact_id, artifact_size = verify_contract_safety_resume_source(
        artifact,
        run_metadata,
        artifact_metadata,
        registration=registration,
        full_run_plan=full_plan,
        stage_run_plan=stage_plan,
        input_lock_sha256=INPUT_LOCK_SHA256,
        expected_source_run_id=123,
        expected_source_artifact_sha256=artifact_sha256,
        expected_source_workflow_commit=workflow_commit,
        expected_source_reservation_id=RESERVATION_ID,
    )

    assert bundle == source_bundle
    assert summary == source_summary
    assert artifact_id == 456
    assert artifact_size == 789
    with pytest.raises(ValueError, match="artifact metadata"):
        verify_contract_safety_resume_source(
            artifact,
            run_metadata,
            artifact_metadata,
            registration=registration,
            full_run_plan=full_plan,
            stage_run_plan=stage_plan,
            input_lock_sha256=INPUT_LOCK_SHA256,
            expected_source_run_id=123,
            expected_source_artifact_sha256="2" * 64,
            expected_source_workflow_commit=workflow_commit,
            expected_source_reservation_id=RESERVATION_ID,
        )


def test_resume_export_combines_source_and_new_rows_without_repeating_samples(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration, full_plan, stage_plan = _contract_stage()
    source_runs = [
        run
        for run in stage_plan.runs
        if run.host == "codex" and run.condition == "control"
    ]
    new_runs = [run for run in stage_plan.runs if run not in source_runs]
    source_bundle = AgentResultBundle(
        lineage_id=LINEAGE_ID,
        run_plan_sha256=digest_value(stage_plan.model_dump(mode="json")),
        rows=tuple(_row_for_run(run, success=True, cost=0.001) for run in source_runs),
    )
    new_bundle = AgentResultBundle(
        lineage_id=LINEAGE_ID,
        run_plan_sha256=digest_value(stage_plan.model_dump(mode="json")),
        rows=tuple(_row_for_run(run, success=True, cost=0.001) for run in new_runs),
    )
    source_summary = ExportSummary(
        log_count=1,
        row_count=10,
        artifact_incomplete_count=0,
        model_ids=(FLASH_MODEL_ID,),
        conditions=("control",),
    )

    def fake_export(*args: object, **kwargs: object) -> AgentResultBundle:
        del args
        summary_path = kwargs["summary_output"]
        assert isinstance(summary_path, Path)
        summary_path.write_bytes(
            canonical_json(
                ExportSummary(
                    log_count=3,
                    row_count=30,
                    artifact_incomplete_count=0,
                    model_ids=(FLASH_MODEL_ID,),
                    conditions=("control", "treatment"),
                ).model_dump(mode="json")
            )
        )
        return new_bundle

    monkeypatch.setattr(
        "benchmarks.formal_eval.pilot_stage.export_agent_rows",
        fake_export,
    )
    output = tmp_path / "output"
    output.mkdir()

    combined = _export_stage_rows(
        registration=registration,
        log_dir=tmp_path / "logs",
        output=output,
        lineage_id=full_plan.lineage_id,
        stage_run_plan=stage_plan,
        allow_incomplete_run_plan=False,
        source_bundle=source_bundle,
        source_summary=source_summary,
    )

    assert len(combined.rows) == 40
    assert {row.sample_id for row in combined.rows} == {
        run.sample_id for run in stage_plan.runs
    }
    written = AgentResultBundle.model_validate_json(
        (output / "agent-results-bundle.json").read_bytes(),
        strict=True,
    )
    summary = ExportSummary.model_validate_json(
        (output / "cleaning.json").read_bytes(),
        strict=True,
    )
    assert written == combined
    assert summary.log_count == 4
    assert summary.row_count == 40


def test_flash_stage_resume_runs_only_missing_batches_and_budgets_incremental_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration, full_plan, stage_plan = _contract_stage()
    source_runs = [
        run
        for run in stage_plan.runs
        if run.host == "codex" and run.condition == "control"
    ]
    source_rows = tuple(
        _row_for_run(run, success=index != 0, cost=0 if index == 0 else 0.001)
        for index, run in enumerate(source_runs)
    )
    source_bundle = AgentResultBundle(
        lineage_id=LINEAGE_ID,
        run_plan_sha256=digest_value(stage_plan.model_dump(mode="json")),
        rows=source_rows,
    )
    source_summary = ExportSummary(
        log_count=1,
        row_count=10,
        artifact_incomplete_count=0,
        model_ids=(FLASH_MODEL_ID,),
        conditions=("control",),
    )
    remaining_rows = tuple(
        _row_for_run(run, success=True, cost=0.001)
        for run in stage_plan.runs
        if run.sample_id not in {row.sample_id for row in source_rows}
    )
    combined = AgentResultBundle(
        lineage_id=LINEAGE_ID,
        run_plan_sha256=digest_value(stage_plan.model_dump(mode="json")),
        rows=tuple(
            sorted(
                (*source_rows, *remaining_rows),
                key=lambda row: row.sample_id.encode("utf-8"),
            )
        ),
    )
    source_root = tmp_path / "source"
    source_root.mkdir()
    source_stage = pilot_stage_preregistration(
        registration,
        full_plan,
        stage_plan,
        input_lock_sha256=INPUT_LOCK_SHA256,
        workflow_run_id=123,
        campaign_reservation_id=RESERVATION_ID,
        campaign_ledger_commit_sha=LEDGER_COMMIT,
    )
    _write_json_artifact(source_root / "stage.json", source_stage, trailing_lf=True)
    monkeypatch.setattr(
        "benchmarks.formal_eval.pilot_stage.verify_pilot_input_bundle",
        lambda root: (
            registration,
            _manifest(),
            full_plan,
            SimpleNamespace(model_dump=lambda mode: {"schema_version": 2}),
        ),
    )
    monkeypatch.setattr(
        "benchmarks.formal_eval.pilot_stage.verify_contract_safety_resume_source",
        lambda *args, **kwargs: (source_bundle, source_summary, 456, 789),
    )
    monkeypatch.setattr(
        "benchmarks.formal_eval.pilot_stage.shutil.which",
        lambda name: "/usr/bin/inspect",
    )
    executed: list[list[str]] = []
    monkeypatch.setattr(
        "benchmarks.formal_eval.pilot_stage.subprocess.run",
        lambda command, **kwargs: executed.append(command),
    )
    monkeypatch.setattr(
        "benchmarks.formal_eval.pilot_stage.require_batch_health",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "benchmarks.formal_eval.pilot_stage.observed_model_cost_cny",
        lambda *args, **kwargs: 0.0,
    )
    monkeypatch.setattr(
        "benchmarks.formal_eval.pilot_stage._export_stage_rows",
        lambda **kwargs: combined,
    )
    output = tmp_path / "output"

    effect = run_flash_stage(
        tmp_path / "frozen",
        tmp_path / "logs",
        output,
        campaign_budget_cny=70,
        campaign_spend_before_cny=62.600378,
        workflow_run_id=999,
        campaign_reservation_id="2" * 64,
        campaign_ledger_commit_sha="3" * 40,
        resume_root=source_root,
        resume_run_metadata=tmp_path / "run.json",
        resume_artifact_metadata=tmp_path / "artifacts.json",
        resume_source_run_id=123,
        resume_source_artifact_sha256="4" * 64,
        resume_source_workflow_commit="5" * 40,
        resume_source_reservation_id=RESERVATION_ID,
    )

    assert len(executed) == 3
    budget = json.loads((output / "budget.json").read_bytes())
    resume = json.loads((output / "resume.json").read_bytes())
    assert budget["approved_budget_cny"] == 6.35
    assert budget["run_count"] == budget["expected_run_count"] == 30
    assert budget["actual_model_cost_cny"] == pytest.approx(0.03)
    assert budget["total_cost_upper_cny"] == pytest.approx(3.37844)
    assert resume["source_workflow_run_id"] == 123
    assert resume["remaining_run_count"] == 30
    assert effect.paired_unit_count == 20


def test_safety_confirmation_freezes_four_hard_tasks_three_pairs_and_budget() -> None:
    registration = frozen_safety_pilot_registration(COMMIT)
    manifest = _manifest().model_copy(
        update={
            "dataset_release": registration.dataset_release,
            "tasks": tuple(
                task.model_copy(
                    update={
                        "task_id": SAFETY_CONFIRMATION_TASK_IDS[index]
                        if index < len(SAFETY_CONFIRMATION_TASK_IDS)
                        else task.task_id,
                        "corpus": "semantic_join_failure",
                    }
                )
                for index, task in enumerate(_manifest().tasks)
            ),
        }
    )
    full_plan = build_pilot_run_plan(manifest, registration, LINEAGE_ID)

    stage_plan = safety_confirmation_run_plan(registration, full_plan)
    envelope = safety_confirmation_budget_envelope(registration)

    assert stage_plan.evaluation_id.endswith(SAFETY_CONFIRMATION_STAGE_ID)
    assert len(stage_plan.runs) == 24
    assert len({run.task_id for run in stage_plan.runs}) == 4
    assert {run.repetition for run in stage_plan.runs} == {0, 1, 2}
    assert {run.condition for run in stage_plan.runs} == {"control", "treatment"}
    assert envelope.run_count == 24
    assert envelope.total_upper_cny == pytest.approx(5.478752)
    assert envelope.total_upper_cny < SAFETY_CONFIRMATION_APPROVED_BUDGET_CNY == 5.53


def test_flash_stage_commands_cover_both_hosts_and_conditions(tmp_path: Path) -> None:
    registration = frozen_pilot_registration(COMMIT)

    commands = build_flash_stage_commands(
        inspect="/usr/bin/inspect",
        registration=registration,
        root=tmp_path / "frozen",
        log_dir=tmp_path / "logs",
        lineage_id=LINEAGE_ID,
    )

    assert len(commands) == 4
    assert {command[command.index("--model") + 1] for command in commands} == {
        "openai-api/deepseek/deepseek-v4-flash"
    }
    assert {
        next(value for value in command if value.startswith("host="))
        for command in commands
    } == {"host=codex", "host=claude_code"}
    assert {
        next(value for value in command if value.startswith("condition="))
        for command in commands
    } == {"condition=control", "condition=treatment"}
    assert all(
        command[2].endswith("benchmarks/formal_eval/inspect_task.py@formal_pilot_eval")
        for command in commands
    )


def test_safety_confirmation_commands_use_codex_three_epochs_and_exact_tasks(
    tmp_path: Path,
) -> None:
    commands = build_safety_confirmation_commands(
        inspect="/usr/bin/inspect",
        registration=frozen_safety_pilot_registration(COMMIT),
        root=tmp_path / "frozen",
        log_dir=tmp_path / "logs",
        lineage_id=LINEAGE_ID,
    )

    assert len(commands) == 2
    assert {command[command.index("--epochs") + 1] for command in commands} == {"3"}
    assert {
        next(value for value in command if value.startswith("host="))
        for command in commands
    } == {"host=codex"}
    assert {
        next(value for value in command if value.startswith("condition="))
        for command in commands
    } == {"condition=control", "condition=treatment"}
    assert {
        next(value for value in command if value.startswith("task_ids="))
        for command in commands
    } == {f"task_ids={','.join(SAFETY_CONFIRMATION_TASK_IDS)}"}


@pytest.mark.parametrize(
    ("treatment_wins", "control_wins", "expected"),
    ((0, 0, 1.0), (5, 0, 0.0625), (6, 0, 0.03125), (7, 0, 0.015625), (6, 1, 0.125)),
)
def test_exact_two_sided_mcnemar_is_preregistered_and_deterministic(
    treatment_wins: int,
    control_wins: int,
    expected: float,
) -> None:
    assert exact_two_sided_mcnemar_p_value(
        treatment_wins=treatment_wins,
        control_wins=control_wins,
    ) == expected


def test_stage_effect_requires_positive_exact_significance() -> None:
    registration = frozen_pilot_registration(COMMIT)
    full_plan = build_pilot_run_plan(_manifest(), registration, LINEAGE_ID)
    stage_plan = flash_stage_run_plan(registration, full_plan)
    preregistration = pilot_stage_preregistration(
        registration,
        full_plan,
        stage_plan,
        input_lock_sha256=INPUT_LOCK_SHA256,
        workflow_run_id=123,
        campaign_reservation_id=RESERVATION_ID,
        campaign_ledger_commit_sha=LEDGER_COMMIT,
    )
    rows = tuple(
        row
        for pair_index in range(20)
        for row in (
            _row(pair_index, condition="control", success=pair_index >= 7),
            _row(pair_index, condition="treatment", success=True),
        )
    )

    effect = stage_effect_report(rows, preregistration)

    assert effect.control_successes == 13
    assert effect.treatment_successes == 20
    assert effect.treatment_wins == 7
    assert effect.control_wins == 0
    assert effect.absolute_improvement == 0.35
    assert effect.exact_p_value == 0.015625
    assert effect.significant_improvement is True
    assert effect.treatment_mcp_grounded == 20


def test_stage_infrastructure_failure_exports_partial_sanitized_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration = frozen_pilot_registration(COMMIT)
    full_plan = build_pilot_run_plan(_manifest(), registration, LINEAGE_ID)
    exported: list[bool] = []
    health_checks: list[bool] = []
    observed_costs = iter((0.0, 0.125))
    monkeypatch.setattr(
        "benchmarks.formal_eval.pilot_stage.verify_pilot_input_bundle",
        lambda root: (
            registration,
            _manifest(),
            full_plan,
            SimpleNamespace(model_dump=lambda mode: {"schema_version": 2}),
        ),
    )
    monkeypatch.setattr(
        "benchmarks.formal_eval.pilot_stage.shutil.which",
        lambda name: "/usr/bin/inspect",
    )
    monkeypatch.setattr(
        "benchmarks.formal_eval.pilot_stage.build_flash_stage_commands",
        lambda **kwargs: tuple(
            [
                "inspect",
                "eval",
                "task",
                "--log-dir",
                str(tmp_path / f"batch-{index}"),
            ]
            for index in range(4)
        ),
    )
    monkeypatch.setattr(
        "benchmarks.formal_eval.pilot_stage.subprocess.run",
        lambda *args, **kwargs: None,
    )
    def fail_systemic_batch(*args: object, **kwargs: object) -> None:
        del args
        health_checks.append(bool(kwargs["allow_isolated_infrastructure_failures"]))
        raise RuntimeError("pilot batch has a systemic infrastructure failure")

    monkeypatch.setattr(
        "benchmarks.formal_eval.pilot_stage.require_batch_health",
        fail_systemic_batch,
    )
    monkeypatch.setattr(
        "benchmarks.formal_eval.pilot_stage.observed_model_cost_cny",
        lambda *args, **kwargs: next(observed_costs),
    )
    monkeypatch.setattr(
        "benchmarks.formal_eval.pilot_stage._export_stage_rows",
        lambda **kwargs: exported.append(kwargs["allow_incomplete_run_plan"]),
    )
    output = tmp_path / "output"

    with pytest.raises(RuntimeError, match="systemic infrastructure failure"):
        run_flash_stage(
            tmp_path / "frozen",
            tmp_path / "logs",
            output,
            campaign_budget_cny=50,
            campaign_spend_before_cny=0,
            workflow_run_id=123,
            campaign_reservation_id=RESERVATION_ID,
            campaign_ledger_commit_sha=LEDGER_COMMIT,
        )

    assert exported == [True]
    assert health_checks == [True]
    checkpoint = json.loads((output / "budget-checkpoint.json").read_bytes())
    assert checkpoint["completed_batches"] == 1
    assert checkpoint["actual_model_cost_cny"] == 0.125


def _manifest() -> FormalManifestV2:
    tasks = tuple(
        FormalTask(
            task_id=f"task-{index:02d}",
            database_id="database-a" if index < 10 else "database-b",
            database_variant_group=f"variant-{index}",
            corpus="natural",
            split="confirmatory",
            domain="test",
            source_type="sqlite",
            database_scale="small",
            ambiguity="none",
            fanout_type="none",
            question_sha256=_digest(f"question-{index}"),
            schema_sha256=_digest(f"schema-{index}"),
            sql_shape_sha256=_digest(f"shape-{index}"),
            schema_family_id=f"schema-{index}",
            question_template_id=f"question-{index}",
            sql_structure_id=f"sql-{index}",
            allowed_graphs=((('child.parent_id', 'parent.id'),),),
            oracle_has_safe_path=True,
            join_depth=1,
        )
        for index in range(20)
    )
    return FormalManifestV2(dataset_release="pilot-stage-test", tasks=tasks)


def _contract_stage() -> tuple[object, object, object]:
    registration = frozen_contract_safety_pilot_registration(COMMIT)
    base = _manifest()
    manifest = base.model_copy(
        update={
            "dataset_release": registration.dataset_release,
            "tasks": tuple(
                task.model_copy(update={"corpus": "semantic_join_failure"})
                for task in base.tasks
            ),
        }
    )
    full_plan = build_pilot_run_plan(manifest, registration, LINEAGE_ID)
    return registration, full_plan, flash_stage_run_plan(registration, full_plan)


def _row_for_run(run: RunSpec, *, success: bool, cost: float) -> AgentResultRow:
    treatment = run.condition == "treatment"
    return AgentResultRow(
        sample_id=run.sample_id,
        task_id=run.task_id,
        database_id=run.database_id,
        corpus=run.corpus,
        condition=run.condition,
        model_id=run.model_id,
        host=run.host,
        domain="test",
        source_type="sqlite",
        database_scale="small",
        join_depth=1,
        ambiguity="none",
        fanout_type="none",
        repetition=run.repetition,
        output_sha256=_digest(run.sample_id),
        artifact_complete=True,
        oracle_has_safe_path=True,
        submitted_sql=success,
        safe_abstention=False,
        join_graph_correct=success,
        evaluator_validation_passed=success,
        join_correct_task_completion=success,
        dangerous_sql_submitted=False,
        execution_correct=success,
        plan_called=success if treatment else None,
        plan_usable=success if treatment else None,
        complete_entity_planning=success if treatment else None,
        final_sql_validated=success if treatment else None,
        mcp_grounded=success if treatment else None,
        protocol_compliant=success if treatment else None,
        protocol_violation=None,
        blocking_applicable=False if treatment else None,
        blocking_compliant=None,
        bypassed=False if treatment else None,
        tool_error=False if treatment else None,
        total_time_seconds=1,
        input_tokens=1 if success else 0,
        input_cache_read_tokens=0,
        input_cache_write_tokens=0,
        output_tokens=1 if success else 0,
        calculated_cost_cny=cost,
        provider_reported_cost_usd=None,
        failure_code=None if success else "INFRASTRUCTURE_FAILURE",
    )


def _write_json_artifact(path: Path, model: object, *, trailing_lf: bool) -> None:
    payload = canonical_json(model.model_dump(mode="json"))  # type: ignore[attr-defined]
    path.write_bytes(payload + (b"\n" if trailing_lf else b""))


def _row(
    pair_index: int,
    *,
    condition: str,
    success: bool,
) -> AgentResultRow:
    treatment = condition == "treatment"
    return AgentResultRow(
        sample_id=f"sample-{pair_index:02d}-{condition}",
        task_id=f"task-{pair_index:02d}",
        database_id="database-a" if pair_index < 10 else "database-b",
        corpus="natural",
        condition=condition,
        model_id=FLASH_MODEL_ID,
        host="codex" if pair_index % 2 == 0 else "claude_code",
        domain="test",
        source_type="sqlite",
        database_scale="small",
        join_depth=1,
        ambiguity="none",
        fanout_type="none",
        repetition=0,
        output_sha256=_digest(f"output-{pair_index}-{condition}"),
        artifact_complete=True,
        oracle_has_safe_path=True,
        submitted_sql=success,
        safe_abstention=False,
        join_graph_correct=success,
        evaluator_validation_passed=success,
        join_correct_task_completion=success,
        dangerous_sql_submitted=False,
        execution_correct=success,
        plan_called=True if treatment else None,
        plan_usable=True if treatment else None,
        complete_entity_planning=True if treatment else None,
        final_sql_validated=True if treatment else None,
        mcp_grounded=True if treatment else None,
        protocol_compliant=True if treatment else None,
        protocol_violation=None,
        blocking_applicable=False if treatment else None,
        blocking_compliant=None,
        bypassed=False if treatment else None,
        tool_error=False if treatment else None,
        total_time_seconds=1,
        input_tokens=1,
        input_cache_read_tokens=0,
        input_cache_write_tokens=0,
        output_tokens=1,
        calculated_cost_cny=0.001,
        provider_reported_cost_usd=None,
        failure_code=None if success else "WRONG_PLAN",
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
