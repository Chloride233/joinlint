from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from benchmarks.formal_eval.contracts import Host, InputLockV2, ProtocolViolation, StrictModel
from benchmarks.formal_eval.dispatch import inspect_subprocess_environment
from benchmarks.formal_eval.lifecycle import (
    LIFECYCLE_STORE_KEY,
    LifecycleFailureReason,
    parse_lifecycle,
)
from benchmarks.formal_eval.lineage import digest_value
from benchmarks.formal_eval.manifest import load_document
from benchmarks.formal_eval.pilot import (
    MODAL_CPU_USD_PER_CORE_SECOND,
    MODAL_MEMORY_USD_PER_GIB_SECOND,
    PilotRegistration,
    load_pilot_calibration_spec,
    verify_pilot_inputs,
)
from benchmarks.formal_eval.pilot_dispatch import (
    build_pilot_commands,
    model_usage_cost_cny,
    observed_model_cost_cny,
    require_batch_health,
)
from joinlint.contracts import canonical_json


CALIBRATION_BUDGET_CNY = 4.0
CALIBRATION_TOKEN_LIMITS: dict[Host, int] = {
    "codex": 35_000,
    "claude_code": 35_000,
}
CALIBRATION_TOKEN_ACCOUNTING_CEILINGS: dict[Host, int] = {
    "codex": 45_000,
    "claude_code": 45_000,
}
TARGET_HEADROOM_RATIO = 0.2
REMOTE_DEPENDENCIES = ("anthropic", "inspect-ai", "inspect-sandboxes", "inspect-swe", "modal")


class CalibrationBudgetEnvelope(StrictModel):
    run_count: Literal[8] = 8
    model_cost_upper_cny: float
    modal_compute_upper_cny: float
    modal_image_build_reserve_cny: float
    total_upper_cny: float


class CalibrationResourceContract(StrictModel):
    token_limit_by_host: dict[Host, int]
    token_accounting_ceiling_by_host: dict[Host, int] | None = None
    token_limit_type: Literal["(input*0.5)+output"] = "(input*0.5)+output"
    message_limit: Literal[20] = 20
    evaluation_timeout_seconds: Literal[90] = 90
    sandbox_timeout_seconds: Literal[150] = 150
    cpu_cores: Literal[0.5] = 0.5
    memory_mib: Literal[2048] = 2048

    @model_validator(mode="after")
    def require_frozen_host_limits(self) -> CalibrationResourceContract:
        if self.token_limit_by_host != CALIBRATION_TOKEN_LIMITS:
            raise ValueError("calibration token limits do not match the frozen host contract")
        if (
            self.token_accounting_ceiling_by_host is not None
            and self.token_accounting_ceiling_by_host
            != CALIBRATION_TOKEN_ACCOUNTING_CEILINGS
        ):
            raise ValueError("calibration accounting ceilings do not match the frozen contract")
        return self


class UsageBreakdown(StrictModel):
    uncached_input_tokens: int = Field(ge=0)
    cache_read_input_tokens: int = Field(ge=0)
    cache_write_input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    context_input_tokens: int = Field(ge=0)
    inspect_weighted_tokens: float = Field(ge=0)
    calculated_cost_cny: float = Field(ge=0)
    cache_read_ratio: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def require_consistent_totals(self) -> UsageBreakdown:
        context = (
            self.uncached_input_tokens
            + self.cache_read_input_tokens
            + self.cache_write_input_tokens
        )
        weighted = context * 0.5 + self.output_tokens
        ratio = self.cache_read_input_tokens / context if context else 0.0
        if self.context_input_tokens != context:
            raise ValueError("context input token total is inconsistent")
        if self.inspect_weighted_tokens != weighted:
            raise ValueError("Inspect weighted token total is inconsistent")
        if not math.isclose(self.cache_read_ratio, ratio, rel_tol=0, abs_tol=1e-12):
            raise ValueError("cache-read ratio is inconsistent")
        if not math.isfinite(self.calculated_cost_cny):
            raise ValueError("calculated model cost must be finite")
        return self


