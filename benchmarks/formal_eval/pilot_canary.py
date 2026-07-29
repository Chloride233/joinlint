from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from benchmarks.formal_eval.contracts import InputLockV2, StrictModel
from benchmarks.formal_eval.dispatch import inspect_subprocess_environment
from benchmarks.formal_eval.lineage import digest_value
from benchmarks.formal_eval.manifest import load_document
from benchmarks.formal_eval.pilot import (
    MODAL_CPU_USD_PER_CORE_SECOND,
    MODAL_MEMORY_USD_PER_GIB_SECOND,
    PilotRegistration,
    verify_pilot_inputs,
)
from benchmarks.formal_eval.pilot_dispatch import (
    build_pilot_commands,
    observed_model_cost_cny,
    require_batch_health,
)
from joinlint.contracts import canonical_json


CANARY_BUDGET_CNY = 2.25
CANARY_SANDBOX_TIMEOUT_SECONDS = 150
REMOTE_DEPENDENCIES = ("inspect-ai", "inspect-sandboxes", "inspect-swe", "modal")


class PilotCanaryBudget(StrictModel):
    run_count: Literal[1] = 1
    model_cost_upper_cny: float
    modal_compute_upper_cny: float
    modal_image_build_reserve_cny: float
    total_upper_cny: float


class PilotCanaryReport(StrictModel):
    schema_version: Literal[1] = 1
    status: Literal["passed"] = "passed"
    model_id: str
    host: Literal["codex"] = "codex"
    condition: Literal["treatment"] = "treatment"
    sample_count: Literal[1] = 1
    scorer_artifacts: tuple[str, ...]
    actual_model_cost_cny: float
    modal_compute_upper_cny: float
    modal_image_build_reserve_cny: float
    total_cost_upper_cny: float
    approved_budget_cny: Literal[2.25] = 2.25
    workflow_run_id: int = Field(gt=0)
    joinlint_commit: str
    input_lock_sha256: str
    dependency_versions: dict[str, str]


def canary_budget_envelope(registration: PilotRegistration) -> PilotCanaryBudget:
    model = _canary_model(registration)
    rate = max(
        model.pricing_cny.input_cache_hit_per_million_cny,
        model.pricing_cny.input_cache_miss_per_million_cny,
        model.pricing_cny.output_per_million_cny,
    )
    model_upper = registration.token_limit_per_run * rate / 1_000_000
    modal_usd = CANARY_SANDBOX_TIMEOUT_SECONDS * (
        registration.cpu_cores * MODAL_CPU_USD_PER_CORE_SECOND
        + (registration.memory_mib / 1024) * MODAL_MEMORY_USD_PER_GIB_SECOND
    )
    modal_cny = modal_usd * registration.usd_to_cny_upper
    total = model_upper + modal_cny + registration.modal_image_build_reserve_cny
    return PilotCanaryBudget(
        model_cost_upper_cny=model_upper,
        modal_compute_upper_cny=modal_cny,
        modal_image_build_reserve_cny=registration.modal_image_build_reserve_cny,
        total_upper_cny=total,
    )


def build_canary_command(
    *,
    inspect: str,
    registration: PilotRegistration,
    root: Path,
    log_dir: Path,
    lineage_id: str,
) -> list[str]:
    model = _canary_model(registration)
    matches = [
        command
        for command in build_pilot_commands(
            inspect=inspect,
            registration=registration,
            root=root,
            log_dir=log_dir,
            lineage_id=lineage_id,
        )
        if command[command.index("--model") + 1] == model.id
        and "host=codex" in command
        and "condition=treatment" in command
    ]
    if len(matches) != 1:
        raise ValueError("pilot canary command is not uniquely defined")
    command = list(matches[0])
    command[3:3] = ["--limit", "1"]
    sandbox_timeout_index = command.index(
        f"sandbox_timeout={registration.modal_sandbox_timeout_seconds}"
    )
    command[sandbox_timeout_index] = f"sandbox_timeout={CANARY_SANDBOX_TIMEOUT_SECONDS}"
    return command


