from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from inspect_ai.scorer import Score, Scorer, Target, mean, scorer
from inspect_ai.solver import TaskState

from benchmarks.agent_join.contracts import JoinScore, PilotSample
from benchmarks.agent_join.execution import execution_matches
from benchmarks.agent_join.sql_edges import (
    extract_join_edges,
    extract_submission,
    score_join_graph,
)
from benchmarks.agent_join.trace import score_trace


EXECUTION_DEADLINE_SECONDS = 5
EXECUTION_MAX_ROWS = 10_000


def _sample(state: TaskState) -> PilotSample:
    return PilotSample.model_validate_json(
        json.dumps(state.metadata, ensure_ascii=False)
    )


@scorer(metrics=[mean()])
def join_outcome_scorer() -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        del target
        sample = _sample(state)
        completion = state.output.completion if state.output is not None else ""
        submitted_sql = ""
        warning = ""
        try:
            submission = extract_submission(completion)
            submitted_sql = submission.sql
            warning = submission.warning
            predicted = extract_join_edges(submission.sql, sample.schema_map)
            outcome = score_join_graph(predicted, sample.allowed_graphs)
        except ValueError:
            outcome = JoinScore(
                wrong_join=True,
                precision=0.0,
                recall=0.0,
                f1=0.0,
                predicted_edges=[],
                matched_graph=sample.allowed_graphs[0],
                error_code="INVALID_MODEL_OUTPUT",
            )
        metadata = outcome.model_dump(mode="json")
        metadata["submitted_sql"] = submitted_sql
        metadata["warning"] = warning
        return Score(
            value=0 if outcome.wrong_join else 1,
            answer=completion,
            metadata=metadata,
        )

    return score


@scorer(metrics=[mean()])
def mcp_trace_scorer() -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        del target
        sample = _sample(state)
        if sample.arm in {"A", "B"}:
            return Score.unscored(metadata={"applicable": False})
        completion = state.output.completion if state.output is not None else ""
        submission_error = False
        try:
            submission = extract_submission(completion)
            predicted = extract_join_edges(submission.sql, sample.schema_map)
            warning = submission.warning
        except ValueError:
            submission_error = True
            predicted = frozenset()
            warning = ""
        trace = score_trace(
            state.messages,
            predicted,
            sample.allowed_graphs[0],
            warning,
        )
        metadata = trace.model_dump(mode="json")
        metadata["applicable"] = True
        metadata["submission_error"] = submission_error
        return Score(value=1 if trace.grounded else 0, metadata=metadata)

    return score


@scorer(metrics=[mean()])
def execution_scorer() -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        del target
        sample = _sample(state)
        completion = state.output.completion if state.output is not None else ""
        try:
            submitted_sql = extract_submission(completion).sql
        except ValueError:
            return Score(
                value=0,
                metadata={
                    "executed": False,
                    "equivalent": None,
                    "error_code": "INVALID_MODEL_OUTPUT",
                },
            )
        result = execution_matches(
            Path(sample.database_path),
            sample.task_id,
            sample.gold_sql,
            submitted_sql,
            deadline_seconds=EXECUTION_DEADLINE_SECONDS,
            max_rows=EXECUTION_MAX_ROWS,
        )
        metadata = asdict(result)
        metadata.pop("rows", None)
        if result.error_code == "GOLD_EXECUTION_FAILED":
            return Score.unscored(metadata=metadata)
        return Score(
            value=1 if result.equivalent else 0,
            metadata=metadata,
        )

    return score