class ResourceCell(StrictModel):
    model_id: str
    host: Host
    task_id: str
    configured_token_limit: int = Field(gt=0)
    observed_weighted_tokens: float | None = Field(default=None, ge=0)
    headroom_tokens: float | None = None
    lifecycle_reason: LifecycleFailureReason | None = None
    model_limit_reached: bool
    time_limit_reached: bool
    resource_sufficient: bool
    usage: UsageBreakdown | None = None

    @model_validator(mode="after")
    def require_consistent_resource_state(self) -> ResourceCell:
        if (self.usage is None) != (self.observed_weighted_tokens is None):
            raise ValueError("resource usage and observed weighted tokens must appear together")
        if (self.usage is None) != (self.headroom_tokens is None):
            raise ValueError("resource usage and headroom must appear together")
        if self.usage is not None:
            if self.observed_weighted_tokens != self.usage.inspect_weighted_tokens:
                raise ValueError("observed weighted tokens do not match usage")
            expected_headroom = self.configured_token_limit - self.observed_weighted_tokens
            if self.headroom_tokens != expected_headroom:
                raise ValueError("resource headroom is inconsistent")
        sufficient = (
            self.usage is not None
            and self.observed_weighted_tokens <= self.configured_token_limit
            and not self.model_limit_reached
            and not self.time_limit_reached
        )
        if self.resource_sufficient != sufficient:
            raise ValueError("resource sufficiency is inconsistent")
        if self.model_limit_reached != (
            self.lifecycle_reason == LifecycleFailureReason.MODEL_LIMIT
        ):
            raise ValueError("model-limit flag is inconsistent")
        if self.time_limit_reached != (
            self.lifecycle_reason == LifecycleFailureReason.MODEL_TIMEOUT
        ):
            raise ValueError("time-limit flag is inconsistent")
        return self


class ResourceHostSummary(StrictModel):
    host: Host
    cell_count: int = Field(ge=0)
    configured_token_limit: int = Field(gt=0)
    peak_observed_weighted_tokens: float | None = Field(default=None, ge=0)
    minimum_headroom_tokens: float | None = None
    observed_cache_read_floor_tokens: int | None = Field(default=None, ge=0)
    target_headroom_ratio: Literal[0.2] = TARGET_HEADROOM_RATIO
    target_headroom_tokens: int | None = Field(default=None, ge=0)
    limit_censored: bool


class ResourceAttestation(StrictModel):
    status: Literal["passed", "failed"]
    cells: tuple[ResourceCell, ...]
    hosts: tuple[ResourceHostSummary, ...]

    @model_validator(mode="after")
    def require_consistent_status(self) -> ResourceAttestation:
        complete = len(self.cells) == 8 and len({_cell_key(cell) for cell in self.cells}) == 8
        passed = complete and all(cell.resource_sufficient for cell in self.cells)
        if (self.status == "passed") != passed:
            raise ValueError("resource attestation status is inconsistent")
        host_names = tuple(summary.host for summary in self.hosts)
        cell_hosts = tuple(sorted({cell.host for cell in self.cells}))
        if host_names != cell_hosts:
            raise ValueError("resource host summaries must be unique and sorted")
        expected_hosts = _resource_host_summaries(list(self.cells), host_names)
        if self.hosts != expected_hosts:
            raise ValueError("resource host summaries are inconsistent")
        return self


class InfrastructureCell(StrictModel):
    model_id: str
    host: Host
    task_id: str
    prepared: bool
    host_binary_sha256: str | None


class HarnessCell(StrictModel):
    model_id: str
    host: Host
    task_id: str
    plan_called: bool
    plan_usable: bool
    complete_entity_planning: bool
    final_sql_validated: bool
    validation_passed: bool
    mcp_grounded: bool
    protocol_compliant: bool | None = None
    protocol_violation: ProtocolViolation | None = None
    tool_error: bool


class ScoringCell(StrictModel):
    model_id: str
    host: Host
    task_id: str
    scoring_eligible: bool
    scorer_artifacts: tuple[str, ...]
    output_parseable: bool


class InfrastructureAttestation(StrictModel):
    status: Literal["passed", "failed"]
    cells: tuple[InfrastructureCell, ...]

    @model_validator(mode="after")
    def require_consistent_status(self) -> InfrastructureAttestation:
        passed = len(self.cells) == 8 and len({_cell_key(cell) for cell in self.cells}) == 8
        passed = passed and all(cell.prepared for cell in self.cells)
        if (self.status == "passed") != passed:
            raise ValueError("infrastructure attestation status is inconsistent")
        return self


class HarnessAttestation(StrictModel):
    status: Literal["passed", "failed"]
    cells: tuple[HarnessCell, ...]

    @model_validator(mode="after")
    def require_consistent_status(self) -> HarnessAttestation:
        passed = len(self.cells) == 8 and len({_cell_key(cell) for cell in self.cells}) == 8
        passed = passed and all(
            cell.plan_called
            and cell.plan_usable
            and cell.complete_entity_planning
            and cell.final_sql_validated
            and cell.validation_passed
            and cell.mcp_grounded
            and cell.protocol_compliant is not False
            and not cell.tool_error
            for cell in self.cells
        )
        if (self.status == "passed") != passed:
            raise ValueError("harness attestation status is inconsistent")
        return self


