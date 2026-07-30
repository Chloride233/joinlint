from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("inspect_ai")

from inspect_ai.agent import AgentState, agent
from inspect_ai.event import SampleLimitEvent
from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageTool,
    GenerateConfig,
    ModelOutput,
    get_model,
)
from inspect_ai.scorer import Target
from inspect_ai.solver import TaskState
from inspect_ai.tool import ToolCall
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

    monkeypatch.setattr(
        inspect_task, "_extract_submission_tool_call", forbidden_semantic_logic
    )
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


def test_host_context_drift_is_a_distinct_pre_model_readiness_failure() -> None:
    state = _pending_state()
    init_subtask_store(state.store)

    result = asyncio.run(
        inspect_task.evaluation_lifecycle(_host_context_drift_agent(), 1, 1)(state, None)  # type: ignore[arg-type]
    )

    record = parse_lifecycle(result.store.get(LIFECYCLE_STORE_KEY))
    assert record.failure_reason == LifecycleFailureReason.HOST_CONTEXT_DRIFT
    assert record.evaluation_status == "not_started"
    assert scoring_eligibility(record).failure_code == "INFRASTRUCTURE_FAILURE"


def test_readiness_forces_sandbox_tools_injection_before_agent_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    class ReadySandbox:
        async def exec(self, *args: object, **kwargs: object) -> SimpleNamespace:
            calls.append(("exec", args, kwargs))
            return SimpleNamespace(success=True)

        async def exec_remote(self, command: list[str], *, stream: bool) -> SimpleNamespace:
            calls.append(("exec_remote", tuple(command), stream))
            return SimpleNamespace(success=True)

    monkeypatch.setattr(inspect_task, "sandbox", lambda: ReadySandbox())

    asyncio.run(inspect_task._run_readiness_probes("codex"))

    assert calls[-1] == ("exec_remote", ("true",), False)


