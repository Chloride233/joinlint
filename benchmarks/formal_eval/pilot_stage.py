from __future__ import annotations

import argparse
import math
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from benchmarks.formal_eval.contracts import AgentResultBundle, AgentResultRow, StrictModel
from benchmarks.formal_eval.dispatch import inspect_subprocess_environment
from benchmarks.formal_eval.export import export_agent_rows
from benchmarks.formal_eval.lineage import digest_value
from benchmarks.formal_eval.pilot import (
    MODAL_CPU_USD_PER_CORE_SECOND,
    MODAL_MEMORY_USD_PER_GIB_SECOND,
    PILOT_DATASET_RELEASE,
    PilotBudgetCheckpoint,
    PilotBudgetEnvelope,
    PilotBudgetReport,
    PilotRegistration,
    model_batch_upper_costs,
    verify_pilot_input_bundle,
)
from benchmarks.formal_eval.pilot_dispatch import (
    build_pilot_commands,
    observed_model_cost_cny,
    pilot_campaign_budget,
    require_batch_health,
)
from benchmarks.formal_eval.run_plan import RunPlanV2
from benchmarks.formal_eval.run_plan import RunSpec, sample_id_for
from joinlint.contracts import canonical_json


STAGE_ID = "flash_full_dataset_v1"
SAFETY_STAGE_ID = "semantic_join_safety_v1"
CONTRACT_SAFETY_STAGE_ID = "semantic_join_contract_safety_v1"
SAFETY_CONFIRMATION_STAGE_ID = "semantic_join_safety_confirmation_v1"
STAGE_APPROVED_BUDGET_CNY = 7.4
SAFETY_STAGE_APPROVED_BUDGET_CNY = 8.0
SAFETY_CONFIRMATION_APPROVED_BUDGET_CNY = 5.53
STAGE_ALPHA = 0.05
SAFETY_CONFIRMATION_TASK_IDS = (
    "sjf-commerce-ordered_products",
    "sjf-healthcare-visit_diagnosis",
    "sjf-healthcare-visit_lab",
    "sjf-subscriptions-account_owner",
)


class PilotStagePreregistration(StrictModel):
    schema_version: Literal[1] = 1
    stage_id: Literal[
        "flash_full_dataset_v1",
        "semantic_join_safety_v1",
        "semantic_join_contract_safety_v1",
        "semantic_join_safety_confirmation_v1",
    ] = STAGE_ID
    claim_scope: Literal[
        "exploratory_bird_pilot",
        "synthetic_join_safety_stress",
        "trusted_query_contract_join_safety_stress",
        "posthoc_synthetic_hard_case_confirmation",
    ] = (
        "exploratory_bird_pilot"
    )
    joinlint_commit: str
    input_lock_sha256: str
    full_run_plan_sha256: str
    stage_run_plan_sha256: str
    model_id: str
    model_tier: Literal["cost_efficient"] = "cost_efficient"
    hosts: tuple[Literal["codex"], Literal["claude_code"]] = (
        "codex",
        "claude_code",
    )
    conditions: tuple[Literal["control"], Literal["treatment"]] = (
        "control",
        "treatment",
    )
    task_count: int = Field(gt=0, le=20)
    paired_unit_count: int = Field(gt=0, le=20)
    run_count: int = Field(gt=0, le=40)
    primary_outcome: Literal["join_correct_task_completion"] = (
        "join_correct_task_completion"
    )
    statistical_test: Literal["exact_two_sided_mcnemar"] = (
        "exact_two_sided_mcnemar"
    )
    alpha: Literal[0.05] = STAGE_ALPHA
    approved_budget_cny: Literal[5.53, 7.4, 8.0] = STAGE_APPROVED_BUDGET_CNY
    workflow_run_id: int = Field(gt=0)
    campaign_reservation_id: str
    campaign_ledger_commit_sha: str

    @field_validator(
        "input_lock_sha256",
        "full_run_plan_sha256",
        "stage_run_plan_sha256",
        "campaign_reservation_id",
    )
    @classmethod
    def require_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("Pilot stage digest must be one lowercase SHA-256")
        return value

    @field_validator("joinlint_commit")
    @classmethod
    def require_commit(cls, value: str) -> str:
        if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("Pilot stage commit must be one full lowercase Git SHA")
        return value

    @field_validator("campaign_ledger_commit_sha")
    @classmethod
    def require_ledger_commit(cls, value: str) -> str:
        if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("Pilot stage ledger commit must be one full lowercase Git SHA")
        return value

    @model_validator(mode="after")
    def require_stage_shape(self) -> PilotStagePreregistration:
        expected = {
            STAGE_ID: ("exploratory_bird_pilot", 20, 20, 40, 7.4),
            SAFETY_STAGE_ID: ("synthetic_join_safety_stress", 20, 20, 40, 8.0),
            CONTRACT_SAFETY_STAGE_ID: (
                "trusted_query_contract_join_safety_stress",
                20,
                20,
                40,
                8.0,
            ),
            SAFETY_CONFIRMATION_STAGE_ID: (
                "posthoc_synthetic_hard_case_confirmation",
                4,
                12,
                24,
                5.53,
            ),
        }[self.stage_id]
        actual = (
            self.claim_scope,
            self.task_count,
            self.paired_unit_count,
            self.run_count,
            self.approved_budget_cny,
        )
        if actual != expected:
            raise ValueError("Pilot stage shape is inconsistent")
        return self