class ScoringAttestation(StrictModel):
    status: Literal["passed", "failed"]
    cells: tuple[ScoringCell, ...]

    @model_validator(mode="after")
    def require_consistent_status(self) -> ScoringAttestation:
        required = {"formal_join_scorer", "formal_execution_scorer"}
        passed = len(self.cells) == 8 and len({_cell_key(cell) for cell in self.cells}) == 8
        passed = passed and all(
            cell.scoring_eligible
            and required <= set(cell.scorer_artifacts)
            and cell.output_parseable
            for cell in self.cells
        )
        if (self.status == "passed") != passed:
            raise ValueError("scoring attestation status is inconsistent")
        return self


class PilotCalibrationReport(StrictModel):
    schema_version: Literal[1, 2, 3, 4, 5] = 5
    status: Literal["passed", "failed"]
    readiness_status: Literal["passed", "failed"] | None = None
    calibration_task_ids: tuple[str, str]
    resource_contract: CalibrationResourceContract
    infrastructure: InfrastructureAttestation
    resource: ResourceAttestation | None = None
    harness: HarnessAttestation
    scoring: ScoringAttestation
    actual_model_cost_cny: float
    modal_compute_upper_cny: float
    modal_image_build_reserve_cny: float
    total_cost_upper_cny: float
    approved_run_budget_cny: Literal[4.0] = 4.0
    campaign_spend_before_cny: float = Field(ge=0)
    campaign_budget_cny: float = Field(gt=0)
    campaign_total_upper_cny: float
    workflow_run_id: int = Field(gt=0)
    joinlint_commit: str
    input_lock_sha256: str
    dependency_versions: dict[str, str]

    @model_validator(mode="after")
    def require_consistent_status_and_budget(self) -> PilotCalibrationReport:
        within_budget = (
            self.total_cost_upper_cny <= self.approved_run_budget_cny
            and self.campaign_total_upper_cny <= self.campaign_budget_cny
        )
        legacy_passed = all(
            attestation.status == "passed"
            for attestation in (self.infrastructure, self.harness, self.scoring)
        )
        legacy_passed = legacy_passed and within_budget
        if self.schema_version == 1:
            if (self.status == "passed") != legacy_passed:
                raise ValueError("pilot calibration report status is inconsistent")
            if any(
                value is not None
                for value in (self.readiness_status, self.resource)
            ):
                raise ValueError("schema v1 cannot contain resource readiness fields")
            return self
        if self.resource is None:
            raise ValueError("schema v2+ requires a resource attestation")
        if (
            self.schema_version >= 4
            and self.resource_contract.token_accounting_ceiling_by_host
            != CALIBRATION_TOKEN_ACCOUNTING_CEILINGS
        ):
            raise ValueError("schema v4 requires the frozen accounting ceilings")
        readiness = (
            calibration_readiness_status(
                infrastructure=self.infrastructure,
                resource=self.resource,
                scoring=self.scoring,
                within_budget=within_budget,
            )
            if self.schema_version == 2
            else calibration_authorization_status(
                infrastructure=self.infrastructure,
                resource=self.resource,
                scoring=self.scoring,
                within_budget=within_budget,
                accounting_ceiling_by_host=(
                    CALIBRATION_TOKEN_LIMITS
                    if self.schema_version == 3
                    else CALIBRATION_TOKEN_ACCOUNTING_CEILINGS
                ),
            )
        )
        if self.readiness_status != readiness:
            raise ValueError("pilot calibration readiness status is inconsistent")
        expected_passed = legacy_passed if self.schema_version == 2 else readiness == "passed"
        if (self.status == "passed") != expected_passed:
            raise ValueError("pilot calibration report status is inconsistent")
        if self.schema_version == 5 and any(
            cell.protocol_compliant is None for cell in self.harness.cells
        ):
            raise ValueError("schema v5 requires protocol compliance evidence")
        return self


def calibration_readiness_status(
    *,
    infrastructure: InfrastructureAttestation,
    resource: ResourceAttestation,
    scoring: ScoringAttestation,
    within_budget: bool,
) -> Literal["passed", "failed"]:
    passed = (
        infrastructure.status == "passed"
        and resource.status == "passed"
        and scoring_pipeline_available(scoring)
        and within_budget
    )
    return "passed" if passed else "failed"


def calibration_authorization_status(
    *,
    infrastructure: InfrastructureAttestation,
    resource: ResourceAttestation,
    scoring: ScoringAttestation,
    within_budget: bool,
    accounting_ceiling_by_host: dict[Host, int] = CALIBRATION_TOKEN_ACCOUNTING_CEILINGS,
) -> Literal["passed", "failed"]:
    passed = (
        infrastructure.status == "passed"
        and resource_pipeline_available(
            resource,
            accounting_ceiling_by_host=accounting_ceiling_by_host,
        )
        and scoring_pipeline_available(scoring)
        and within_budget
    )
    return "passed" if passed else "failed"


