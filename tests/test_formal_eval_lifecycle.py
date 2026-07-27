from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from inspect_ai.agent import AgentState, agent
from inspect_ai.model import ModelOutput
from inspect_ai.scorer import Target
from inspect_ai.solver import TaskState
from inspect_ai.util import store
from inspect_ai.util._store import init_subtask_store

from benchmarks.formal_eval import inspect_task
from benchmarks.formal_eval.lifecycle import (
    LIFECYCLE_STORE_KEY,
    LifecycleFailureReason,
    allow_scoring,
    complete_evaluation,
    fail_evaluation,
    infrastructure_prepared,
    new_lifecycle,
    parse_lifecycle,
    readiness_failed,
    readiness_passed,
    scoring_eligibility,
    start_evaluation,
    write_lifecycle,
)


NOW = datetime(2026, 7, 27, tzinfo=timezone.utc)


def test_evaluation_cannot_start_before_readiness() -> None:
    record = new_lifecycle("codex", "0.144.1", now=NOW)

    with pytest.raises(ValueError, match="illegal lifecycle transition"):
        start_evaluation(record, now=NOW)


def test_infrastructure_preparation_does_not_start_evaluation_or_pass_readiness() -> None:
    record = infrastructure_prepared(
        new_lifecycle("codex", "0.144.1", now=NOW),
        duration_seconds=2,
        now=NOW,
    )

    assert record.phase == "INFRASTRUCTURE_PENDING"
    assert record.infrastructure_status == "pending"
    assert record.evaluation_status == "not_started"
    assert scoring_eligibility(record).eligible is False


def test_readiness_failure_is_scoring_ineligible() -> None:
    record = readiness_failed(
        new_lifecycle("codex", "0.144.1", now=NOW),
        duration_seconds=1,
        detail="readiness_timeout",
        now=NOW,
    )

    eligibility = scoring_eligibility(record)

    assert eligibility.eligible is False
    assert eligibility.failure_code == "INFRASTRUCTURE_FAILURE"
    assert eligibility.lifecycle_reason == LifecycleFailureReason.READINESS_FAILED


def test_started_evaluation_is_scoring_ineligible() -> None:
    record = readiness_passed(
        new_lifecycle("codex", "0.144.1", now=NOW),
        duration_seconds=1,
        now=NOW,
    )
    record = start_evaluation(record, now=NOW)

    eligibility = scoring_eligibility(record)

    assert eligibility.eligible is False
    assert eligibility.lifecycle_reason == LifecycleFailureReason.EVALUATION_NOT_STARTED


def test_completed_evaluation_is_scoring_eligible() -> None:
    record = readiness_passed(
        new_lifecycle("codex", "0.144.1", now=NOW),
        duration_seconds=1,
        now=NOW,
    )
    record = start_evaluation(record, now=NOW)
    record = complete_evaluation(record, duration_seconds=2, now=NOW)
    record = allow_scoring(record)

    assert scoring_eligibility(record).eligible is True


@pytest.mark.parametrize(
    "scorer_factory",
    [inspect_task.formal_join_scorer, inspect_task.formal_execution_scorer],
)
def test_semantic_scorers_do_not_parse_output_without_lifecycle_eligibility(
    monkeypatch: pytest.MonkeyPatch,
    scorer_factory: object,
) -> None:
    def forbidden_semantic_logic(*args: object, **kwargs: object) -> None:
        raise AssertionError("semantic SQL logic must not run")

    monkeypatch.setattr(inspect_task, "extract_submission", forbidden_semantic_logic)
    state = _state(store={})

    score = asyncio.run(scorer_factory()(state, Target("SELECT 1")))  # type: ignore[operator]

    assert score.value == 0
    assert score.metadata == {
        "scoring_eligible": False,
        "score_kind": "task_outcome",
        "failure_code": "INFRASTRUCTURE_FAILURE",
        "lifecycle_reason": LifecycleFailureReason.EVALUATION_NOT_STARTED,
    }


