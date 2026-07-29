from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path
from typing import Any

from benchmarks.formal_eval.export import export_agent_rows
from benchmarks.formal_eval.dispatch import REPOSITORY_ROOT, inspect_subprocess_environment
from benchmarks.formal_eval.pilot import (
    PilotBudgetCheckpoint,
    PilotRegistration,
    budget_envelope,
    pilot_budget_checkpoint,
    pilot_budget_report,
    verify_pilot_inputs,
)
from joinlint.contracts import canonical_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the frozen 20-task Modal pilot")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    registration, _, run_plan = verify_pilot_inputs(arguments.root)
    inspect = shutil.which("inspect")
    if inspect is None:
        raise ValueError("Inspect CLI is unavailable")
    commands = build_pilot_commands(
        inspect=inspect,
        registration=registration,
        root=arguments.root,
        log_dir=arguments.log_dir,
        lineage_id=run_plan.lineage_id,
    )
    envelope = budget_envelope(registration)
    if envelope.total_upper_cny > registration.budget_cny:
        raise ValueError("pilot upper-bound cost exceeds the approved budget")
    arguments.output.mkdir(parents=True, exist_ok=True)
    for batch_index, command in enumerate(commands):
        before = pilot_budget_checkpoint(
            registration,
            completed_batches=batch_index,
            actual_model_cost_cny=observed_model_cost_cny(arguments.log_dir, registration),
        )
        _write_checkpoint(arguments.output, before)
        if not before.safe_to_continue:
            raise RuntimeError("pilot stopped before the next batch could exceed the budget")
        batch_log_dir = Path(command[command.index("--log-dir") + 1])
        batch_log_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(command, check=True, env=inspect_subprocess_environment())
        require_batch_health(
            batch_log_dir,
            expected_sample_count=registration.task_count,
        )
        observed = observed_model_cost_cny(arguments.log_dir, registration)
        after = pilot_budget_checkpoint(
            registration,
            completed_batches=batch_index + 1,
            actual_model_cost_cny=observed,
        )
        _write_checkpoint(arguments.output, after)
        if not after.safe_to_continue:
            raise RuntimeError("pilot stopped because the approved cost ceiling was reached")

    bundle = export_agent_rows(
        arguments.log_dir,
        arguments.output / "agent-results-bundle.json",
        expected_model_ids={model.returned_id for model in registration.models},
        model_pricing={model.returned_id: model.pricing_cny for model in registration.models},
        lineage_id=run_plan.lineage_id,
        run_plan=run_plan,
        phase="all",
        summary_output=arguments.output / "cleaning.json",
    )
    rows_path = arguments.output / "pilot-agent-results.json"
    rows_path.write_bytes(
        canonical_json([row.model_dump(mode="json") for row in bundle.rows]) + b"\n"
    )
    budget = pilot_budget_report(registration, run_plan, bundle)
    (arguments.output / "budget.json").write_bytes(
        canonical_json(budget.model_dump(mode="json")) + b"\n"
    )
    return 0 if budget.passed else 2


def _write_checkpoint(output: Path, checkpoint: PilotBudgetCheckpoint) -> None:
    payload = checkpoint.model_dump(mode="json")
    (output / "budget-checkpoint.json").write_bytes(canonical_json(payload) + b"\n")


def require_batch_health(log_dir: Path, *, expected_sample_count: int) -> None:
    from inspect_ai.log import list_eval_logs, read_eval_log

    samples = [
        sample
        for info in list_eval_logs(str(log_dir), recursive=True)
        for sample in (read_eval_log(info.name, header_only=False).samples or [])
    ]
    require_sample_batch_health(samples, expected_sample_count=expected_sample_count)


def require_sample_batch_health(
    samples: list[Any],
    *,
    expected_sample_count: int,
) -> None:
    if len(samples) != expected_sample_count:
        raise RuntimeError("pilot batch produced an incomplete sample set")
    if all(sample.error is not None and not sample.scores for sample in samples):
        raise RuntimeError("pilot batch has a systemic infrastructure failure")


def build_pilot_commands(
    *,
    inspect: str,
    registration: PilotRegistration,
    root: Path,
    log_dir: Path,
    lineage_id: str,
) -> tuple[list[str], ...]:
    resolved_root = root.resolve()
    dockerfile = (REPOSITORY_ROOT / "Dockerfile.formal-pilot").resolve()
    commands: list[list[str]] = []
    for model in registration.models:
        for host in registration.hosts:
            for condition in ("control", "treatment"):
                commands.append(
                    [
                        inspect,
                        "eval",
                        "benchmarks/formal_eval/inspect_task.py@formal_pilot_eval",
                        "--model",
                        model.id,
                        "--log-dir",
                        str(log_dir / model.tier / host / condition),
                        "--epochs",
                        "1",
                        "--max-retries",
                        "0",
                        "--max-sandboxes",
                        str(registration.max_sandboxes),
                        "--no-fail-on-error",
                        "--score-on-error",
                        "--display",
                        "none",
                        "--seed",
                        str(registration.seed),
                        "-T",
                        f"sealed_tasks={resolved_root / 'agent-tasks.json'}",
                        "-T",
                        f"manifest={resolved_root / 'manifest.json'}",
                        "-T",
                        f"host={host}",
                        "-T",
                        f"condition={condition}",
                        "-T",
                        f"agent_version={registration.host_versions[host]}",
                        "-T",
                        f"dockerfile={dockerfile}",
                        "-T",
                        f"lineage_id={lineage_id}",
                        "-T",
                        f"token_limit={registration.token_limit_per_run}",
                        "-T",
                        f"time_limit={registration.time_limit_seconds}",
                        "-T",
                        f"sandbox_timeout={registration.modal_sandbox_timeout_seconds}",
                        "-T",
                        f"cpu={registration.cpu_cores}",
                        "-T",
                        f"memory_mib={registration.memory_mib}",
                    ]
                )
    return tuple(commands)


def observed_model_cost_cny(log_dir: Path, registration: PilotRegistration) -> float:
    from inspect_ai.log import list_eval_logs, read_eval_log

    pricing = {model.returned_id: model.pricing_cny for model in registration.models}
    total = 0.0
    for info in list_eval_logs(str(log_dir), recursive=True):
        log = read_eval_log(info.name, header_only=False)
        for sample in log.samples or []:
            for model_id, usage in sample.model_usage.items():
                model_pricing = pricing.get(model_id)
                if model_pricing is None:
                    raise ValueError(f"unexpected returned model identity: {model_id}")
                cache_read = usage.input_tokens_cache_read or 0
                cache_write = usage.input_tokens_cache_write or 0
                total += (
                    cache_read * model_pricing.input_cache_hit_per_million_cny
                    + (usage.input_tokens + cache_write)
                    * model_pricing.input_cache_miss_per_million_cny
                    + usage.output_tokens * model_pricing.output_per_million_cny
                ) / 1_000_000
    return total


if __name__ == "__main__":
    raise SystemExit(main())