class PilotStageEffect(StrictModel):
    schema_version: Literal[1] = 1
    stage_id: Literal[
        "flash_full_dataset_v1",
        "semantic_join_safety_v1",
        "semantic_join_contract_safety_v1",
        "semantic_join_safety_confirmation_v1",
    ] = STAGE_ID
    preregistration_sha256: str
    primary_outcome: Literal["join_correct_task_completion"] = (
        "join_correct_task_completion"
    )
    statistical_test: Literal["exact_two_sided_mcnemar"] = (
        "exact_two_sided_mcnemar"
    )
    alpha: Literal[0.05] = STAGE_ALPHA
    paired_unit_count: int = Field(gt=0, le=20)
    control_successes: int = Field(ge=0, le=20)
    treatment_successes: int = Field(ge=0, le=20)
    treatment_wins: int = Field(ge=0, le=20)
    control_wins: int = Field(ge=0, le=20)
    both_success: int = Field(ge=0, le=20)
    both_failure: int = Field(ge=0, le=20)
    absolute_improvement: float = Field(ge=-1, le=1)
    exact_p_value: float = Field(ge=0, le=1)
    significant_improvement: bool
    treatment_plan_called: int = Field(ge=0, le=20)
    treatment_mcp_grounded: int = Field(ge=0, le=20)
    treatment_final_sql_validated: int = Field(ge=0, le=20)
    treatment_protocol_compliant: int = Field(ge=0, le=20)

    @field_validator("preregistration_sha256")
    @classmethod
    def require_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("Pilot stage preregistration digest must be one lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def require_consistent_effect(self) -> PilotStageEffect:
        if (
            self.treatment_wins
            + self.control_wins
            + self.both_success
            + self.both_failure
            != self.paired_unit_count
        ):
            raise ValueError("Pilot stage pair counts are inconsistent")
        if self.control_successes != self.control_wins + self.both_success:
            raise ValueError("Pilot stage control count is inconsistent")
        if self.treatment_successes != self.treatment_wins + self.both_success:
            raise ValueError("Pilot stage treatment count is inconsistent")
        expected_effect = (
            self.treatment_successes - self.control_successes
        ) / self.paired_unit_count
        if not math.isclose(self.absolute_improvement, expected_effect, abs_tol=1e-12):
            raise ValueError("Pilot stage effect is inconsistent")
        expected_p = exact_two_sided_mcnemar_p_value(
            treatment_wins=self.treatment_wins,
            control_wins=self.control_wins,
        )
        if not math.isclose(self.exact_p_value, expected_p, abs_tol=1e-12):
            raise ValueError("Pilot stage p-value is inconsistent")
        expected_significant = expected_effect > 0 and expected_p < self.alpha
        if self.significant_improvement != expected_significant:
            raise ValueError("Pilot stage significance decision is inconsistent")
        return self


def flash_stage_run_plan(
    registration: PilotRegistration,
    full_run_plan: RunPlanV2,
) -> RunPlanV2:
    flash = _flash_model(registration)
    runs = tuple(run for run in full_run_plan.runs if run.model_id == flash.returned_id)
    stage_id, _ = _stage_identity(registration)
    plan = RunPlanV2(
        evaluation_id=f"{full_run_plan.evaluation_id}-{stage_id}",
        lineage_id=full_run_plan.lineage_id,
        runs=runs,
        blind_review_sample_ids=(),
    )
    conditions = {run.condition for run in runs}
    if (
        len(runs) != 40
        or len({run.task_id for run in runs}) != 20
        or conditions != {"control", "treatment"}
        or {run.host for run in runs} != {"codex", "claude_code"}
    ):
        raise ValueError("frozen Pilot does not contain the Flash full-dataset stage")
    grouped: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for run in runs:
        grouped[(run.host, run.database_id, run.task_id)].add(run.condition)
    if len(grouped) != 20 or any(value != conditions for value in grouped.values()):
        raise ValueError("Flash stage does not contain 20 complete pairs")
    return plan


def safety_confirmation_run_plan(
    registration: PilotRegistration,
    full_run_plan: RunPlanV2,
) -> RunPlanV2:
    flash = _flash_model(registration)
    originals = {
        run.task_id: run
        for run in full_run_plan.runs
        if run.model_id == flash.returned_id
        and run.condition == "control"
        and run.task_id in SAFETY_CONFIRMATION_TASK_IDS
    }
    if set(originals) != set(SAFETY_CONFIRMATION_TASK_IDS):
        raise ValueError("frozen Pilot is missing a safety confirmation task")
    runs = tuple(
        RunSpec(
            sample_id=sample_id_for(
                task_id=original.task_id,
                database_id=original.database_id,
                corpus=original.corpus,
                condition=condition,
                model_id=original.model_id,
                host="codex",
                repetition=repetition,
            ),
            task_id=original.task_id,
            database_id=original.database_id,
            corpus=original.corpus,
            condition=condition,
            model_id=original.model_id,
            host="codex",
            repetition=repetition,
            confirmatory=False,
        )
        for task_id in SAFETY_CONFIRMATION_TASK_IDS
        for original in (originals[task_id],)
        for repetition in range(3)
        for condition in ("control", "treatment")
    )
    return RunPlanV2(
        evaluation_id=f"{full_run_plan.evaluation_id}-{SAFETY_CONFIRMATION_STAGE_ID}",
        lineage_id=full_run_plan.lineage_id,
        runs=tuple(sorted(runs, key=lambda run: run.sample_id.encode("utf-8"))),
        blind_review_sample_ids=(),
    )


def flash_stage_budget_envelope(registration: PilotRegistration) -> PilotBudgetEnvelope:
    flash = _flash_model(registration)
    flash_index = next(
        index for index, model in enumerate(registration.models) if model == flash
    )
    all_batch_costs = model_batch_upper_costs(registration)
    batches_per_model = len(all_batch_costs) // len(registration.models)
    model_upper = sum(
        all_batch_costs[
            flash_index * batches_per_model : (flash_index + 1) * batches_per_model
        ]
    )
    run_count = registration.task_count * 2
    modal_usd = run_count * registration.modal_sandbox_timeout_seconds * (
        registration.cpu_cores * MODAL_CPU_USD_PER_CORE_SECOND
        + (registration.memory_mib / 1024) * MODAL_MEMORY_USD_PER_GIB_SECOND
    )
    modal_cny = modal_usd * registration.usd_to_cny_upper
    total = model_upper + modal_cny + registration.modal_image_build_reserve_cny
    envelope = PilotBudgetEnvelope(
        run_count=run_count,
        model_cost_upper_cny=model_upper,
        modal_compute_upper_cny=modal_cny,
        modal_image_build_reserve_cny=registration.modal_image_build_reserve_cny,
        total_upper_cny=total,
    )
    if envelope.run_count != 40 or envelope.total_upper_cny > _stage_approved_budget(
        registration
    ):
        raise ValueError("Flash Pilot stage exceeds its approved budget")
    return envelope


def safety_confirmation_budget_envelope(
    registration: PilotRegistration,
) -> PilotBudgetEnvelope:
    full = flash_stage_budget_envelope(registration)
    ratio = 24 / 40
    envelope = PilotBudgetEnvelope(
        run_count=24,
        model_cost_upper_cny=full.model_cost_upper_cny * ratio,
        modal_compute_upper_cny=full.modal_compute_upper_cny * ratio,
        modal_image_build_reserve_cny=full.modal_image_build_reserve_cny,
        total_upper_cny=(
            full.model_cost_upper_cny * ratio
            + full.modal_compute_upper_cny * ratio
            + full.modal_image_build_reserve_cny
        ),
    )
    if envelope.total_upper_cny > SAFETY_CONFIRMATION_APPROVED_BUDGET_CNY:
        raise ValueError("safety confirmation exceeds its approved budget")
    return envelope


def build_flash_stage_commands(
    *,
    inspect: str,
    registration: PilotRegistration,
    root: Path,
    log_dir: Path,
    lineage_id: str,
) -> tuple[list[str], ...]:
    flash = _flash_model(registration)
    task_spec = f"{Path(__file__).with_name('inspect_task.py').resolve()}@formal_pilot_eval"
    commands: list[list[str]] = []
    for original in build_pilot_commands(
        inspect=inspect,
        registration=registration,
        root=root,
        log_dir=log_dir,
        lineage_id=lineage_id,
    ):
        model_id = original[original.index("--model") + 1]
        if model_id != flash.id:
            continue
        command = list(original)
        command[2] = task_spec
        commands.append(command)
    if len(commands) != 4:
        raise ValueError("Flash Pilot stage requires four host-condition commands")
    return tuple(commands)


def build_safety_confirmation_commands(
    *,
    inspect: str,
    registration: PilotRegistration,
    root: Path,
    log_dir: Path,
    lineage_id: str,
) -> tuple[list[str], ...]:
    commands = []
    task_ids = ",".join(SAFETY_CONFIRMATION_TASK_IDS)
    for original in build_flash_stage_commands(
        inspect=inspect,
        registration=registration,
        root=root,
        log_dir=log_dir,
        lineage_id=lineage_id,
    ):
        if next(value for value in original if value.startswith("host=")) != "host=codex":
            continue
        command = list(original)
        command[command.index("--epochs") + 1] = "3"
        partition_index = next(
            index for index, value in enumerate(command) if value.startswith("task_partition=")
        )
        command[partition_index] = f"task_ids={task_ids}"
        commands.append(command)
    if len(commands) != 2:
        raise ValueError("safety confirmation requires two condition commands")
    return tuple(commands)


def pilot_stage_preregistration(
    registration: PilotRegistration,
    full_run_plan: RunPlanV2,
    stage_run_plan: RunPlanV2,
    *,
    input_lock_sha256: str,
    workflow_run_id: int,
    campaign_reservation_id: str,
    campaign_ledger_commit_sha: str,
    confirmation: bool = False,
) -> PilotStagePreregistration:
    if confirmation:
        if _stage_identity(registration)[0] != SAFETY_STAGE_ID:
            raise ValueError("safety confirmation requires the frozen safety Pilot")
        stage_id = SAFETY_CONFIRMATION_STAGE_ID
        claim_scope = "posthoc_synthetic_hard_case_confirmation"
        task_count, paired_unit_count, run_count = 4, 12, 24
        approved_budget = SAFETY_CONFIRMATION_APPROVED_BUDGET_CNY
    else:
        stage_id, claim_scope = _stage_identity(registration)
        task_count, paired_unit_count, run_count = 20, 20, 40
        approved_budget = _stage_approved_budget(registration)
    return PilotStagePreregistration(
        stage_id=stage_id,
        claim_scope=claim_scope,
        task_count=task_count,
        paired_unit_count=paired_unit_count,
        run_count=run_count,
        approved_budget_cny=approved_budget,
        joinlint_commit=registration.joinlint_commit,
        input_lock_sha256=input_lock_sha256,
        full_run_plan_sha256=digest_value(full_run_plan.model_dump(mode="json")),
        stage_run_plan_sha256=digest_value(stage_run_plan.model_dump(mode="json")),
        model_id=_flash_model(registration).returned_id,
        workflow_run_id=workflow_run_id,
        campaign_reservation_id=campaign_reservation_id,
        campaign_ledger_commit_sha=campaign_ledger_commit_sha,
    )


def exact_two_sided_mcnemar_p_value(*, treatment_wins: int, control_wins: int) -> float:
    if treatment_wins < 0 or control_wins < 0:
        raise ValueError("McNemar discordant counts cannot be negative")
    discordant = treatment_wins + control_wins
    if discordant == 0:
        return 1.0
    tail = min(treatment_wins, control_wins)
    probability = sum(math.comb(discordant, value) for value in range(tail + 1))
    return min(1.0, 2.0 * probability / (2**discordant))


def stage_effect_report(
    rows: tuple[AgentResultRow, ...],
    preregistration: PilotStagePreregistration,
) -> PilotStageEffect:
    grouped: dict[tuple[str, str, str, int], dict[str, AgentResultRow]] = defaultdict(dict)
    for row in rows:
        key = (row.host, row.database_id, row.task_id, row.repetition)
        if row.model_id != preregistration.model_id or row.condition in grouped[key]:
            raise ValueError("Pilot stage result identity is invalid")
        grouped[key][row.condition] = row
    if len(grouped) != preregistration.paired_unit_count or any(
        set(pair) != {"control", "treatment"} for pair in grouped.values()
    ):
        raise ValueError("Pilot stage requires 20 complete control-treatment pairs")

    pairs = [(pair["control"], pair["treatment"]) for pair in grouped.values()]
    control_successes = sum(control.join_correct_task_completion for control, _ in pairs)
    treatment_successes = sum(treatment.join_correct_task_completion for _, treatment in pairs)
    treatment_wins = sum(
        not control.join_correct_task_completion and treatment.join_correct_task_completion
        for control, treatment in pairs
    )
    control_wins = sum(
        control.join_correct_task_completion and not treatment.join_correct_task_completion
        for control, treatment in pairs
    )
    both_success = sum(
        control.join_correct_task_completion and treatment.join_correct_task_completion
        for control, treatment in pairs
    )
    both_failure = len(pairs) - treatment_wins - control_wins - both_success
    p_value = exact_two_sided_mcnemar_p_value(
        treatment_wins=treatment_wins,
        control_wins=control_wins,
    )
    treatment_rows = [treatment for _, treatment in pairs]
    improvement = (treatment_successes - control_successes) / len(pairs)
    return PilotStageEffect(
        stage_id=preregistration.stage_id,
        paired_unit_count=preregistration.paired_unit_count,
        preregistration_sha256=digest_value(preregistration.model_dump(mode="json")),
        control_successes=control_successes,
        treatment_successes=treatment_successes,
        treatment_wins=treatment_wins,
        control_wins=control_wins,
        both_success=both_success,
        both_failure=both_failure,
        absolute_improvement=improvement,
        exact_p_value=p_value,
        significant_improvement=improvement > 0 and p_value < STAGE_ALPHA,
        treatment_plan_called=sum(row.plan_called is True for row in treatment_rows),
        treatment_mcp_grounded=sum(row.mcp_grounded is True for row in treatment_rows),
        treatment_final_sql_validated=sum(
            row.final_sql_validated is True for row in treatment_rows
        ),
        treatment_protocol_compliant=sum(
            row.protocol_compliant is True for row in treatment_rows
        ),
    )


def _stage_identity(
    registration: PilotRegistration,
) -> tuple[
    Literal[
        "flash_full_dataset_v1",
        "semantic_join_safety_v1",
        "semantic_join_contract_safety_v1",
    ],
    Literal[
        "exploratory_bird_pilot",
        "synthetic_join_safety_stress",
        "trusted_query_contract_join_safety_stress",
    ],
]:
    if (
        registration.schema_version == 7
        and registration.dataset_release == "semantic-join-contract-safety-pilot-v1"
    ):
        return CONTRACT_SAFETY_STAGE_ID, "trusted_query_contract_join_safety_stress"
    if (
        registration.schema_version == 6
        and registration.dataset_release == "semantic-join-safety-pilot-v1"
    ):
        return SAFETY_STAGE_ID, "synthetic_join_safety_stress"
    if registration.schema_version == 5 and registration.dataset_release == PILOT_DATASET_RELEASE:
        return STAGE_ID, "exploratory_bird_pilot"
    raise ValueError("Pilot registration is not approved for a bounded stage")


def _stage_approved_budget(registration: PilotRegistration) -> float:
    stage_id, _ = _stage_identity(registration)
    if stage_id in {SAFETY_STAGE_ID, CONTRACT_SAFETY_STAGE_ID}:
        return SAFETY_STAGE_APPROVED_BUDGET_CNY
    return STAGE_APPROVED_BUDGET_CNY


def run_flash_stage(
    root: Path,
    log_dir: Path,
    output: Path,
    *,
    campaign_budget_cny: float,
    campaign_spend_before_cny: float,
    workflow_run_id: int,
    campaign_reservation_id: str,
    campaign_ledger_commit_sha: str,
    confirmation: bool = False,
) -> PilotStageEffect:
    if output.exists() or output.is_symlink():
        raise ValueError("Pilot stage output directory must not exist")
    if log_dir.resolve() == output.resolve():
        raise ValueError("Pilot stage log and output directories must be distinct")
    registration, _, full_run_plan, lock = verify_pilot_input_bundle(root)
    stage_run_plan = (
        safety_confirmation_run_plan(registration, full_run_plan)
        if confirmation
        else flash_stage_run_plan(registration, full_run_plan)
    )
    envelope = (
        safety_confirmation_budget_envelope(registration)
        if confirmation
        else flash_stage_budget_envelope(registration)
    )
    approved_budget = (
        SAFETY_CONFIRMATION_APPROVED_BUDGET_CNY
        if confirmation
        else _stage_approved_budget(registration)
    )
    campaign_before = pilot_campaign_budget(
        campaign_budget_cny=campaign_budget_cny,
        campaign_spend_before_cny=campaign_spend_before_cny,
        pilot_cost_upper_cny=approved_budget,
    )
    if not campaign_before.passed:
        raise ValueError("Flash Pilot stage could exceed the campaign budget")
    preregistration = pilot_stage_preregistration(
        registration,
        full_run_plan,
        stage_run_plan,
        input_lock_sha256=digest_value(lock.model_dump(mode="json")),
        workflow_run_id=workflow_run_id,
        campaign_reservation_id=campaign_reservation_id,
        campaign_ledger_commit_sha=campaign_ledger_commit_sha,
        confirmation=confirmation,
    )
    output.mkdir(parents=True)
    _write_json(output / "stage.json", preregistration)
    _write_json(output / "stage-run-plan.json", stage_run_plan)

    inspect = shutil.which("inspect")
    if inspect is None:
        raise ValueError("Inspect CLI is unavailable")
    command_builder = (
        build_safety_confirmation_commands if confirmation else build_flash_stage_commands
    )
    commands = command_builder(
        inspect=inspect,
        registration=registration,
        root=root,
        log_dir=log_dir,
        lineage_id=full_run_plan.lineage_id,
    )
    batch_upper = envelope.model_cost_upper_cny / len(commands)
    for batch_index, command in enumerate(commands):
        observed = observed_model_cost_cny(log_dir, registration)
        _write_stage_checkpoint(
            output,
            envelope=envelope,
            approved_budget_cny=approved_budget,
            completed_batches=batch_index,
            total_batches=len(commands),
            actual_model_cost_cny=observed,
            batch_upper_cny=batch_upper,
        )
        batch_log_dir = Path(command[command.index("--log-dir") + 1])
        batch_log_dir.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(command, check=True, env=inspect_subprocess_environment())
            require_batch_health(
                batch_log_dir,
                expected_sample_count=12 if confirmation else 10,
            )
        except (subprocess.CalledProcessError, RuntimeError):
            _write_stage_checkpoint(
                output,
                envelope=envelope,
                approved_budget_cny=approved_budget,
                completed_batches=batch_index + 1,
                total_batches=len(commands),
                actual_model_cost_cny=observed_model_cost_cny(log_dir, registration),
                batch_upper_cny=batch_upper,
            )
            _export_stage_rows(
                registration=registration,
                log_dir=log_dir,
                output=output,
                lineage_id=full_run_plan.lineage_id,
                stage_run_plan=stage_run_plan,
                allow_incomplete_run_plan=True,
            )
            raise
    observed = observed_model_cost_cny(log_dir, registration)
    _write_stage_checkpoint(
        output,
        envelope=envelope,
        approved_budget_cny=approved_budget,
        completed_batches=len(commands),
        total_batches=len(commands),
        actual_model_cost_cny=observed,
        batch_upper_cny=batch_upper,
    )

    bundle = _export_stage_rows(
        registration=registration,
        log_dir=log_dir,
        output=output,
        lineage_id=full_run_plan.lineage_id,
        stage_run_plan=stage_run_plan,
        allow_incomplete_run_plan=False,
    )
    actual_model_cost = sum(row.calculated_cost_cny for row in bundle.rows)
    total_upper = (
        actual_model_cost
        + envelope.modal_compute_upper_cny
        + envelope.modal_image_build_reserve_cny
    )
    budget = PilotBudgetReport(
        approved_budget_cny=approved_budget,
        actual_model_cost_cny=actual_model_cost,
        modal_compute_upper_cny=envelope.modal_compute_upper_cny,
        modal_image_build_reserve_cny=envelope.modal_image_build_reserve_cny,
        total_cost_upper_cny=total_upper,
        run_count=len(bundle.rows),
        expected_run_count=len(stage_run_plan.runs),
        passed=total_upper <= approved_budget,
    )
    _write_json(output / "budget.json", budget)
    campaign = pilot_campaign_budget(
        campaign_budget_cny=campaign_budget_cny,
        campaign_spend_before_cny=campaign_spend_before_cny,
        pilot_cost_upper_cny=total_upper,
    )
    _write_json(output / "campaign-budget.json", campaign)
    effect = stage_effect_report(bundle.rows, preregistration)
    _write_json(output / "effect.json", effect)
    if not budget.passed or not campaign.passed:
        raise RuntimeError("Flash Pilot stage exceeded its approved budget")
    return effect


def _export_stage_rows(
    *,
    registration: PilotRegistration,
    log_dir: Path,
    output: Path,
    lineage_id: str,
    stage_run_plan: RunPlanV2,
    allow_incomplete_run_plan: bool,
) -> AgentResultBundle:
    flash = _flash_model(registration)
    bundle = export_agent_rows(
        log_dir,
        output / "agent-results-bundle.json",
        expected_model_ids={flash.returned_id},
        model_pricing={flash.returned_id: flash.pricing_cny},
        lineage_id=lineage_id,
        run_plan=stage_run_plan,
        summary_output=output / "cleaning.json",
        allow_incomplete_run_plan=allow_incomplete_run_plan,
    )
    _write_json(output / "pilot-agent-results.json", bundle.rows)
    return bundle


def _write_stage_checkpoint(
    output: Path,
    *,
    envelope: PilotBudgetEnvelope,
    approved_budget_cny: float,
    completed_batches: int,
    total_batches: int,
    actual_model_cost_cny: float,
    batch_upper_cny: float,
) -> None:
    remaining = (total_batches - completed_batches) * batch_upper_cny
    projected = (
        actual_model_cost_cny
        + remaining
        + envelope.modal_compute_upper_cny
        + envelope.modal_image_build_reserve_cny
    )
    checkpoint = PilotBudgetCheckpoint(
        approved_budget_cny=approved_budget_cny,
        completed_batches=completed_batches,
        total_batches=total_batches,
        actual_model_cost_cny=actual_model_cost_cny,
        remaining_model_cost_upper_cny=remaining,
        modal_compute_upper_cny=envelope.modal_compute_upper_cny,
        modal_image_build_reserve_cny=envelope.modal_image_build_reserve_cny,
        projected_total_upper_cny=projected,
        safe_to_continue=projected <= approved_budget_cny,
    )
    _write_json(output / "budget-checkpoint.json", checkpoint)
    if not checkpoint.safe_to_continue:
        raise RuntimeError("Flash Pilot stage stopped before exceeding its budget")


def _write_json(path: Path, value: StrictModel | tuple[AgentResultRow, ...]) -> None:
    payload = (
        value.model_dump(mode="json")
        if isinstance(value, StrictModel)
        else [item.model_dump(mode="json") for item in value]
    )
    path.write_bytes(canonical_json(payload) + b"\n")


def _flash_model(registration: PilotRegistration):
    selected = [model for model in registration.models if model.tier == "cost_efficient"]
    if len(selected) != 1:
        raise ValueError("Pilot registration requires exactly one cost-efficient model")
    return selected[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the preregistered Flash Pilot stage")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--campaign-budget-cny", type=float, required=True)
    parser.add_argument("--campaign-spend-before-cny", type=float, required=True)
    parser.add_argument("--workflow-run-id", type=int, required=True)
    parser.add_argument("--campaign-reservation-id", required=True)
    parser.add_argument("--campaign-ledger-commit-sha", required=True)
    parser.add_argument("--confirmation", action="store_true")
    arguments = parser.parse_args(argv)
    effect = run_flash_stage(
        arguments.root,
        arguments.log_dir,
        arguments.output,
        campaign_budget_cny=arguments.campaign_budget_cny,
        campaign_spend_before_cny=arguments.campaign_spend_before_cny,
        workflow_run_id=arguments.workflow_run_id,
        campaign_reservation_id=arguments.campaign_reservation_id,
        campaign_ledger_commit_sha=arguments.campaign_ledger_commit_sha,
        confirmation=arguments.confirmation,
    )
    print(effect.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