def resource_pipeline_available(
    resource: ResourceAttestation,
    *,
    accounting_ceiling_by_host: dict[Host, int],
) -> bool:
    return (
        len(resource.cells) == 8
        and len({_cell_key(cell) for cell in resource.cells}) == 8
        and all(
            cell.usage is not None
            and cell.observed_weighted_tokens is not None
            and cell.observed_weighted_tokens <= accounting_ceiling_by_host[cell.host]
            for cell in resource.cells
        )
    )


def scoring_pipeline_available(scoring: ScoringAttestation) -> bool:
    required = {"formal_join_scorer", "formal_execution_scorer"}
    return (
        len(scoring.cells) == 8
        and len({_cell_key(cell) for cell in scoring.cells}) == 8
        and all(required <= set(cell.scorer_artifacts) for cell in scoring.cells)
    )


def usage_breakdown(usage: Any, pricing: Any) -> UsageBreakdown:
    uncached_input = int(usage.input_tokens or 0)
    cache_read = int(usage.input_tokens_cache_read or 0)
    cache_write = int(usage.input_tokens_cache_write or 0)
    output = int(usage.output_tokens or 0)
    context = uncached_input + cache_read + cache_write
    return UsageBreakdown(
        uncached_input_tokens=uncached_input,
        cache_read_input_tokens=cache_read,
        cache_write_input_tokens=cache_write,
        output_tokens=output,
        context_input_tokens=context,
        inspect_weighted_tokens=context * 0.5 + output,
        calculated_cost_cny=model_usage_cost_cny(usage, pricing),
        cache_read_ratio=cache_read / context if context else 0.0,
    )


def calibration_budget_envelope(
    registration: PilotRegistration,
) -> CalibrationBudgetEnvelope:
    run_count = 2 * len(registration.models) * len(registration.hosts)
    if run_count != 8:
        raise ValueError("pilot calibration requires the frozen eight-run matrix")
    model_upper = 0.0
    for model in registration.models:
        weighted_rate = max(
            model.pricing_cny.input_cache_hit_per_million_cny / 0.5,
            model.pricing_cny.input_cache_miss_per_million_cny / 0.5,
            model.pricing_cny.output_per_million_cny,
        )
        model_upper += sum(
            2
            * CALIBRATION_TOKEN_ACCOUNTING_CEILINGS[host]
            * weighted_rate
            / 1_000_000
            for host in registration.hosts
        )
    modal_usd = run_count * registration.modal_sandbox_timeout_seconds * (
        registration.cpu_cores * MODAL_CPU_USD_PER_CORE_SECOND
        + (registration.memory_mib / 1024) * MODAL_MEMORY_USD_PER_GIB_SECOND
    )
    modal_cny = modal_usd * registration.usd_to_cny_upper
    total = model_upper + modal_cny + registration.modal_image_build_reserve_cny
    return CalibrationBudgetEnvelope(
        model_cost_upper_cny=model_upper,
        modal_compute_upper_cny=modal_cny,
        modal_image_build_reserve_cny=registration.modal_image_build_reserve_cny,
        total_upper_cny=total,
    )


def build_calibration_commands(
    *,
    inspect: str,
    registration: PilotRegistration,
    root: Path,
    log_dir: Path,
    lineage_id: str,
) -> tuple[list[str], ...]:
    _, manifest, _ = verify_pilot_inputs(root)
    specification = load_pilot_calibration_spec(root, manifest)
    task_ids = ",".join(specification.task_ids)
    commands: list[list[str]] = []
    for original in build_pilot_commands(
        inspect=inspect,
        registration=registration,
        root=root,
        log_dir=log_dir,
        lineage_id=lineage_id,
    ):
        if "condition=treatment" not in original:
            continue
        command = list(original)
        partition_index = next(
            index for index, value in enumerate(command) if value.startswith("task_partition=")
        )
        command[partition_index] = f"task_ids={task_ids}"
        host = next(
            value.removeprefix("host=") for value in command if value.startswith("host=")
        )
        token_index = command.index(f"token_limit={registration.token_limit_per_run}")
        command[token_index] = f"token_limit={CALIBRATION_TOKEN_LIMITS[host]}"
        commands.append(command)
    if len(commands) != 4:
        raise ValueError("pilot calibration does not cover all four model/host cells")
    return tuple(commands)