def test_readiness_reports_sandbox_tools_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenSandbox:
        async def exec(self, *args: object, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(success=True)

        async def exec_remote(self, command: list[str], *, stream: bool) -> SimpleNamespace:
            return SimpleNamespace(success=False)

    monkeypatch.setattr(inspect_task, "sandbox", lambda: BrokenSandbox())

    with pytest.raises(RuntimeError, match="sandbox_tools_readiness_failed"):
        asyncio.run(inspect_task._run_readiness_probes("codex"))


def test_readiness_probes_the_selected_host_bridge_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    class ReadySandbox:
        async def exec(self, command: list[str], **kwargs: object) -> SimpleNamespace:
            commands.append(command)
            return SimpleNamespace(success=True)

        async def exec_remote(self, command: list[str], *, stream: bool) -> SimpleNamespace:
            return SimpleNamespace(success=True)

    monkeypatch.setattr(inspect_task, "sandbox", lambda: ReadySandbox())

    asyncio.run(inspect_task._run_readiness_probes("claude_code"))

    assert "import anthropic" in commands[0][2]


def test_model_limit_after_first_request_is_not_infrastructure_failure() -> None:
    record = readiness_passed(
        new_lifecycle("codex", "0.144.1", now=NOW),
        duration_seconds=1,
        now=NOW,
    )
    record = start_evaluation(record, now=NOW)
    record = fail_evaluation(
        record,
        reason=LifecycleFailureReason.MODEL_LIMIT,
        duration_seconds=2,
        now=NOW,
    )

    eligibility = scoring_eligibility(record)

    assert eligibility.failure_code == "MODEL_LIMIT"


def test_native_sample_limit_usage_detects_generic_bridge_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exceeded = SimpleNamespace(limit=35_000, usage=52_973)
    unused = SimpleNamespace(limit=None, usage=0)
    monkeypatch.setattr(
        inspect_task,
        "sample_limits",
        lambda: SimpleNamespace(token=exceeded, message=unused, turn=unused),
    )

    assert inspect_task._sample_model_limit_exceeded() is True


def test_native_sample_limit_usage_skips_unsupported_message_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnsupportedMessageLimit:
        limit = 12

        @property
        def usage(self) -> float:
            raise NotImplementedError

    below = SimpleNamespace(limit=35_000, usage=20_000)
    unused = SimpleNamespace(limit=None, usage=0)
    monkeypatch.setattr(
        inspect_task,
        "sample_limits",
        lambda: SimpleNamespace(
            token=below,
            message=UnsupportedMessageLimit(),
            turn=unused,
        ),
    )

    assert inspect_task._sample_model_limit_exceeded() is False


def test_native_sample_limit_event_detects_unsupported_message_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnsupportedMessageLimit:
        limit = 20

        @property
        def usage(self) -> float:
            raise NotImplementedError

    below = SimpleNamespace(limit=35_000, usage=20_000)
    unused = SimpleNamespace(limit=None, usage=0)
    monkeypatch.setattr(
        inspect_task,
        "sample_limits",
        lambda: SimpleNamespace(
            token=below,
            message=UnsupportedMessageLimit(),
            turn=unused,
        ),
    )
    event = SampleLimitEvent(
        type="message",
        limit=20,
        message="Message limit reached. count: 20; limit: 20",
    )
    history = SimpleNamespace(recent_events=lambda count: [event])
    monkeypatch.setattr(inspect_task, "transcript", lambda: SimpleNamespace(history=history))

    assert inspect_task._sample_model_limit_exceeded() is True


def test_formal_join_scorer_uses_successful_submission_tool_call() -> None:
    state = _eligible_state()
    state.metadata = {
        "condition": "control",
        "allowed_graphs": [[]],
        "schema": {},
        "oracle_has_safe_path": True,
    }
    state.messages = _submission_messages("submit-one", "SELECT 1", "")

    score = asyncio.run(inspect_task.formal_join_scorer()(state, Target("SELECT 1")))

    assert score.value == 1
    assert score.metadata["join_correct_task_completion"] is True


def test_formal_join_scorer_rejects_duplicate_submission_tool_calls() -> None:
    state = _eligible_state()
    state.metadata = {"condition": "control"}
    state.messages = [
        *_submission_messages("submit-one", "SELECT 1", ""),
        *_submission_messages("submit-two", "SELECT 2", ""),
    ]

    score = asyncio.run(inspect_task.formal_join_scorer()(state, Target("SELECT 1")))

    assert score.value == 0
    assert score.metadata["failure_code"] == "SQL_PARSE_FAILED"


def test_sql_parse_failure_still_reports_treatment_tool_funnel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _eligible_state()
    state.metadata = {"condition": "treatment", "expected_entities": ["cars", "trains"]}
    monkeypatch.setattr(inspect_task, "_tool_events", lambda messages: [])
    monkeypatch.setattr(
        inspect_task,
        "assess_trace",
        lambda *args, **kwargs: SimpleNamespace(
            model_dump=lambda mode: {"plan_called": True, "final_sql_validated": False}
        ),
    )

    score = asyncio.run(inspect_task.formal_join_scorer()(state, Target("SELECT 1")))

    assert score.metadata["failure_code"] == "SQL_PARSE_FAILED"
    assert score.metadata["trace"]["plan_called"] is True


def test_claude_namespaced_joinlint_tools_are_recognized() -> None:
    assert inspect_task._tool_name("mcp__JoinLint__get_join_plan") == "get_join_plan"
    assert inspect_task._tool_name("mcp__JoinLint__validate_sql") == "validate_sql"
    assert inspect_task._tool_name("mcp__EvaluationDatabase__execute_sql") is None


def test_codex_bridge_tool_result_envelope_is_parsed() -> None:
    assert inspect_task._tool_result_payload(
        'Wall time: 0.0065 seconds\nOutput:\n{"status":"ok"}'
    ) == {"status": "ok"}


def test_unrecognized_tool_result_envelope_is_not_parsed() -> None:
    assert inspect_task._tool_result_payload('Output:\n{"status":"ok"}') is None


def test_pilot_task_ids_accept_inspect_list_normalization() -> None:
    assert inspect_task._normalized_pilot_task_ids("task-a,task-b") == (
        "task-a",
        "task-b",
    )
    assert inspect_task._normalized_pilot_task_ids(["task-a", "task-b"]) == (
        "task-a",
        "task-b",
    )


def test_treatment_harness_is_single_plan_and_fail_closed() -> None:
    assert "first tool call must be JoinLint get_join_plan" in inspect_task.HARNESS_PROMPT
    assert "Call get_join_plan exactly once" in inspect_task.HARNESS_PROMPT
    assert "submit empty SQL" in inspect_task.HARNESS_PROMPT
    assert "GRAIN_INCOMPATIBLE" in inspect_task.HARNESS_PROMPT


def test_host_context_profile_disables_unneeded_builtin_tools() -> None:
    assert inspect_task.CODEX_CONTEXT_CONFIG_OVERRIDES == {
        "features.apps": "false",
        "features.computer_use": "false",
        "features.default_mode_request_user_input": "false",
        "features.image_generation": "false",
        "features.multi_agent": "false",
        "features.plugins": "false",
        "features.shell_tool": "false",
        "features.unified_exec": "false",
        "features.workspace_dependencies": "false",
    }
    assert {"Bash", "Read", "Write", "Agent", "WebSearch"} <= set(
        inspect_task.CLAUDE_DISALLOWED_BUILTIN_TOOLS
    )


def test_host_context_profile_accepts_only_required_mcp_and_bounded_codex_tools() -> None:
    codex_tools = [
        SimpleNamespace(name=name)
        for name in (
            "execute_sql",
            "submit_sql",
            "get_join_plan",
            "validate_sql",
            "update_plan",
            "request_user_input",
            "view_image",
        )
    ]
    inspect_task._require_host_tool_surface("codex", "treatment", codex_tools)

    claude_tools = [
        SimpleNamespace(name=name)
        for name in (
            "mcp__EvaluationDatabase__execute_sql",
            "mcp__EvaluationDatabase__submit_sql",
            "mcp__JoinLint__get_join_plan",
            "mcp__JoinLint__validate_sql",
        )
    ]
    inspect_task._require_host_tool_surface("claude_code", "treatment", claude_tools)

    with pytest.raises(RuntimeError, match="unexpected=exec_command"):
        inspect_task._require_host_tool_surface(
            "codex",
            "treatment",
            [*codex_tools, SimpleNamespace(name="exec_command")],
        )
    with pytest.raises(RuntimeError, match="missing=mcp__JoinLint__validate_sql"):
        inspect_task._require_host_tool_surface(
            "claude_code",
            "treatment",
            claude_tools[:-1],
        )


def test_host_context_probe_short_circuits_before_provider_model() -> None:
    state = _pending_state()
    init_subtask_store(state.store)
    tools = [
        SimpleNamespace(name=name)
        for name in (
            "execute_sql",
            "submit_sql",
            "get_join_plan",
            "validate_sql",
            "update_plan",
        )
    ]
    context_filter = inspect_task._host_context_filter(
        "codex",
        "treatment",
        short_circuit=True,
    )

    output = asyncio.run(
        context_filter(
            get_model("mockllm/model"),
            [],
            tools,
            None,
            GenerateConfig(),
        )
    )

    assert output is not None
    assert output.usage is None
    assert output.completion == "Host context profile accepted."
    observation = state.store.get(inspect_task.HOST_CONTEXT_STORE_KEY)
    assert observation["tool_names"] == (
        "execute_sql",
        "get_join_plan",
        "submit_sql",
        "update_plan",
        "validate_sql",
    )
    record = parse_lifecycle(state.store.get(LIFECYCLE_STORE_KEY))
    assert record.evaluation_status == "started"
    record = complete_evaluation(record, duration_seconds=1, now=NOW)
    write_lifecycle(state.store, allow_scoring(record))
    score = asyncio.run(inspect_task.formal_host_context_scorer()(state, Target("")))
    assert score.value == 1
    assert score.metadata["provider_short_circuited"] is True


def test_solver_uses_pure_frozen_host_context_options() -> None:
    codex = inspect_task._codex_host_options(strict_pilot=True)
    claude = inspect_task._claude_host_options(strict_pilot=True)

    assert codex == {
        "web_search": "disabled",
        "goals": False,
        "config_overrides": inspect_task.CODEX_CONTEXT_CONFIG_OVERRIDES,
        "retry_refusals": 0,
    }
    assert claude == {
        "disallowed_tools": list(inspect_task.CLAUDE_DISALLOWED_BUILTIN_TOOLS),
        "retry_refusals": 0,
        "retry_uncaught_errors": 0,
    }


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


def _submission_messages(call_id: str, sql: str, warning: str) -> list[object]:
    return [
        ChatMessageAssistant(
            content="",
            tool_calls=[
                ToolCall(
                    id=call_id,
                    function="mcp__EvaluationDatabase__submit_sql",
                    arguments={"sql": sql, "warning": warning},
                )
            ],
        ),
        ChatMessageTool(
            content='{"status":"ok"}',
            tool_call_id=call_id,
            function="mcp__EvaluationDatabase__submit_sql",
        ),
    ]


def _pending_state() -> TaskState:
    record = infrastructure_prepared(
        new_lifecycle("codex", "0.144.1", now=NOW),
        duration_seconds=0,
        host_binary_sha256="a" * 64,
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


def _eligible_state() -> TaskState:
    record = readiness_passed(
        new_lifecycle("codex", "0.144.1", now=NOW),
        duration_seconds=0,
        now=NOW,
    )
    record = start_evaluation(record, now=NOW)
    record = complete_evaluation(record, duration_seconds=1, now=NOW)
    record = allow_scoring(record)
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


@agent
def _host_context_drift_agent():  # type: ignore[no-untyped-def]
    async def execute(agent_state: AgentState) -> AgentState:
        del agent_state
        raise inspect_task.HostContextDriftError("host_tool_surface_mismatch")

    return execute
