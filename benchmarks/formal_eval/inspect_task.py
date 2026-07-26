from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.model import ChatMessageAssistant, ChatMessageTool, GenerateConfig
from inspect_ai.scorer import Score, Scorer, Target, mean, scorer
from inspect_ai.solver import TaskState
from inspect_ai.tool import MCPServerConfigStdio
from inspect_ai.util import ComposeConfig, ComposeService
from benchmarks.agent_join.execution import execution_matches
from benchmarks.agent_join.sql_edges import extract_join_edges, extract_submission, score_join_graph
from benchmarks.formal_eval.contracts import (
    Condition,
    FormalManifestV2,
    Host,
    SealedAgentTask,
)
from benchmarks.formal_eval.deterministic import sanitized_mcp_environment
from benchmarks.formal_eval.manifest import load_document, verify_sealed_task_hashes
from benchmarks.formal_eval.oracle_mcp import OracleDocument
from benchmarks.formal_eval.trace import ToolEvent, assess_trace
from joinlint.contracts import canonical_json


BASE_PROMPT = """You are evaluating one SQLite question. Use the EvaluationDatabase execute_sql tool for data access. Return exactly one JSON object with string fields sql and warning. sql must contain one read-only SELECT, or be empty only when no safe join exists. Do not include Markdown."""
HARNESS_PROMPT = """Before generating multi-table SQL, call JoinLint get_join_plan with every intended table instance in entity_refs, a unique request-local ref for each instance, and the intended start_ref and expected_grain_ref. Use only the returned proof predicates. Call JoinLint validate_sql with the exact final SQL and returned plan_id. Do not execute SQL when planning or validation is error, inconclusive, stale, unavailable, or blocking. JoinLint proof is not query correctness."""


@task
def formal_agent_eval(
    sealed_tasks: str,
    manifest: str,
    host: Host,
    condition: Condition,
    agent_version: str,
    image_reference: str,
    lineage_id: str,
) -> Task:
    manifest_document = load_document(Path(manifest), FormalManifestV2)
    sealed = _load_sealed(Path(sealed_tasks))
    samples = _samples(manifest_document, sealed, condition, host, lineage_id)
    solver = _solver(host, condition, agent_version)
    return Task(
        dataset=samples,
        solver=solver,
        scorer=[formal_join_scorer(), formal_execution_scorer()],
        config=GenerateConfig(
            temperature=0,
            max_tokens=4096,
            parallel_tool_calls=False,
            cache=False,
            extra_body={"thinking": {"type": "disabled"}},
        ),
        sandbox=(
            "modal",
            ComposeConfig(
                services={
                    "default": ComposeService(
                        image=image_reference,
                        working_dir="/workspace/joinlint",
                        x_default=True,
                    )
                }
            ),
        ),
        name=f"joinlint_formal_{host}_{condition}",
    )