def run_calibration(
    root: Path,
    log_dir: Path,
    output: Path,
    *,
    workflow_run_id: int,
    campaign_budget_cny: float,
    campaign_spend_before_cny: float,
) -> PilotCalibrationReport:
    registration, manifest, run_plan = verify_pilot_inputs(root)
    specification = load_pilot_calibration_spec(root, manifest)
    envelope = calibration_budget_envelope(registration)
    if envelope.total_upper_cny > CALIBRATION_BUDGET_CNY:
        raise ValueError("pilot calibration upper-bound cost exceeds its approved run budget")
    if campaign_spend_before_cny + CALIBRATION_BUDGET_CNY > campaign_budget_cny:
        raise ValueError("pilot calibration could exceed the approved campaign budget")
    inspect = shutil.which("inspect")
    if inspect is None:
        raise ValueError("Inspect CLI is unavailable")
    commands = build_calibration_commands(
        inspect=inspect,
        registration=registration,
        root=root,
        log_dir=log_dir,
        lineage_id=run_plan.lineage_id,
    )
    output.mkdir(parents=True, exist_ok=True)
    for command in commands:
        batch_log_dir = Path(command[command.index("--log-dir") + 1])
        batch_log_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(command, check=True, env=inspect_subprocess_environment())
        require_batch_health(batch_log_dir, expected_sample_count=2)
    actual_model_cost = observed_model_cost_cny(log_dir, registration)
    total_upper = (
        actual_model_cost
        + envelope.modal_compute_upper_cny
        + envelope.modal_image_build_reserve_cny
    )
    infrastructure, resource, harness, scoring = verify_calibration_logs(
        log_dir,
        registration=registration,
        task_ids=specification.task_ids,
    )
    campaign_total = campaign_spend_before_cny + total_upper
    readiness_status = calibration_authorization_status(
        infrastructure=infrastructure,
        resource=resource,
        scoring=scoring,
        within_budget=(
            total_upper <= CALIBRATION_BUDGET_CNY
            and campaign_total <= campaign_budget_cny
        ),
    )
    report = PilotCalibrationReport(
        status="passed" if readiness_status == "passed" else "failed",
        readiness_status=readiness_status,
        calibration_task_ids=specification.task_ids,
        resource_contract=CalibrationResourceContract(
            token_limit_by_host=CALIBRATION_TOKEN_LIMITS,
            token_accounting_ceiling_by_host=CALIBRATION_TOKEN_ACCOUNTING_CEILINGS,
            token_limit_type=registration.token_limit_type,
            message_limit=registration.message_limit_per_run,
            evaluation_timeout_seconds=registration.time_limit_seconds,
            sandbox_timeout_seconds=registration.modal_sandbox_timeout_seconds,
            cpu_cores=registration.cpu_cores,
            memory_mib=registration.memory_mib,
        ),
        infrastructure=infrastructure,
        resource=resource,
        harness=harness,
        scoring=scoring,
        actual_model_cost_cny=actual_model_cost,
        modal_compute_upper_cny=envelope.modal_compute_upper_cny,
        modal_image_build_reserve_cny=envelope.modal_image_build_reserve_cny,
        total_cost_upper_cny=total_upper,
        campaign_spend_before_cny=campaign_spend_before_cny,
        campaign_budget_cny=campaign_budget_cny,
        campaign_total_upper_cny=campaign_total,
        workflow_run_id=workflow_run_id,
        joinlint_commit=registration.joinlint_commit,
        input_lock_sha256=_input_lock_sha256(root),
        dependency_versions=dependency_versions(),
    )
    (output / "calibration.json").write_bytes(
        canonical_json(report.model_dump(mode="json")) + b"\n"
    )
    return report


def verify_calibration_logs(
    log_dir: Path,
    *,
    registration: PilotRegistration,
    task_ids: tuple[str, str],
) -> tuple[
    InfrastructureAttestation,
    ResourceAttestation,
    HarnessAttestation,
    ScoringAttestation,
]:
    from inspect_ai.log import list_eval_logs, read_eval_log

    samples = [
        sample
        for info in list_eval_logs(str(log_dir), recursive=True)
        for sample in (read_eval_log(info.name, header_only=False).samples or [])
    ]
    return attest_calibration_samples(
        samples,
        registration=registration,
        task_ids=task_ids,
    )


