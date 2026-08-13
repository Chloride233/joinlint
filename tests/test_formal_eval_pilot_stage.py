from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from benchmarks.formal_eval.contracts import AgentResultRow, FormalManifestV2, FormalTask
from benchmarks.formal_eval.lineage import digest_value
from benchmarks.formal_eval.pilot import build_pilot_run_plan, frozen_pilot_registration
from benchmarks.formal_eval.pilot_stage import (
    STAGE_APPROVED_BUDGET_CNY,
    build_flash_stage_commands,
    exact_two_sided_mcnemar_p_value,
    flash_stage_budget_envelope,
    flash_stage_run_plan,
    pilot_stage_preregistration,
    stage_effect_report,
)


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