@scorer(metrics=[mean()])
def formal_join_scorer() -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        del target
        metadata = state.metadata or {}
        completion = state.output.completion if state.output is not None else ""
        try:
            submission = extract_submission(completion)
        except (ValueError, json.JSONDecodeError):
            return Score(value=0, metadata={"failure_code": "SQL_PARSE_FAILED"})
        allowed = metadata.get("allowed_graphs") or []
        oracle_has_safe_path = bool(metadata.get("oracle_has_safe_path"))
        condition = str(metadata["condition"])
        trace = None
        if condition in {"treatment", "oracle_mcp", "no_harness"}:
            trace = assess_trace(
                _tool_events(state.messages),
                expected_entities=set(metadata["expected_entities"]),
                final_sql=submission.sql,
                final_edges=set(),
                submitted_sql=bool(submission.sql),
            )
        if not submission.sql:
            safe_abstention = not oracle_has_safe_path and bool(submission.warning.strip())
            failure = None if safe_abstention else (
                trace.failure_code
                if trace is not None and trace.failure_code is not None
                else "PLAN_INCONCLUSIVE"
            )
            payload: dict[str, Any] = {
                "submitted_sql": False,
                "safe_abstention": safe_abstention,
                "join_correct_task_completion": safe_abstention,
                "join_graph_correct": False,
                "evaluator_validation_passed": False,
                "dangerous_sql_submitted": False,
                "failure_code": failure,
            }
            if trace is not None:
                payload["trace"] = trace.model_dump(mode="json")
            return Score(
                value=1 if safe_abstention else 0,
                metadata=payload,
            )
        try:
            edges = extract_join_edges(submission.sql, metadata["schema"])
            join_score = score_join_graph(edges, allowed)
        except (KeyError, ValueError):
            return Score(value=0, metadata={"failure_code": "SQL_PARSE_FAILED"})
        if condition in {"treatment", "oracle_mcp", "no_harness"}:
            trace = assess_trace(
                _tool_events(state.messages),
                expected_entities=set(metadata["expected_entities"]),
                final_sql=submission.sql,
                final_edges=set(edges),
                submitted_sql=True,
            )
        validation_passed = not join_score.wrong_join if trace is None else trace.validation_passed
        success = not join_score.wrong_join and validation_passed
        failure = None
        if not success:
            failure = (
                trace.failure_code
                if trace is not None and trace.failure_code is not None
                else "WRONG_PLAN"
            )
        payload: dict[str, Any] = {
            "submitted_sql": True,
            "safe_abstention": False,
            "join_correct_task_completion": success,
            "join_graph_correct": not join_score.wrong_join,
            "evaluator_validation_passed": validation_passed,
            "dangerous_sql_submitted": join_score.wrong_join,
            "failure_code": failure,
        }
        if trace is not None:
            payload["trace"] = trace.model_dump(mode="json")
        return Score(value=1 if success else 0, metadata=payload)

    return score


@scorer(metrics=[mean()])
def formal_execution_scorer() -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        del target
        metadata = state.metadata or {}
        completion = state.output.completion if state.output is not None else ""
        try:
            sql = extract_submission(completion).sql
        except (ValueError, json.JSONDecodeError):
            return Score(value=0, metadata={"error_code": "SQL_PARSE_FAILED"})
        if not sql:
            return Score(value=0, metadata={"error_code": "NO_SQL"})
        result = execution_matches(
            Path(str(metadata["database_path"])),
            str(metadata["task_id"]),
            str(metadata["gold_sql"]),
            sql,
            deadline_seconds=5,
            max_rows=10_000,
        )
        return Score(
            value=1 if result.equivalent else 0,
            metadata={"error_code": result.error_code},
        )

    return score


def _load_sealed(path: Path) -> dict[str, SealedAgentTask]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("sealed task file must be one regular file")
    payload = json.loads(path.read_text(encoding="utf-8"))
    tasks = [SealedAgentTask.model_validate(item) for item in payload]
    by_id = {item.task_id: item for item in tasks}
    if len(by_id) != len(tasks):
        raise ValueError("sealed task file contains duplicate task IDs")
    return by_id


def _samples(
    manifest: FormalManifestV2,
    sealed: dict[str, SealedAgentTask],
    condition: Condition,
    host: Host,
    lineage_id: str,
) -> list[Sample]:
    diagnostic = condition in {"oracle_inline", "oracle_mcp", "no_harness"}
    selected = [
        task
        for task in manifest.tasks
        if (task.split == "diagnostic") == diagnostic
        and (task.split == "confirmatory" or diagnostic)
    ]
    samples: list[Sample] = []
    for manifest_task in selected:
        actual = sealed.get(manifest_task.task_id)
        if actual is None:
            raise ValueError(f"sealed task is missing: {manifest_task.task_id}")
        verify_sealed_task_hashes(
            manifest_task.question_sha256,
            manifest_task.schema_sha256,
            manifest_task.sql_shape_sha256,
            actual,
        )
        body = f"Question:\n{actual.question}\n\nSchema:\n{actual.schema_text}"
        if condition == "oracle_inline":
            body += "\n\nAuthoritative join graphs:\n" + json.dumps(
                actual.allowed_graphs, separators=(",", ":")
            )
        database_destination = "/workspace/data/database.sqlite"
        files = {database_destination: actual.database_path}
        if condition == "oracle_mcp":
            oracle = OracleDocument(
                schema=actual.schema_map,
                allowed_graphs=actual.allowed_graphs,
            )
            files["/workspace/oracle.json"] = canonical_json(
                oracle.model_dump(mode="json", by_alias=True)
            ).decode("utf-8")
        samples.append(
            Sample(
                id=actual.task_id,
                input=body,
                target=actual.gold_sql,
                files=files,
                metadata={
                    "task_id": actual.task_id,
                    "database_id": actual.database_id,
                    "database_path": actual.database_path,
                    "gold_sql": actual.gold_sql,
                    "schema": actual.schema_map,
                    "expected_entities": actual.expected_entities,
                    "allowed_graphs": actual.allowed_graphs,
                    "oracle_has_safe_path": actual.oracle_has_safe_path,
                    "condition": condition,
                    "host": host,
                    "domain": manifest_task.domain,
                    "source_type": manifest_task.source_type,
                    "database_scale": manifest_task.database_scale,
                    "join_depth": manifest_task.join_depth,
                    "ambiguity": manifest_task.ambiguity,
                    "fanout_type": manifest_task.fanout_type,
                    "lineage_id": lineage_id,
                },
            )
        )
    return samples