def attest_calibration_samples(
    samples: list[Any],
    *,
    registration: PilotRegistration,
    task_ids: tuple[str, str],
) -> tuple[
    InfrastructureAttestation,
    ResourceAttestation,
    HarnessAttestation,
    ScoringAttestation,
]:
    expected_models = {model.returned_id for model in registration.models}
    infrastructure_cells: list[InfrastructureCell] = []
    resource_cells: list[ResourceCell] = []
    harness_cells: list[HarnessCell] = []
    scoring_cells: list[ScoringCell] = []
    observed_keys: set[tuple[str, str, str]] = set()
    for sample in samples:
        metadata = sample.metadata if isinstance(sample.metadata, dict) else {}
        host = str(metadata.get("host", ""))
        task_id = str(metadata.get("task_id", ""))
        usage_models = set(sample.model_usage)
        model_id = next(iter(usage_models)) if len(usage_models) == 1 else ""
        key = (model_id, host, task_id)
        if key in observed_keys:
            raise RuntimeError("pilot calibration contains a duplicate cell")
        observed_keys.add(key)
        lifecycle_value = (getattr(sample, "store", None) or {}).get(LIFECYCLE_STORE_KEY)
        try:
            lifecycle = parse_lifecycle(lifecycle_value)
            prepared = (
                lifecycle.infrastructure_status == "ready"
                and lifecycle.host_binary_sha256 is not None
            )
            scoring_eligible = lifecycle.scoring_eligible
            digest = lifecycle.host_binary_sha256
            lifecycle_reason = lifecycle.failure_reason
        except (TypeError, ValueError):
            prepared = False
            scoring_eligible = False
            digest = None
            lifecycle_reason = None
        scores = sample.scores or {}
        scorer_artifacts = tuple(sorted(scores))
        join_score = scores.get("formal_join_scorer")
        execution_score = scores.get("formal_execution_scorer")
        join_metadata = (
            join_score.metadata
            if join_score is not None and isinstance(join_score.metadata, dict)
            else {}
        )
        execution_metadata = (
            execution_score.metadata
            if execution_score is not None and isinstance(execution_score.metadata, dict)
            else {}
        )
        trace = join_metadata.get("trace")
        trace = trace if isinstance(trace, dict) else {}
        output_parseable = (
            join_metadata.get("failure_code") != "SQL_PARSE_FAILED"
            and execution_metadata.get("error_code") != "SQL_PARSE_FAILED"
        )
        infrastructure_cells.append(
            InfrastructureCell(
                model_id=model_id,
                host=host,  # type: ignore[arg-type]
                task_id=task_id,
                prepared=prepared,
                host_binary_sha256=digest,
            )
        )
        usage = sample.model_usage.get(model_id) if model_id else None
        model = next(
            (candidate for candidate in registration.models if candidate.returned_id == model_id),
            None,
        )
        breakdown = (
            usage_breakdown(usage, model.pricing_cny)
            if usage is not None and model is not None
            else None
        )
        configured_limit = CALIBRATION_TOKEN_LIMITS.get(host, 1)  # type: ignore[arg-type]
        observed_weighted = (
            breakdown.inspect_weighted_tokens if breakdown is not None else None
        )
        headroom = (
            configured_limit - observed_weighted
            if observed_weighted is not None
            else None
        )
        model_limit_reached = lifecycle_reason == LifecycleFailureReason.MODEL_LIMIT
        time_limit_reached = lifecycle_reason == LifecycleFailureReason.MODEL_TIMEOUT
        resource_cells.append(
            ResourceCell(
                model_id=model_id,
                host=host,  # type: ignore[arg-type]
                task_id=task_id,
                configured_token_limit=configured_limit,
                observed_weighted_tokens=observed_weighted,
                headroom_tokens=headroom,
                lifecycle_reason=lifecycle_reason,
                model_limit_reached=model_limit_reached,
                time_limit_reached=time_limit_reached,
                resource_sufficient=(
                    breakdown is not None
                    and observed_weighted is not None
                    and observed_weighted <= configured_limit
                    and not model_limit_reached
                    and not time_limit_reached
                ),
                usage=breakdown,
            )
        )
        harness_cells.append(
            HarnessCell(
                model_id=model_id,
                host=host,  # type: ignore[arg-type]
                task_id=task_id,
                plan_called=bool(trace.get("plan_called")),
                plan_usable=bool(trace.get("plan_usable")),
                complete_entity_planning=bool(trace.get("complete_entity_planning")),
                final_sql_validated=bool(trace.get("final_sql_validated")),
                validation_passed=bool(trace.get("validation_passed")),
                mcp_grounded=bool(trace.get("mcp_grounded")),
                protocol_compliant=bool(trace.get("protocol_compliant")),
                protocol_violation=(
                    trace.get("protocol_violation")
                    if isinstance(trace.get("protocol_violation"), str)
                    else None
                ),
                tool_error=bool(trace.get("tool_error", True)),
            )
        )
        scoring_cells.append(
            ScoringCell(
                model_id=model_id,
                host=host,  # type: ignore[arg-type]
                task_id=task_id,
                scoring_eligible=scoring_eligible,
                scorer_artifacts=scorer_artifacts,
                output_parseable=output_parseable,
            )
        )
    expected_keys = {
        (model_id, host, task_id)
        for model_id in expected_models
        for host in registration.hosts
        for task_id in task_ids
    }
    complete = observed_keys == expected_keys
    infrastructure_cells.sort(key=_cell_key)
    resource_cells.sort(key=_cell_key)
    harness_cells.sort(key=_cell_key)
    scoring_cells.sort(key=_cell_key)
    infrastructure_passed = complete and all(cell.prepared for cell in infrastructure_cells)
    resource_passed = complete and all(cell.resource_sufficient for cell in resource_cells)
    harness_passed = complete and all(
        cell.plan_called
        and cell.plan_usable
        and cell.complete_entity_planning
        and cell.final_sql_validated
        and cell.validation_passed
        and cell.mcp_grounded
        and cell.protocol_compliant
        and not cell.tool_error
        for cell in harness_cells
    )
    required_scorers = {"formal_join_scorer", "formal_execution_scorer"}
    scoring_passed = complete and all(
        cell.scoring_eligible
        and required_scorers <= set(cell.scorer_artifacts)
        and cell.output_parseable
        for cell in scoring_cells
    )
    return (
        InfrastructureAttestation(
            status="passed" if infrastructure_passed else "failed",
            cells=tuple(infrastructure_cells),
        ),
        ResourceAttestation(
            status="passed" if resource_passed else "failed",
            cells=tuple(resource_cells),
            hosts=_resource_host_summaries(resource_cells, registration.hosts),
        ),
        HarnessAttestation(
            status="passed" if harness_passed else "failed",
            cells=tuple(harness_cells),
        ),
        ScoringAttestation(
            status="passed" if scoring_passed else "failed",
            cells=tuple(scoring_cells),
        ),
    )


