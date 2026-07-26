from __future__ import annotations

import math
from collections import defaultdict
from statistics import NormalDist, stdev

from pydantic import Field

from benchmarks.formal_eval.contracts import AgentResultRow, FormalManifestV2, StrictModel
from benchmarks.formal_eval.run_plan import RunPlanV2


class PowerPlan(StrictModel):
    lineage_id: str
    pilot_database_count: int
    database_effect_sd: float = Field(ge=0)
    target_effect: float
    target_power: float
    alpha: float
    required_database_count: int
    required_task_count: int
    minimum_formal_run_count: int


class PowerReadiness(StrictModel):
    required_database_count: int
    actual_database_count: int
    required_task_count: int
    actual_task_count: int
    required_run_count: int
    actual_run_count: int
    ready: bool


def power_plan_from_pilot(
    rows: list[AgentResultRow],
    *,
    lineage_id: str,
    target_effect: float = 0.10,
    target_power: float = 0.80,
    alpha: float = 0.05,
    minimum_databases: int = 12,
    tasks_per_database: int = 5,
    repetitions: int = 3,
    model_count: int = 2,
    host_count: int = 2,
    condition_count: int = 2,
) -> PowerPlan:
    if not 0.0 < target_effect <= 1.0:
        raise ValueError("target effect must be within (0, 1]")
    if not 0.0 < target_power < 1.0 or not 0.0 < alpha < 1.0:
        raise ValueError("power and alpha must be within (0, 1)")
    effects = _database_effects(rows)
    if len(effects) < 2:
        raise ValueError("power planning requires at least two pilot databases")
    database_sd = stdev(effects)
    if database_sd == 0.0:
        required = minimum_databases
    else:
        z_alpha = NormalDist().inv_cdf(1.0 - alpha / 2.0)
        z_power = NormalDist().inv_cdf(target_power)
        required = math.ceil(((z_alpha + z_power) * database_sd / target_effect) ** 2)
        required = max(minimum_databases, required)
    tasks = required * tasks_per_database
    runs = tasks * repetitions * model_count * host_count * condition_count
    return PowerPlan(
        lineage_id=lineage_id,
        pilot_database_count=len(effects),
        database_effect_sd=database_sd,
        target_effect=target_effect,
        target_power=target_power,
        alpha=alpha,
        required_database_count=required,
        required_task_count=tasks,
        minimum_formal_run_count=runs,
    )


def validate_power_readiness(
    plan: PowerPlan,
    manifest: FormalManifestV2,
    run_plan: RunPlanV2,
) -> PowerReadiness:
    if plan.lineage_id != run_plan.lineage_id:
        raise ValueError("power and run plans have different evaluation lineages")
    confirmatory = [
        task
        for task in manifest.tasks
        if task.corpus == "natural" and task.split == "confirmatory"
    ]
    database_count = len({task.database_id for task in confirmatory})
    run_count = sum(run.confirmatory for run in run_plan.runs)
    ready = (
        database_count >= plan.required_database_count
        and len(confirmatory) >= plan.required_task_count
        and run_count >= plan.minimum_formal_run_count
    )
    return PowerReadiness(
        required_database_count=plan.required_database_count,
        actual_database_count=database_count,
        required_task_count=plan.required_task_count,
        actual_task_count=len(confirmatory),
        required_run_count=plan.minimum_formal_run_count,
        actual_run_count=run_count,
        ready=ready,
    )


def _database_effects(rows: list[AgentResultRow]) -> list[float]:
    grouped: dict[tuple[str, str, str, str, int], dict[str, AgentResultRow]] = defaultdict(dict)
    for row in rows:
        if row.corpus != "natural" or row.condition not in {"control", "treatment"}:
            continue
        key = (row.model_id, row.host, row.database_id, row.task_id, row.repetition)
        if row.condition in grouped[key]:
            raise ValueError("duplicate pilot result row")
        grouped[key][row.condition] = row
    by_database: dict[str, list[float]] = defaultdict(list)
    for key, conditions in grouped.items():
        if set(conditions) != {"control", "treatment"}:
            raise ValueError("pilot power rows require paired conditions")
        control = conditions["control"]
        treatment = conditions["treatment"]
        by_database[key[2]].append(
            float(treatment.join_correct_task_completion)
            - float(control.join_correct_task_completion)
        )
    return [sum(values) / len(values) for _, values in sorted(by_database.items())]