def _solver(host: Host, condition: Condition, agent_version: str):  # type: ignore[no-untyped-def]
    from inspect_swe import claude_code, codex_cli

    servers = [
        MCPServerConfigStdio(
            name="EvaluationDatabase",
            command="python",
            args=[
                "-m",
                "benchmarks.formal_eval.database_mcp",
                "--database",
                "/workspace/data/database.sqlite",
            ],
            cwd="/workspace/joinlint",
            env=sanitized_mcp_environment(),
        )
    ]
    if condition in {"treatment", "no_harness"}:
        servers.append(
            MCPServerConfigStdio(
                name="JoinLint",
                command="python",
                args=["-m", "joinlint", "serve-mcp", "--auto", "--project", "/workspace/data"],
                cwd="/workspace/joinlint",
                env=sanitized_mcp_environment(),
            )
        )
    elif condition == "oracle_mcp":
        servers.append(
            MCPServerConfigStdio(
                name="JoinLint",
                command="python",
                args=[
                    "-m",
                    "benchmarks.formal_eval.oracle_mcp",
                    "--oracle",
                    "/workspace/oracle.json",
                ],
                cwd="/workspace/joinlint",
                env=sanitized_mcp_environment(),
            )
        )
    prompt = BASE_PROMPT
    if condition in {"treatment", "oracle_mcp"}:
        prompt += "\n\n" + HARNESS_PROMPT
    if host == "codex":
        return codex_cli(
            version=agent_version,
            system_prompt=prompt,
            mcp_servers=servers,
            web_search="disabled",
            goals=False,
        )
    return claude_code(
        version=agent_version,
        system_prompt=prompt,
        mcp_servers=servers,
        disallowed_tools=["WebSearch"],
    )


def _tool_events(messages: list[object]) -> list[ToolEvent]:
    events: list[ToolEvent] = []
    for message in messages:
        if isinstance(message, ChatMessageAssistant):
            for call in message.tool_calls or []:
                tool = _tool_name(call.function)
                if tool is not None:
                    events.append(
                        ToolEvent(
                            kind="call",
                            call_id=call.id,
                            tool=tool,
                            arguments=call.arguments,
                        )
                    )
        elif isinstance(message, ChatMessageTool):
            tool = _tool_name(message.function or "")
            if tool is None or message.tool_call_id is None:
                continue
            result = _tool_result_payload(message.content)
            if isinstance(result, dict):
                events.append(
                    ToolEvent(
                        kind="result",
                        call_id=message.tool_call_id,
                        tool=tool,
                        result=result,
                        transport_error=message.error is not None,
                    )
                )
    return events


def _tool_result_payload(content: object) -> dict[str, Any] | None:
    text: str | None = content if isinstance(content, str) else None
    if isinstance(content, list):
        blocks = [
            value
            for block in content
            if isinstance((value := getattr(block, "text", None)), str)
        ]
        if len(blocks) == 1:
            text = blocks[0]
    if text is None:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _tool_name(value: str) -> str | None:
    if value in {"get_join_plan", "validate_sql"}:
        return value
    for prefix in ("JoinLint_", "JoinLint__"):
        candidate = value.removeprefix(prefix)
        if candidate in {"get_join_plan", "validate_sql"}:
            return candidate
    return None