def verify_calibration_attestation_values(
    report: PilotCalibrationReport,
    *,
    expected_run_id: int,
    current_commit: str,
    input_lock_sha256: str,
    dependency_versions: dict[str, str],
    run_metadata: dict[str, Any],
    repository: str,
) -> None:
    if report.status != "passed" or report.readiness_status != "passed":
        raise ValueError("pilot calibration readiness did not pass")
    if report.workflow_run_id != expected_run_id or run_metadata.get("id") != expected_run_id:
        raise ValueError("calibration attestation run ID mismatch")
    if run_metadata.get("name") != "formal-pilot-canary" or run_metadata.get(
        "path"
    ) != ".github/workflows/formal-pilot-canary.yml":
        raise ValueError("calibration attestation came from an unexpected workflow")
    if run_metadata.get("event") != "workflow_dispatch" or run_metadata.get(
        "conclusion"
    ) != "success":
        raise ValueError("calibration workflow did not complete successfully")
    if (run_metadata.get("head_repository") or {}).get("full_name") != repository:
        raise ValueError("calibration attestation repository mismatch")
    if report.joinlint_commit != current_commit or run_metadata.get("head_sha") != current_commit:
        raise ValueError("calibration attestation commit mismatch")
    if report.input_lock_sha256 != input_lock_sha256:
        raise ValueError("calibration attestation input lock mismatch")
    if report.dependency_versions != dependency_versions:
        raise ValueError("calibration attestation dependency versions mismatch")


def verify_calibration_attestation(
    root: Path,
    attestation_path: Path,
    run_metadata_path: Path,
    *,
    expected_run_id: int,
    current_commit: str,
    repository: str,
) -> None:
    registration, manifest, _ = verify_pilot_inputs(root)
    if registration.joinlint_commit != current_commit:
        raise ValueError("checked-out JoinLint commit does not match the pilot registration")
    report = PilotCalibrationReport.model_validate_json(attestation_path.read_bytes())
    if report.schema_version != 5:
        raise ValueError("pilot calibration attestation schema is not current")
    specification = load_pilot_calibration_spec(root, manifest)
    if report.calibration_task_ids != specification.task_ids:
        raise ValueError("calibration attestation task IDs mismatch")
    formal_limits = {
        host: registration.token_limit_per_run for host in registration.hosts
    }
    if report.resource_contract.token_limit_by_host != formal_limits:
        raise ValueError("calibration resource contract does not match the formal Pilot")
    formal_accounting_ceilings = {
        host: registration.token_accounting_ceiling_per_run
        for host in registration.hosts
    }
    if (
        report.resource_contract.token_accounting_ceiling_by_host
        != formal_accounting_ceilings
    ):
        raise ValueError("calibration accounting contract does not match the formal Pilot")
    expected_keys = {
        (model.returned_id, host, task_id)
        for model in registration.models
        for host in registration.hosts
        for task_id in specification.task_ids
    }
    for cells in (
        report.infrastructure.cells,
        report.harness.cells,
        report.scoring.cells,
    ):
        if {(cell.model_id, cell.host, cell.task_id) for cell in cells} != expected_keys:
            raise ValueError("calibration attestation matrix mismatch")
    if report.schema_version >= 2:
        if report.resource is None:
            raise ValueError("calibration resource readiness is missing")
        if {
            (cell.model_id, cell.host, cell.task_id) for cell in report.resource.cells
        } != expected_keys:
            raise ValueError("calibration resource readiness matrix mismatch")
    run_metadata = json.loads(run_metadata_path.read_bytes())
    if not isinstance(run_metadata, dict):
        raise ValueError("calibration run metadata must be an object")
    verify_calibration_attestation_values(
        report,
        expected_run_id=expected_run_id,
        current_commit=current_commit,
        input_lock_sha256=_input_lock_sha256(root),
        dependency_versions=dependency_versions(),
        run_metadata=run_metadata,
        repository=repository,
    )


