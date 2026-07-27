from __future__ import annotations

import hashlib
import math
import random
from collections import Counter
from typing import Literal

from pydantic import model_validator

from benchmarks.formal_eval.contracts import (
    Condition,
    FormalManifestV2,
    Host,
    PreregistrationV2,
    StrictModel,
)


class RunSpec(StrictModel):
    sample_id: str
    task_id: str
    database_id: str
    corpus: Literal["natural", "semantic_join_failure"]
    condition: Condition
    model_id: str
    host: Host
    repetition: int
    confirmatory: bool


class RunPlanV2(StrictModel):
    schema_version: Literal[2] = 2
    evaluation_id: str
    lineage_id: str
    runs: tuple[RunSpec, ...]
    blind_review_sample_ids: tuple[str, ...]

    @model_validator(mode="after")
    def require_unique_samples(self) -> RunPlanV2:
        sample_ids = [run.sample_id for run in self.runs]
        if len(sample_ids) != len(set(sample_ids)):
            raise ValueError("run plan contains duplicate sample IDs")
        if len(self.blind_review_sample_ids) != len(set(self.blind_review_sample_ids)):
            raise ValueError("run plan contains duplicate blind-review sample IDs")
        confirmatory_ids = {run.sample_id for run in self.runs if run.confirmatory}
        if not set(self.blind_review_sample_ids) <= confirmatory_ids:
            raise ValueError("blind-review sample must belong to the confirmatory plan")
        return self


def build_run_plan(
    manifest: FormalManifestV2,
    preregistration: PreregistrationV2,
    lineage_id: str,
) -> RunPlanV2:
    runs: list[RunSpec] = []
    confirmatory = [
        task for task in manifest.tasks if task.corpus == "natural" and task.split == "confirmatory"
    ]
    diagnostic = [task for task in manifest.tasks if task.split == "diagnostic"]
    for task in confirmatory:
        for model in preregistration.models:
            for host in preregistration.hosts:
                for repetition in range(preregistration.repetitions):
                    for condition in ("control", "treatment"):
                        runs.append(
                            _run(
                                task.task_id,
                                task.database_id,
                                task.corpus,
                                condition,
                                model.returned_id,
                                host,
                                repetition,
                                confirmatory=True,
                            )
                        )
    for task in diagnostic:
        for model in preregistration.models:
            for host in preregistration.hosts:
                for condition in ("oracle_inline", "oracle_mcp", "no_harness"):
                    runs.append(
                        _run(
                            task.task_id,
                            task.database_id,
                            task.corpus,
                            condition,
                            model.returned_id,
                            host,
                            0,
                            confirmatory=False,
                        )
                    )
    runs.sort(key=lambda run: run.sample_id.encode("utf-8"))
    confirmatory_ids = [run.sample_id for run in runs if run.confirmatory]
    review_count = max(80, math.ceil(len(confirmatory_ids) * 0.10))
    review_rng = random.Random(preregistration.seed)
    blind_review_ids = tuple(
        sorted(
            review_rng.sample(confirmatory_ids, k=review_count),
            key=lambda value: value.encode("utf-8"),
        )
    )
    return RunPlanV2(
        evaluation_id=preregistration.evaluation_id,
        lineage_id=lineage_id,
        runs=tuple(runs),
        blind_review_sample_ids=blind_review_ids,
    )


def summarize_run_plan(plan: RunPlanV2) -> dict[str, int]:
    counts = Counter("confirmatory" if run.confirmatory else "diagnostic" for run in plan.runs)
    return {
        "total": len(plan.runs),
        "confirmatory": counts["confirmatory"],
        "diagnostic": counts["diagnostic"],
    }


def _run(
    task_id: str,
    database_id: str,
    corpus: str,
    condition: str,
    model_id: str,
    host: str,
    repetition: int,
    *,
    confirmatory: bool,
) -> RunSpec:
    sample_id = sample_id_for(
        task_id=task_id,
        database_id=database_id,
        corpus=corpus,
        condition=condition,
        model_id=model_id,
        host=host,
        repetition=repetition,
    )
    return RunSpec(
        sample_id=sample_id,
        task_id=task_id,
        database_id=database_id,
        corpus=corpus,  # type: ignore[arg-type]
        condition=condition,  # type: ignore[arg-type]
        model_id=model_id,
        host=host,  # type: ignore[arg-type]
        repetition=repetition,
        confirmatory=confirmatory,
    )


def sample_id_for(
    *,
    task_id: str,
    database_id: str,
    corpus: str,
    condition: str,
    model_id: str,
    host: str,
    repetition: int,
) -> str:
    raw = "\0".join(
        (task_id, database_id, corpus, condition, model_id, host, str(repetition))
    )
    return f"run-{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"
