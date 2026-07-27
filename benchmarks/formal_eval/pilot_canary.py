from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal

from benchmarks.formal_eval.contracts import StrictModel
from benchmarks.formal_eval.dispatch import inspect_subprocess_environment
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


def canary_budget_envelope(registration: PilotRegistration) -> PilotCanaryBudget:
    model = _canary_model(registration)
    rate = max(
        model.pricing_cny.input_cache_hit_per_million_cny,
        model.pricing_cny.input_cache_miss_per_million_cny,
        model.pricing_cny.output_per_million_cny,
    )
    model_upper = registration.token_limit_per_run * rate / 1_000_000
    modal_usd = registration.modal_sandbox_timeout_seconds * (
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
    return command


def run_canary(root: Path, log_dir: Path, output: Path) -> PilotCanaryReport:
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
    if log_model_id != expected_model_id:
        raise RuntimeError("pilot canary model identity does not match registration")
    usage_models = set(sample.model_usage)
    if usage_models != {expected_model_id}:
        raise RuntimeError("pilot canary provider model identity is missing or unexpected")
    return expected_model_id, tuple(sorted(scorer_artifacts))


def _canary_model(registration: PilotRegistration):  # type: ignore[no-untyped-def]
    models = [model for model in registration.models if model.tier == "high_capability"]
    if len(models) != 1:
        raise ValueError("pilot canary requires one high-capability model")
    return models[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one frozen Modal pilot canary sample")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    report = run_canary(arguments.root, arguments.log_dir, arguments.output)
    print(report.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
