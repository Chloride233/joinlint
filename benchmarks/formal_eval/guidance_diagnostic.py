from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from inspect_ai import Task, eval
from inspect_ai.agent import AgentSubmit, as_solver, react
from inspect_ai.dataset import Sample
from inspect_ai.log import read_eval_log
from inspect_ai.model import ChatMessageAssistant, ModelOutput, get_model
from inspect_ai.scorer import Score, Scorer, Target, mean, scorer
from inspect_ai.solver import TaskState
from inspect_ai.tool import Tool, ToolCall, tool
from pydantic import model_validator

from benchmarks.formal_eval.contracts import StrictModel
from joinlint.contracts import canonical_json
from joinlint.mcp_contracts import NextAction, error_response


Condition = Literal["guidance_removed", "guidance_v3"]


class GuidanceCase(StrictModel):
    case_id: str
    command: Literal["get_join_plan", "validate_sql"]
    code: str
    inconclusive: bool
    expected_action: NextAction


class GuidanceDiagnosticSummary(StrictModel):
    schema_version: Literal[1] = 1
    evidence_class: Literal["synthetic_non_evidentiary_guidance_ablation"] = (
        "synthetic_non_evidentiary_guidance_ablation"
    )
    sample_count: Literal[8] = 8
    model: Literal["mockllm/model"] = "mockllm/model"
    passed_by_condition: dict[Condition, int]
    total_by_condition: dict[Condition, int]
    pipeline_passed: bool

    @model_validator(mode="after")
    def require_complete_matrix(self) -> GuidanceDiagnosticSummary:
        expected = {"guidance_removed": 4, "guidance_v3": 4}
        if self.total_by_condition != expected:
            raise ValueError("guidance diagnostic requires four samples per condition")
        return self


CASES = (
    GuidanceCase(
        case_id="proof-stale",
        command="validate_sql",
        code="PROOF_STALE",
        inconclusive=True,
        expected_action="replan",
    ),
    GuidanceCase(
        case_id="proof-graph-mismatch",
        command="validate_sql",
        code="PROOF_GRAPH_MISMATCH",
        inconclusive=False,
        expected_action="revise_sql",
    ),
    GuidanceCase(
        case_id="resource-limit",
        command="get_join_plan",
        code="RESOURCE_LIMIT_EXCEEDED",
        inconclusive=True,
        expected_action="reduce_request",
    ),
    GuidanceCase(
        case_id="no-verified-path",
        command="get_join_plan",
        code="NO_VERIFIED_PATH",
        inconclusive=True,
        expected_action="stop",
    ),
)


