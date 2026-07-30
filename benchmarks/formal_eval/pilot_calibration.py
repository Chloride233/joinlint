from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from benchmarks.formal_eval.contracts import Host, InputLockV2, StrictModel
from benchmarks.formal_eval.dispatch import inspect_subprocess_environment
from benchmarks.formal_eval.lifecycle import LIFECYCLE_STORE_KEY, parse_lifecycle
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
    observed_model_cost_cny,
    require_batch_health,
)
from joinlint.contracts import canonical_json


CALIBRATION_BUDGET_CNY = 4.0
REMOTE_DEPENDENCIES = ("anthropic", "inspect-ai", "inspect-sandboxes", "inspect-swe", "modal")


class CalibrationBudgetEnvelope(StrictModel):
    run_count: Literal[8] = 8
    model_cost_upper_cny: float
    modal_compute_upper_cny: float
    modal_image_build_reserve_cny: float
    total_upper_cny: float


class CalibrationResourceContract(StrictModel):
    token_limit: Literal[35_000] = 35_000
    token_limit_type: Literal["(input*0.5)+output"] = "(input*0.5)+output"
    message_limit: Literal[12] = 12
    evaluation_timeout_seconds: Literal[90] = 90
    sandbox_timeout_seconds: Literal[150] = 150
    cpu_cores: Literal[0.5] = 0.5
    memory_mib: Literal[2048] = 2048


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
    schema_version: Literal[1] = 1
    status: Literal["passed", "failed"]
    calibration_task_ids: tuple[str, str]
    resource_contract: CalibrationResourceContract
    infrastructure: InfrastructureAttestation
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
        passed = all(
            attestation.status == "passed"
            for attestation in (self.infrastructure, self.harness, self.scoring)
        )
        passed = (
            passed
            and self.total_cost_upper_cny <= self.approved_run_budget_cny
            and self.campaign_total_upper_cny <= self.campaign_budget_cny
        )
        if (self.status == "passed") != passed:
            raise ValueError("pilot calibration report status is inconsistent")
        return self


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
        model_upper += (
            len(registration.hosts)
            * 2
            * registration.token_limit_per_run
            * weighted_rate
            / 1_000_000
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
    infrastructure, harness, scoring = verify_calibration_logs(
        log_dir,
        registration=registration,
        task_ids=specification.task_ids,
    )
    passed = all(
        attestation.status == "passed"
        for attestation in (infrastructure, harness, scoring)
    )
    campaign_total = campaign_spend_before_cny + total_upper
    if total_upper > CALIBRATION_BUDGET_CNY or campaign_total > campaign_budget_cny:
        passed = False
    report = PilotCalibrationReport(
        status="passed" if passed else "failed",
        calibration_task_ids=specification.task_ids,
        resource_contract=CalibrationResourceContract(
            token_limit=registration.token_limit_per_run,
            token_limit_type=registration.token_limit_type,
            message_limit=registration.message_limit_per_run,
            evaluation_timeout_seconds=registration.time_limit_seconds,
            sandbox_timeout_seconds=registration.modal_sandbox_timeout_seconds,
            cpu_cores=registration.cpu_cores,
            memory_mib=registration.memory_mib,
        ),
        infrastructure=infrastructure,
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
) -> tuple[InfrastructureAttestation, HarnessAttestation, ScoringAttestation]:
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
) -> tuple[InfrastructureAttestation, HarnessAttestation, ScoringAttestation]:
    expected_models = {model.returned_id for model in registration.models}
    infrastructure_cells: list[InfrastructureCell] = []
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
        except (TypeError, ValueError):
            prepared = False
            scoring_eligible = False
            digest = None
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
    harness_cells.sort(key=_cell_key)
    scoring_cells.sort(key=_cell_key)
    infrastructure_passed = complete and all(cell.prepared for cell in infrastructure_cells)
    harness_passed = complete and all(
        cell.plan_called
        and cell.plan_usable
        and cell.complete_entity_planning
        and cell.final_sql_validated
        and cell.validation_passed
        and cell.mcp_grounded
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
    if report.status != "passed":
        raise ValueError("pilot calibration did not pass all three attestations")
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
    specification = load_pilot_calibration_spec(root, manifest)
    if report.calibration_task_ids != specification.task_ids:
        raise ValueError("calibration attestation task IDs mismatch")
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


def _cell_key(cell: InfrastructureCell | HarnessCell | ScoringCell) -> tuple[bytes, bytes, bytes]:
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