def run_canary(
    root: Path,
    log_dir: Path,
    output: Path,
    *,
    workflow_run_id: int,
) -> PilotCanaryReport:
    registration, _, run_plan = verify_pilot_inputs(root)
    envelope = canary_budget_envelope(registration)
    if envelope.total_upper_cny > CANARY_BUDGET_CNY:
        raise ValueError("pilot canary upper-bound cost exceeds the approved budget")
    inspect = shutil.which("inspect")
    if inspect is None:
        raise ValueError("Inspect CLI is unavailable")
    command = build_canary_command(
        inspect=inspect,
        registration=registration,
        root=root,
        log_dir=log_dir,
        lineage_id=run_plan.lineage_id,
    )
    Path(command[command.index("--log-dir") + 1]).mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run(command, check=True, env=inspect_subprocess_environment())
    require_batch_health(log_dir, expected_sample_count=1)
    model_id, scorer_artifacts = _verify_canary_log(log_dir, registration)
    actual_model_cost = observed_model_cost_cny(log_dir, registration)
    total_upper = (
        actual_model_cost
        + envelope.modal_compute_upper_cny
        + envelope.modal_image_build_reserve_cny
    )
    if total_upper > CANARY_BUDGET_CNY:
        raise RuntimeError("pilot canary reached the approved cost ceiling")
    report = PilotCanaryReport(
        model_id=model_id,
        scorer_artifacts=scorer_artifacts,
        actual_model_cost_cny=actual_model_cost,
        modal_compute_upper_cny=envelope.modal_compute_upper_cny,
        modal_image_build_reserve_cny=envelope.modal_image_build_reserve_cny,
        total_cost_upper_cny=total_upper,
        workflow_run_id=workflow_run_id,
        joinlint_commit=registration.joinlint_commit,
        input_lock_sha256=_input_lock_sha256(root),
        dependency_versions=dependency_versions(),
    )
    (output / "canary.json").write_bytes(
        canonical_json(report.model_dump(mode="json")) + b"\n"
    )
    return report


def _verify_canary_log(
    log_dir: Path,
    registration: PilotRegistration,
) -> tuple[str, tuple[str, ...]]:
    from inspect_ai.log import list_eval_logs, read_eval_log

    infos = list_eval_logs(str(log_dir), recursive=True)
    if len(infos) != 1:
        raise RuntimeError("pilot canary did not produce exactly one Inspect log")
    log = read_eval_log(infos[0].name, header_only=False)
    samples = log.samples or []
    return require_canary_artifacts(
        log_model_id=log.eval.model,
        samples=samples,
        expected_model_id=_canary_model(registration).returned_id,
    )


def require_canary_artifacts(
    *,
    log_model_id: str,
    samples: list[Any],
    expected_model_id: str,
) -> tuple[str, tuple[str, ...]]:
    if len(samples) != 1:
        raise RuntimeError("pilot canary did not produce exactly one sample")
    sample = samples[0]
    if sample.error is not None:
        raise RuntimeError("pilot canary sample has a runtime error")
    required_scorers = {"formal_join_scorer", "formal_execution_scorer"}
    scorer_artifacts = set(sample.scores or {})
    if not required_scorers.issubset(scorer_artifacts):
        raise RuntimeError("pilot canary scorer artifacts are incomplete")
    join_score = sample.scores["formal_join_scorer"]
    join_metadata = join_score.metadata if isinstance(join_score.metadata, dict) else {}
    if join_metadata.get("scoring_eligible") is not True:
        raise RuntimeError("pilot canary evaluation lifecycle was not scoring eligible")
    if log_model_id != expected_model_id:
        raise RuntimeError("pilot canary model identity does not match registration")
    usage_models = set(sample.model_usage)
    if usage_models != {expected_model_id}:
        raise RuntimeError("pilot canary provider model identity is missing or unexpected")
    return expected_model_id, tuple(sorted(scorer_artifacts))


