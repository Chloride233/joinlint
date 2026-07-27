from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from inspect_ai import Task, eval
from inspect_ai.agent import AgentSubmit, as_solver, react
from inspect_ai.dataset import Sample
from inspect_ai.model import ChatMessageAssistant, ChatMessageTool, ModelOutput, get_model
from inspect_ai.log import read_eval_log
from inspect_ai.scorer import Score, Scorer, Target, mean, scorer
from inspect_ai.solver import TaskState
from inspect_ai.tool import Tool, ToolCall, mcp_server_stdio, tool

from benchmarks.agent_join.sql_edges import canonical_edge, extract_submission
from benchmarks.formal_eval.trace import assess_trace
from benchmarks.formal_eval.inspect_task import _tool_events, _tool_result_payload
from joinlint.contracts import canonical_json


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SAFE_SQL = (
    "SELECT Album.Title, Artist.Name FROM Album "
    "JOIN Artist ON Album.ArtistId = Artist.ArtistId"
)


@tool(name="submit_sql")
def submit_sql() -> Tool:
    async def execute(sql: str, warning: str) -> str:
        """Submit one final SQLite SELECT and an optional safety warning.

        Args:
            sql: The exact final read-only SQLite SELECT.
            warning: Empty after a safe validation; otherwise the blocking reason.
        """
        return json.dumps(
            {"sql": sql, "warning": warning},
            separators=(",", ":"),
            sort_keys=True,
        )

    return execute


@scorer(metrics=[mean()])
def inspect_smoke_scorer() -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        del target
        try:
            submission = extract_submission(state.output.completion)
        except (AttributeError, ValueError, json.JSONDecodeError):
            return Score(value=0, metadata={"failure_code": "SQL_PARSE_FAILED"})
        trace = assess_trace(
            _tool_events(state.messages),
            expected_entities={"Album", "Artist"},
            final_sql=submission.sql,
            final_edges={canonical_edge("Album.ArtistId", "Artist.ArtistId")},
            submitted_sql=bool(submission.sql),
        )
        return Score(
            value=1 if trace.mcp_grounded and submission.sql == SAFE_SQL else 0,
            metadata={"trace": trace.model_dump(mode="json")},
        )

    return score


def run_inspect_smoke(project: Path, output: Path) -> dict[str, Any]:
    project = project.resolve(strict=True)
    output.mkdir(parents=True, exist_ok=True)
    environment = _mcp_environment(output / "cache")
    server = mcp_server_stdio(
        name="JoinLint",
        command=sys.executable,
        args=[
            "-m",
            "joinlint",
            "serve-mcp",
            "--project",
            str(project),
            "--source",
            "chinook.sqlite",
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
    )
    agent = react(
        prompt=(
            "Use JoinLint get_join_plan before generating multi-table SQL. "
            "Validate the exact final SQL with the returned plan_id. "
            "JoinLint proof is not query correctness."
        ),
        tools=[server],
        attempts=1,
        submit=AgentSubmit(tool=submit_sql(), name="submit_sql", keep_in_messages=True),
    )
    task = Task(
        dataset=[
            Sample(
                id="inspect-stage1-smoke",
                input="List every album title with its artist name.",
                target=SAFE_SQL,
            )
        ],
        solver=as_solver(agent),
        scorer=[inspect_smoke_scorer()],
        name="joinlint_stage1_inspect_smoke",
    )
    previous_trace = os.environ.get("INSPECT_TRACE_FILE")
    os.environ["INSPECT_TRACE_FILE"] = str(output / "inspect-trace.log")
    try:
        logs = eval(
            task,
            model=get_model(
                "mockllm/model",
                custom_outputs=_mock_outputs,
                memoize=False,
            ),
            log_dir=str(output / "logs"),
            log_format="eval",
            log_realtime=False,
            display="none",
            fail_on_error=False,
            retry_on_error=0,
            score_on_error=True,
            max_samples=1,
            max_retries=0,
            timeout=60,
            time_limit=60,
            turn_limit=5,
            max_tokens=2048,
            temperature=0,
            parallel_tool_calls=False,
            cache=False,
        )
    finally:
        if previous_trace is None:
            os.environ.pop("INSPECT_TRACE_FILE", None)
        else:
            os.environ["INSPECT_TRACE_FILE"] = previous_trace
    materialized_logs = [
        read_eval_log(log.location) if log.samples is None else log
        for log in logs
    ]
    samples = [sample for log in materialized_logs for sample in log.samples or []]
    passed = (
        len(samples) == 1
        and samples[0].error is None
        and samples[0].scores is not None
        and samples[0].scores["inspect_smoke_scorer"].value == 1
    )
    summary = {
        "schema_version": 2,
        "evidence_class": "synthetic_non_evidentiary_inspect_smoke",
        "inspect_sample_count": len(samples),
        "passed": passed,
        "model": "mockllm/model",
        "tools": ["get_join_plan", "validate_sql"],
    }
    (output / "inspect-smoke.json").write_bytes(canonical_json(summary))
    if not passed:
        raise RuntimeError("Inspect mock smoke did not complete the grounded two-tool flow")
    return summary


def _mock_outputs(
    messages: list[object],
    tools: list[Any],
    tool_choice: object,
    config: object,
) -> ModelOutput:
    del tool_choice, config
    names = {candidate.name for candidate in tools}
    tool_messages = [message for message in messages if isinstance(message, ChatMessageTool)]
    called = {message.function or "" for message in tool_messages}
    if not any(name.endswith("get_join_plan") for name in called):
        function = next(name for name in names if name.endswith("get_join_plan"))
        arguments: dict[str, object] = {
            "entity_refs": [
                {"ref": "album", "entity": "Album"},
                {"ref": "artist", "entity": "Artist"},
            ],
            "start_ref": "album",
            "expected_grain_ref": "album",
        }
    elif not any(name.endswith("validate_sql") for name in called):
        function = next(name for name in names if name.endswith("validate_sql"))
        plan = _last_payload(tool_messages, "get_join_plan")
        data = plan.get("data")
        proof = data.get("proof") if isinstance(data, dict) else None
        plan_id = proof.get("plan_id") if isinstance(proof, dict) else None
        arguments = {"sql": SAFE_SQL, "plan_id": plan_id}
    else:
        function = next(name for name in names if name.endswith("submit_sql"))
        arguments = {"sql": SAFE_SQL, "warning": ""}
    message = ChatMessageAssistant(
        content="",
        tool_calls=[
            ToolCall(
                id=f"inspect-smoke-{len(messages)}",
                function=function,
                arguments=arguments,
            )
        ],
    )
    return ModelOutput.from_message(message, stop_reason="tool_calls")


def _last_payload(messages: list[ChatMessageTool], suffix: str) -> dict[str, Any]:
    for message in reversed(messages):
        if (message.function or "").endswith(suffix):
            return _tool_result_payload(message.content) or {}
    return {}


def _mcp_environment(cache_root: Path) -> dict[str, str]:
    allowed = ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR")
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    environment["PYTHONPATH"] = str(REPOSITORY_ROOT / "src")
    environment["XDG_CACHE_HOME"] = str(cache_root)
    return environment