def dependency_versions() -> dict[str, str]:
    return {name: version(name) for name in REMOTE_DEPENDENCIES}


def _input_lock_sha256(root: Path) -> str:
    lock = load_document(root / "input-lock.json", InputLockV2)
    return digest_value(lock.model_dump(mode="json"))


def _resource_host_summaries(
    cells: list[ResourceCell],
    hosts: tuple[Host, ...],
) -> tuple[ResourceHostSummary, ...]:
    summaries: list[ResourceHostSummary] = []
    for host in sorted(set(hosts)):
        selected = [cell for cell in cells if cell.host == host]
        observed = [cell for cell in selected if cell.usage is not None]
        weighted = [cell.observed_weighted_tokens for cell in observed]
        headroom = [cell.headroom_tokens for cell in observed]
        cache_read = [cell.usage.cache_read_input_tokens for cell in observed if cell.usage]
        peak = max(value for value in weighted if value is not None) if weighted else None
        minimum_headroom = (
            min(value for value in headroom if value is not None) if headroom else None
        )
        target_headroom = (
            math.ceil((peak * TARGET_HEADROOM_RATIO) / 1_000) * 1_000
            if peak is not None
            else None
        )
        summaries.append(
            ResourceHostSummary(
                host=host,
                cell_count=len(selected),
                configured_token_limit=CALIBRATION_TOKEN_LIMITS[host],
                peak_observed_weighted_tokens=peak,
                minimum_headroom_tokens=minimum_headroom,
                observed_cache_read_floor_tokens=min(cache_read) if cache_read else None,
                target_headroom_tokens=target_headroom,
                limit_censored=any(
                    cell.usage is None
                    or cell.model_limit_reached
                    or cell.time_limit_reached
                    for cell in selected
                ),
            )
        )
    return tuple(summaries)


def _cell_key(
    cell: InfrastructureCell | ResourceCell | HarnessCell | ScoringCell,
) -> tuple[bytes, bytes, bytes]:
    return (
        cell.model_id.encode("utf-8"),
        cell.host.encode("utf-8"),
        cell.task_id.encode("utf-8"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run and verify the formal Pilot calibration")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--root", type=Path, required=True)
    run.add_argument("--log-dir", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--workflow-run-id", type=int, required=True)
    run.add_argument("--campaign-budget-cny", type=float, required=True)
    run.add_argument("--campaign-spend-before-cny", type=float, required=True)
    verify = commands.add_parser("verify-attestation")
    verify.add_argument("--root", type=Path, required=True)
    verify.add_argument("--attestation", type=Path, required=True)
    verify.add_argument("--run-metadata", type=Path, required=True)
    verify.add_argument("--expected-run-id", type=int, required=True)
    verify.add_argument("--current-commit", required=True)
    verify.add_argument("--repository", required=True)
    arguments = parser.parse_args(argv)
    if arguments.command == "run":
        report = run_calibration(
            arguments.root,
            arguments.log_dir,
            arguments.output,
            workflow_run_id=arguments.workflow_run_id,
            campaign_budget_cny=arguments.campaign_budget_cny,
            campaign_spend_before_cny=arguments.campaign_spend_before_cny,
        )
        print(report.model_dump_json())
        return 0 if report.status == "passed" else 2
    verify_calibration_attestation(
        arguments.root,
        arguments.attestation,
        arguments.run_metadata,
        expected_run_id=arguments.expected_run_id,
        current_commit=arguments.current_commit,
        repository=arguments.repository,
    )
    print(json.dumps({"status": "ready", "calibration_run_id": arguments.expected_run_id}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