def test_infrastructure_failure_never_creates_normal_agent_result() -> None:
    failed = readiness_failed(
        new_lifecycle("claude_code", "2.1.212", now=NOW),
        duration_seconds=3,
        detail="host_binary_version_mismatch",
        now=NOW,
    )
    state = _state(store={LIFECYCLE_STORE_KEY: failed.model_dump(mode="json")})

    score = asyncio.run(inspect_task.formal_join_scorer()(state, Target("SELECT 1")))

    assert score.value == 0
    assert score.metadata["score_kind"] == "task_outcome"
    assert "join_correct_task_completion" not in score.metadata


def test_model_timeout_remains_distinct_from_infrastructure_failure() -> None:
    record = readiness_passed(
        new_lifecycle("codex", "0.144.1", now=NOW),
        duration_seconds=1,
        now=NOW,
    )
    record = start_evaluation(record, now=NOW)
    record = fail_evaluation(
        record,
        reason=LifecycleFailureReason.MODEL_TIMEOUT,
        duration_seconds=90,
        now=NOW,
    )

    eligibility = scoring_eligibility(record)

    assert eligibility.failure_code == "MODEL_TIMEOUT"
    assert eligibility.lifecycle_reason == LifecycleFailureReason.MODEL_TIMEOUT


def test_evaluation_wrapper_becomes_eligible_only_after_first_model_boundary() -> None:
    state = _ready_state()
    init_subtask_store(state.store)

    result = asyncio.run(
        inspect_task.evaluation_lifecycle(_started_agent(), 1, 1)(state, None)  # type: ignore[arg-type]
    )

    record = parse_lifecycle(result.store.get(LIFECYCLE_STORE_KEY))
    assert record.scoring_eligible is True


def test_agent_stopping_before_first_model_boundary_is_infrastructure_outcome() -> None:
    state = _pending_state()
    init_subtask_store(state.store)

    result = asyncio.run(
        inspect_task.evaluation_lifecycle(_never_started_agent(), 1, 1)(state, None)  # type: ignore[arg-type]
    )

    record = parse_lifecycle(result.store.get(LIFECYCLE_STORE_KEY))
    assert record.failure_reason == LifecycleFailureReason.EVALUATION_NOT_STARTED
    assert record.evaluation_status == "not_started"
    assert scoring_eligibility(record).failure_code == "INFRASTRUCTURE_FAILURE"


def _state(*, store: dict[str, object]) -> TaskState:
    return TaskState(
        model="mockllm/model",
        sample_id="lifecycle-test",
        epoch=1,
        input="question",
        messages=[],
        target=Target("SELECT 1"),
        output=ModelOutput.from_content(model="mockllm/model", content="not-json"),
        metadata={},
        store=store,
    )


def _pending_state() -> TaskState:
    record = infrastructure_prepared(
        new_lifecycle("codex", "0.144.1", now=NOW),
        duration_seconds=0,
        now=NOW,
    )
    return _state(store={LIFECYCLE_STORE_KEY: record.model_dump(mode="json")})


def _ready_state() -> TaskState:
    record = readiness_passed(
        new_lifecycle("codex", "0.144.1", now=NOW),
        duration_seconds=0,
        now=NOW,
    )
    return _state(store={LIFECYCLE_STORE_KEY: record.model_dump(mode="json")})


@agent
def _started_agent():  # type: ignore[no-untyped-def]
    async def execute(agent_state: AgentState) -> AgentState:
        active_store = store()
        record = parse_lifecycle(active_store.get(LIFECYCLE_STORE_KEY))
        write_lifecycle(active_store, start_evaluation(record, now=NOW))
        return agent_state

    return execute


@agent
def _never_started_agent():  # type: ignore[no-untyped-def]
    async def execute(agent_state: AgentState) -> AgentState:
        return agent_state

    return execute