@tool(name="submit_guidance_action")
def submit_guidance_action() -> Tool:
    async def execute(action: str, request_changed: bool, execute_sql: bool) -> str:
        """Submit the action selected after reading one JoinLint response.

        Args:
            action: One stable JoinLint next_action value.
            request_changed: Whether the next request differs from the blocked request.
            execute_sql: Whether SQL should be executed after this non-OK response.
        """
        return json.dumps(
            {
                "action": action,
                "request_changed": request_changed,
                "execute_sql": execute_sql,
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    return execute


@scorer(metrics=[mean()])
def guidance_diagnostic_scorer() -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        del target
        expected_action = str(state.metadata["expected_action"])
        try:
            payload = json.loads(state.output.completion)
        except (AttributeError, TypeError, json.JSONDecodeError):
            return Score(value=0, metadata={"failure": "MALFORMED_SUBMISSION"})
        action = payload.get("action")
        request_changed = payload.get("request_changed")
        execute_sql = payload.get("execute_sql")
        passed = (
            action == expected_action
            and execute_sql is False
            and request_changed is (expected_action != "stop")
        )
        return Score(
            value=1 if passed else 0,
            metadata={
                "condition": state.metadata["condition"],
                "expected_action": expected_action,
                "observed_action": action,
            },
        )

    return score


def run_guidance_diagnostic(output: Path) -> GuidanceDiagnosticSummary:
    output.mkdir(parents=True, exist_ok=True)
    task = Task(
        dataset=_samples(),
        solver=as_solver(
            react(
                prompt=(
                    "Choose the safest next action for the supplied non-OK JoinLint response. "
                    "Never execute SQL after a non-OK response. Retry only with a changed "
                    "request when the selected action is not stop."
                ),
                tools=[],
                attempts=1,
                submit=AgentSubmit(
                    tool=submit_guidance_action(),
                    name="submit_guidance_action",
                    keep_in_messages=True,
                ),
            )
        ),
        scorer=[guidance_diagnostic_scorer()],
        name="joinlint_guidance_ablation_smoke",
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
            turn_limit=3,
            max_tokens=1024,
            temperature=0,
            parallel_tool_calls=False,
            cache=False,
        )
    finally:
        if previous_trace is None:
            os.environ.pop("INSPECT_TRACE_FILE", None)
        else:
            os.environ["INSPECT_TRACE_FILE"] = previous_trace
    samples = [
        sample
        for log in logs
        for sample in (
            read_eval_log(log.location).samples
            if log.samples is None
            else log.samples
        )
        or []
    ]
    totals: dict[Condition, int] = {"guidance_removed": 0, "guidance_v3": 0}
    passed: dict[Condition, int] = {"guidance_removed": 0, "guidance_v3": 0}
    for sample in samples:
        condition: Condition = (sample.id or "").rsplit("-", 1)[-1]  # type: ignore[assignment]
        if condition not in totals:
            continue
        totals[condition] += 1
        if (
            sample.error is None
            and sample.scores is not None
            and sample.scores["guidance_diagnostic_scorer"].value == 1
        ):
            passed[condition] += 1
    summary = GuidanceDiagnosticSummary(
        passed_by_condition=passed,
        total_by_condition=totals,
        pipeline_passed=(passed["guidance_v3"] == 4 and passed["guidance_removed"] == 1),
    )
    (output / "guidance-diagnostic.json").write_bytes(
        canonical_json(summary.model_dump(mode="json"))
    )
    if not summary.pipeline_passed:
        raise RuntimeError("guidance diagnostic mock matrix did not match its frozen policy")
    return summary


def _samples() -> list[Sample]:
    samples: list[Sample] = []
    for case in CASES:
        response = error_response(
            case.command,
            case.code,
            inconclusive=case.inconclusive,
        ).model_dump(mode="json")
        for condition in ("guidance_removed", "guidance_v3"):
            document = json.loads(json.dumps(response))
            if condition == "guidance_removed":
                document["schema_version"] = 2
                document["error"].pop("guidance", None)
            payload = {
                "condition": condition,
                "joinlint_response": document,
            }
            samples.append(
                Sample(
                    id=f"{case.case_id}-{condition}",
                    input=canonical_json(payload).decode("utf-8"),
                    target=case.expected_action,
                    metadata={
                        "condition": condition,
                        "expected_action": case.expected_action,
                    },
                )
            )
    return samples


def _mock_outputs(
    messages: list[object],
    tools: list[Any],
    tool_choice: object,
    config: object,
) -> ModelOutput:
    del tool_choice, config
    document = _input_document(messages)
    response = document["joinlint_response"]
    error = response.get("error") if isinstance(response, dict) else None
    guidance = error.get("guidance") if isinstance(error, dict) else None
    action = guidance.get("next_action") if isinstance(guidance, dict) else "stop"
    function = next(
        candidate.name
        for candidate in tools
        if candidate.name.endswith("submit_guidance_action")
    )
    return ModelOutput.from_message(
        ChatMessageAssistant(
            content="",
            tool_calls=[
                ToolCall(
                    id=f"guidance-diagnostic-{len(messages)}",
                    function=function,
                    arguments={
                        "action": action,
                        "request_changed": action != "stop",
                        "execute_sql": False,
                    },
                )
            ],
        ),
        stop_reason="tool_calls",
    )


def _input_document(messages: list[object]) -> dict[str, Any]:
    for message in messages:
        content = getattr(message, "content", None)
        if not isinstance(content, str):
            continue
        try:
            value = json.loads(content)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "joinlint_response" in value:
            return value
    raise ValueError("guidance diagnostic input is missing")