def dependency_versions() -> dict[str, str]:
    return {name: version(name) for name in REMOTE_DEPENDENCIES}


def verify_canary_attestation_values(
    report: PilotCanaryReport,
    *,
    expected_run_id: int,
    current_commit: str,
    input_lock_sha256: str,
    dependency_versions: dict[str, str],
    run_metadata: dict[str, Any],
    repository: str,
) -> None:
    if report.workflow_run_id != expected_run_id or run_metadata.get("id") != expected_run_id:
        raise ValueError("canary attestation run ID mismatch")
    if run_metadata.get("name") != "formal-pilot-canary" or run_metadata.get("path") != (
        ".github/workflows/formal-pilot-canary.yml"
    ):
        raise ValueError("canary attestation came from an unexpected workflow")
    if run_metadata.get("event") != "workflow_dispatch" or run_metadata.get(
        "conclusion"
    ) != "success":
        raise ValueError("canary workflow did not complete successfully")
    head_repository = run_metadata.get("head_repository") or {}
    if head_repository.get("full_name") != repository:
        raise ValueError("canary attestation repository mismatch")
    if report.joinlint_commit != current_commit or run_metadata.get("head_sha") != current_commit:
        raise ValueError("canary attestation commit mismatch")
    if report.input_lock_sha256 != input_lock_sha256:
        raise ValueError("canary attestation input lock mismatch")
    if report.dependency_versions != dependency_versions:
        raise ValueError("canary attestation dependency versions mismatch")


def verify_canary_attestation(
    root: Path,
    attestation_path: Path,
    run_metadata_path: Path,
    *,
    expected_run_id: int,
    current_commit: str,
    repository: str,
) -> None:
    registration, _, _ = verify_pilot_inputs(root)
    if registration.joinlint_commit != current_commit:
        raise ValueError("checked-out JoinLint commit does not match the pilot registration")
    report = PilotCanaryReport.model_validate_json(attestation_path.read_bytes())
    run_metadata = json.loads(run_metadata_path.read_bytes())
    if not isinstance(run_metadata, dict):
        raise ValueError("canary run metadata must be an object")
    verify_canary_attestation_values(
        report,
        expected_run_id=expected_run_id,
        current_commit=current_commit,
        input_lock_sha256=_input_lock_sha256(root),
        dependency_versions=dependency_versions(),
        run_metadata=run_metadata,
        repository=repository,
    )


def _input_lock_sha256(root: Path) -> str:
    lock = load_document(root / "input-lock.json", InputLockV2)
    return digest_value(lock.model_dump(mode="json"))


def _canary_model(registration: PilotRegistration):  # type: ignore[no-untyped-def]
    models = [model for model in registration.models if model.tier == "high_capability"]
    if len(models) != 1:
        raise ValueError("pilot canary requires one high-capability model")
    return models[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run and verify the formal Pilot canary")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--root", type=Path, required=True)
    run.add_argument("--log-dir", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--workflow-run-id", type=int, required=True)
    verify = commands.add_parser("verify-attestation")
    verify.add_argument("--root", type=Path, required=True)
    verify.add_argument("--attestation", type=Path, required=True)
    verify.add_argument("--run-metadata", type=Path, required=True)
    verify.add_argument("--expected-run-id", type=int, required=True)
    verify.add_argument("--current-commit", required=True)
    verify.add_argument("--repository", required=True)
    arguments = parser.parse_args(argv)
    if arguments.command == "run":
        report = run_canary(
            arguments.root,
            arguments.log_dir,
            arguments.output,
            workflow_run_id=arguments.workflow_run_id,
        )
        print(report.model_dump_json())
        return 0
    verify_canary_attestation(
        arguments.root,
        arguments.attestation,
        arguments.run_metadata,
        expected_run_id=arguments.expected_run_id,
        current_commit=arguments.current_commit,
        repository=arguments.repository,
    )
    print(json.dumps({"status": "ready", "canary_run_id": arguments.expected_run_id}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
