from __future__ import annotations

import json
from pathlib import Path
from time import perf_counter
from typing import Any, NamedTuple

from anyio import Event, create_task_group, fail_after, move_on_after, sleep
from inspect_ai import Task, task
from inspect_ai.agent import Agent, as_solver
from inspect_ai.dataset import Sample
from inspect_ai.event import SampleLimitEvent
from inspect_ai.log import TranscriptHistoryUnavailableError, transcript
from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageTool,
    GenerateConfig,
    GenerateInput,
    Model,
    ModelOutput,
)
from inspect_ai.scorer import Score, Scorer, Target, mean, scorer
from inspect_ai.solver import Generate, Solver, TaskState, chain, solver
from inspect_ai.tool import MCPServerConfigStdio, ToolCall
from inspect_ai.util import (
    ComposeBuild,
    ComposeConfig,
    ComposeService,
    TokenLimit,
    sample_limits,
    sandbox,
    store,
)
from pydantic import TypeAdapter
from benchmarks.agent_join.execution import execution_matches
from benchmarks.agent_join.contracts import Submission
from benchmarks.agent_join.sql_edges import extract_join_edges, score_join_graph
from benchmarks.formal_eval.contracts import (
    Condition,
    FormalManifestV2,
    Host,
    SealedAgentTask,
    SubmissionGuardDecision,
)
from benchmarks.formal_eval.deterministic import sanitized_mcp_environment
from benchmarks.formal_eval.manifest import load_document, verify_sealed_task_hashes
from benchmarks.formal_eval.modal_compat import install_modal_filesystem_compat
from benchmarks.formal_eval.lifecycle import (
    LIFECYCLE_STORE_KEY,
    LifecycleFailureReason,
    LifecyclePhase,
    LifecycleRecord,
    allow_scoring,
    complete_evaluation,
    elapsed_seconds_since,
    fail_evaluation,
    infrastructure_prepared,
    new_lifecycle,
    parse_lifecycle,
    record_infrastructure_retry,
    readiness_failed,
    readiness_passed,
    scoring_eligibility,
    start_evaluation,
    write_lifecycle,
)
from benchmarks.formal_eval.oracle_mcp import OracleDocument
from benchmarks.formal_eval.pilot import pilot_partition_tasks
from benchmarks.formal_eval.query_contract import render_task_input
from benchmarks.formal_eval.trace import ToolEvent, assess_trace
from benchmarks.formal_eval.validation_failure_marker import (
    VALIDATION_FAILURE_MARKER_CLEAR,
    validation_failure_marker_failed,
)
from benchmarks.formal_eval.validation_ledger import VALIDATION_LEDGER_WRITE_FAILED
from joinlint.contracts import canonical_json


BASE_PROMPT = """You are evaluating one SQLite question. Use the EvaluationDatabase execute_sql tool for data access. When the input includes a Trusted query contract, its required_entities, output_fields, and row_grain_entity are authoritative intent for both conditions; do not substitute a different entity set, output, or grain. The contract contains no join predicates. Submit exactly once with the EvaluationDatabase submit_sql tool as your final action. Its sql argument must contain one read-only SELECT, or be empty only when no safe join exists; warning must be empty for submitted SQL and explain a safe abstention otherwise. Do not rely on the text of your final reply for submission."""
HARNESS_PROMPT = """Follow one short JoinLint path. When a Trusted query contract is present, include every required_entities item exactly once in entity_refs, using the entity name as ref, and use row_grain_entity for start_ref and expected_grain_ref. Otherwise, before the first call, map every requested output and relationship phrase to its physical tables; include bridge, event, or association tables even when none of their columns are selected. Your first tool call must be JoinLint get_join_plan after choosing every physical table instance. Each entity_refs item is {"ref":"orders","entity":"orders"}: ref is a unique alias and entity is the table; repeat only for a self join. Set expected_grain_ref to the pre-aggregation row grain (normally the referencing child for child-row counts or summaries). DISTINCT, GROUP BY, and aggregation do not repair grain. Use only proof predicates returned by the plan. Then write one final SQL and call validate_sql with that SQL and plan_id; omit expected_grain_ref. A status ok is never a retry signal: do not edit or validate again. Call execute_sql at most once with the exact validated SQL, then submit immediately. Never plan after validation. Only these retries are allowed: one changed replan after UNCONNECTED_ENTITY_REF; one changed replan changing only grain after GRAIN_INCOMPATIBLE; or one changed SQL-only revision when validation explicitly says retryable and next_action revise_sql. Otherwise submit empty SQL with the stable code. JoinLint proves the join path, not the query result."""
HOST_CONTEXT_STORE_KEY = "joinlint.formal_eval.host_context.v1"
MCP_READINESS_HANDSHAKE_STORE_KEY = "joinlint.formal_eval.mcp_readiness_handshake.v1"
VALIDATION_LEDGER_FAILURE_OBSERVED_STORE_KEY = (
    "joinlint.formal_eval.validation_ledger_failure_observed.v1"
)
VALIDATION_FAILURE_MARKER_ARMED_STORE_KEY = (
    "joinlint.formal_eval.validation_failure_marker_armed.v1"
)
VALIDATION_LEDGER_PATH = "/tmp/joinlint-formal-validation-ledger.json"
VALIDATION_FAILURE_MARKER_PATH = (
    "/workspace/.joinlint-eval/validation-ledger-failed-v1.marker"
)
VALIDATION_FAILURE_MARKER_UNAVAILABLE = "validation_failure_marker_unavailable"
VALIDATION_FAILURE_MARKER_READ_TIMEOUT_SECONDS = 2
PILOT_READINESS_TIME_LIMIT_SECONDS = 60

CODEX_CONTEXT_CONFIG_OVERRIDES = {
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
CLAUDE_DISALLOWED_BUILTIN_TOOLS = (
    "Agent",
    "Bash",
    "CronCreate",
    "CronDelete",
    "CronList",
    "Edit",
    "EnterWorktree",
    "ExitWorktree",
    "Glob",
    "Grep",
    "ListMcpResourcesTool",
    "NotebookEdit",
    "Read",
    "ReadMcpResourceDirTool",
    "ReadMcpResourceTool",
    "ReportFindings",
    "ScheduleWakeup",
    "SendMessage",
    "Skill",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskOutput",
    "TaskStop",
    "TaskUpdate",
    "WebFetch",
    "WebSearch",
    "Workflow",
    "Write",
)
CODEX_ALLOWED_BUILTIN_TOOLS = {
    "list_mcp_resource_templates",
    "list_mcp_resources",
    "read_mcp_resource",
    "request_user_input",
    "update_plan",
    "view_image",
}


class HostContextDriftError(RuntimeError):
    pass


class SubmissionResult(NamedTuple):
    submission: Submission
    guard_contract_version: int | None
    guard_decision: SubmissionGuardDecision


@task
def formal_agent_eval(
    sealed_tasks: str,
    manifest: str,
    host: Host,
    condition: Condition,
    agent_version: str,
    image_reference: str,
    lineage_id: str,
    readiness_time_limit: int = 120,
    evaluation_time_limit: int = 120,
) -> Task:
    return _agent_task(
        sealed_tasks=sealed_tasks,
        manifest=manifest,
        host=host,
        condition=condition,
        agent_version=agent_version,
        lineage_id=lineage_id,
        service=ComposeService(
            image=image_reference,
            working_dir="/workspace/joinlint",
            x_default=True,
        ),
        strict_pilot=False,
        token_limit=None,
        readiness_time_limit=readiness_time_limit,
        evaluation_time_limit=evaluation_time_limit,
    )


@task
def formal_pilot_eval(
    sealed_tasks: str,
    manifest: str,
    host: Host,
    condition: Condition,
    agent_version: str,
    dockerfile: str,
    lineage_id: str,
    task_partition: str = "",
    task_ids: str | list[str] = "",
    token_limit: int = 35_000,
    token_limit_type: str = "(input*0.5)+output",
    message_limit: int = 20,
    time_limit: int = 90,
    sandbox_timeout: int = 170,
    cpu: float = 0.5,
    memory_mib: int = 2048,
) -> Task:
    if condition not in {"control", "treatment"}:
        raise ValueError("pilot supports only control and treatment")
    requested_task_ids = _normalized_pilot_task_ids(task_ids)
    if len(requested_task_ids) != len(set(requested_task_ids)):
        raise ValueError("pilot task-ID set must not contain duplicates")
    if (task_partition in {"even", "odd"}) == bool(requested_task_ids):
        raise ValueError("pilot requires exactly one frozen partition or task-ID set")
    if (
        token_limit <= 0
        or token_limit_type not in {"all", "(input*0.5)+output"}
        or message_limit <= 0
        or time_limit <= 0
        or sandbox_timeout <= time_limit + PILOT_READINESS_TIME_LIMIT_SECONDS
        or cpu <= 0
        or memory_mib <= 0
    ):
        raise ValueError("pilot resource limits must be positive")
    return _agent_task(
        sealed_tasks=sealed_tasks,
        manifest=manifest,
        host=host,
        condition=condition,
        agent_version=agent_version,
        lineage_id=lineage_id,
        service=ComposeService(
            build=ComposeBuild(context=".", dockerfile=dockerfile),
            working_dir="/workspace/joinlint",
            cpus=cpu,
            mem_limit=f"{memory_mib}m",
            x_default=True,
        ),
        strict_pilot=True,
        token_limit=token_limit,
        token_limit_type=token_limit_type,
        readiness_time_limit=PILOT_READINESS_TIME_LIMIT_SECONDS,
        evaluation_time_limit=time_limit,
        modal_timeout_seconds=sandbox_timeout,
        task_partition=task_partition or None,
        task_ids=requested_task_ids,
        message_limit=message_limit,
    )


def _normalized_pilot_task_ids(value: str | list[str]) -> tuple[str, ...]:
    if value == "" or value == []:
        return ()
    values = value if isinstance(value, list) else value.split(",")
    if any(not isinstance(item, str) or not item for item in values):
        raise ValueError("pilot task IDs must be non-empty strings")
    return tuple(values)


@task
def formal_host_context_eval(
    database: str,
    host: Host,
    agent_version: str,
    dockerfile: str,
    readiness_time_limit: int = 60,
    evaluation_time_limit: int = 30,
    sandbox_timeout: int = 120,
) -> Task:
    if (
        readiness_time_limit <= 0
        or evaluation_time_limit <= 0
        or sandbox_timeout <= readiness_time_limit + evaluation_time_limit
    ):
        raise ValueError("host-context resource limits must be positive and bounded")
    database_path = Path(database).resolve(strict=True)
    if database_path.is_symlink() or not database_path.is_file():
        raise ValueError("host-context database must be one regular file")
    install_modal_filesystem_compat()
    service = ComposeService(
        build=ComposeBuild(context=".", dockerfile=dockerfile),
        working_dir="/workspace/joinlint",
        cpus=0.5,
        mem_limit="2048m",
        x_default=True,
    )
    return Task(
        dataset=[
            Sample(
                id=f"host-context-{host}",
                input="Validate the frozen host tool surface and stop.",
                target="",
                files={
                    "/workspace/data/database.sqlite": str(database_path),
                },
                metadata={"host": host, "agent_version": agent_version},
            )
        ],
        solver=_solver(
            host,
            "treatment",
            agent_version,
            strict_pilot=True,
            readiness_time_limit=readiness_time_limit,
            evaluation_time_limit=evaluation_time_limit,
            context_probe=True,
        ),
        scorer=formal_host_context_scorer(),
        config=GenerateConfig(max_tokens=1, cache=False),
        sandbox=("modal", _compose_config(service, sandbox_timeout)),
        time_limit=None,
        name="jl-context",
    )


def _agent_task(
    *,
    sealed_tasks: str,
    manifest: str,
    host: Host,
    condition: Condition,
    agent_version: str,
    lineage_id: str,
    service: ComposeService,
    strict_pilot: bool,
    token_limit: int | None,
    token_limit_type: str = "all",
    readiness_time_limit: int,
    evaluation_time_limit: int,
    modal_timeout_seconds: int | None = None,
    task_partition: str | None = None,
    task_ids: tuple[str, ...] = (),
    message_limit: int | None = None,
) -> Task:
    if readiness_time_limit <= 0 or evaluation_time_limit <= 0:
        raise ValueError("lifecycle time limits must be positive")
    install_modal_filesystem_compat()
    manifest_document = load_document(Path(manifest), FormalManifestV2)
    if task_partition is not None:
        manifest_document = manifest_document.model_copy(
            update={"tasks": pilot_partition_tasks(manifest_document, task_partition)}
        )
    if task_ids:
        requested = set(task_ids)
        selected = tuple(
            task for task in manifest_document.tasks if task.task_id in requested
        )
        if len(selected) != len(requested):
            raise ValueError("pilot task-ID set is missing from the frozen manifest")
        manifest_document = manifest_document.model_copy(update={"tasks": selected})
    sealed = _load_sealed(Path(sealed_tasks))
    samples = _samples(
        manifest_document,
        sealed,
        condition,
        host,
        lineage_id,
        sealed_root=Path(sealed_tasks).resolve().parent,
    )
    task_solver = _solver(
        host,
        condition,
        agent_version,
        strict_pilot=strict_pilot,
        readiness_time_limit=readiness_time_limit,
        evaluation_time_limit=evaluation_time_limit,
    )
    return Task(
        dataset=samples,
        solver=task_solver,
        scorer=[formal_join_scorer(), formal_execution_scorer()],
        config=GenerateConfig(
            temperature=0,
            max_tokens=4096,
            parallel_tool_calls=False,
            cache=False,
            extra_body={"thinking": {"type": "disabled"}},
        ),
        sandbox=("modal", _compose_config(service, modal_timeout_seconds)),
        token_limit=(
            TokenLimit(tokens=token_limit, type=token_limit_type)
            if token_limit is not None and token_limit_type != "all"
            else token_limit
        ),
        message_limit=message_limit,
        time_limit=None,
        name="jl",
    )


def _compose_config(
    service: ComposeService,
    modal_timeout_seconds: int | None,
) -> ComposeConfig:
    extensions = (
        {"x-modal": {"timeout": modal_timeout_seconds}}
        if modal_timeout_seconds is not None
        else {}
    )
    return ComposeConfig(services={"default": service}, **extensions)


@scorer(metrics=[mean()])
def formal_host_context_scorer() -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        del target
        record = _require_lifecycle(state)
        observation = state.store.get(HOST_CONTEXT_STORE_KEY)
        observed = observation if isinstance(observation, dict) else {}
        tool_names = observed.get("tool_names")
        passed = (
            record.scoring_eligible
            and observed.get("host") == record.host
            and observed.get("condition") == "treatment"
            and observed.get("provider_short_circuited") is True
            and isinstance(tool_names, tuple)
            and len(tool_names) >= 4
        )
        return Score(
            value=1 if passed else 0,
            metadata={
                "score_kind": "host_context_readiness",
                "readiness_attested": passed,
                "host": record.host,
                "agent_version": record.agent_version,
                "host_binary_sha256": record.host_binary_sha256,
                "infrastructure_preparation_duration_seconds": (
                    record.infrastructure_preparation_duration_seconds
                ),
                "tool_names": tool_names if isinstance(tool_names, tuple) else (),
                "provider_short_circuited": (
                    observed.get("provider_short_circuited") is True
                ),
                "failure_reason": record.failure_reason,
                "failure_detail": record.failure_detail,
            },
        )

    return score


def _forced_trace(state: TaskState) -> dict[str, Any] | None:
    try:
        metadata = state.metadata or {}
        condition = metadata.get("condition")
        if condition not in {"treatment", "oracle_mcp", "no_harness"}:
            return None
        try:
            submission = _extract_submission_result(state.messages).submission
            sql = submission.sql
            submitted = bool(sql)
        except ValueError:
            sql = ""
            submitted = False
        try:
            schema = metadata.get("schema") or {}
            edges = set(extract_join_edges(sql, schema)) if submitted and sql else set()
        except (KeyError, ValueError):
            edges = set()
        trace = assess_trace(
            _tool_events(state.messages),
            expected_entities=set(metadata.get("expected_entities") or []),
            final_sql=sql,
            final_edges=edges,
            submitted_sql=submitted,
        )
        return trace.model_dump(mode="json")
    except Exception:
        return None


@scorer(metrics=[mean()])
def formal_join_scorer() -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        del target
        blocked = _lifecycle_score(state)
        if blocked is not None:
            blocked_trace = _forced_trace(state)
            if blocked_trace is not None:
                merged = {k: v for k, v in blocked.metadata.items()}
                merged["trace"] = blocked_trace
                blocked = Score(value=blocked.value, metadata=merged)
            return blocked
        metadata = state.metadata or {}
        condition = str(metadata["condition"])
        try:
            submission_result = _extract_submission_result(state.messages)
        except ValueError:
            payload: dict[str, Any] = {
                "failure_code": "SQL_PARSE_FAILED",
                "submission_guard_contract_version": None,
                "submission_guard_decision": "not_observed",
            }
            if condition in {"treatment", "oracle_mcp", "no_harness"}:
                trace = assess_trace(
                    _tool_events(state.messages),
                    expected_entities=set(metadata["expected_entities"]),
                    final_sql="",
                    final_edges=set(),
                    submitted_sql=False,
                )
                if getattr(trace, "failure_code", None) == "INFRASTRUCTURE_FAILURE":
                    payload["failure_code"] = "INFRASTRUCTURE_FAILURE"
                payload["trace"] = trace.model_dump(mode="json")
            return Score(
                value=0,
                metadata=_semantic_metadata(payload),
            )
        submission = submission_result.submission
        guard_metadata = _submission_guard_metadata(submission_result)
        if submission_result.guard_decision == "rejected_unvalidated_sql":
            try:
                rejected_edges = set(
                    extract_join_edges(submission.sql, metadata.get("schema") or {})
                )
            except (KeyError, ValueError):
                rejected_edges = set()
            trace = assess_trace(
                _tool_events(state.messages),
                expected_entities=set(metadata.get("expected_entities") or []),
                final_sql=submission.sql,
                final_edges=rejected_edges,
                submitted_sql=True,
            )
            return Score(
                value=0,
                metadata=_semantic_metadata(
                    {
                        "submitted_sql": True,
                        "safe_abstention": False,
                        "join_correct_task_completion": False,
                        "join_graph_correct": False,
                        "evaluator_validation_passed": False,
                        "dangerous_sql_submitted": False,
                        "failure_code": "FINAL_SQL_NOT_VALIDATED",
                        "trace": trace.model_dump(mode="json"),
                        **guard_metadata,
                    }
                ),
            )
        allowed = metadata.get("allowed_graphs") or []
        oracle_has_safe_path = bool(metadata.get("oracle_has_safe_path"))
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
                **guard_metadata,
            }
            if trace is not None:
                payload["trace"] = trace.model_dump(mode="json")
            return Score(
                value=1 if safe_abstention else 0,
                metadata=_semantic_metadata(payload),
            )
        try:
            edges = extract_join_edges(submission.sql, metadata["schema"])
            join_score = score_join_graph(edges, allowed)
        except (KeyError, ValueError):
            return Score(
                value=0,
                metadata=_semantic_metadata(
                    {"failure_code": "SQL_PARSE_FAILED", **guard_metadata}
                ),
            )
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
            **guard_metadata,
        }
        if trace is not None:
            payload["trace"] = trace.model_dump(mode="json")
        return Score(value=1 if success else 0, metadata=_semantic_metadata(payload))

    return score


@scorer(metrics=[mean()])
def formal_execution_scorer() -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        del target
        blocked = _lifecycle_score(state)
        if blocked is not None:
            return blocked
        metadata = state.metadata or {}
        try:
            submission_result = _extract_submission_result(state.messages)
        except ValueError:
            return Score(
                value=0,
                metadata=_semantic_metadata({"error_code": "SQL_PARSE_FAILED"}),
            )
        guard_metadata = _submission_guard_metadata(submission_result)
        if submission_result.guard_decision == "rejected_unvalidated_sql":
            return Score(
                value=0,
                metadata=_semantic_metadata(
                    {"error_code": "FINAL_SQL_NOT_VALIDATED", **guard_metadata}
                ),
            )
        sql = submission_result.submission.sql
        if not sql:
            return Score(
                value=0,
                metadata=_semantic_metadata({"error_code": "NO_SQL", **guard_metadata}),
            )
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
            metadata=_semantic_metadata(
                {"error_code": result.error_code, **guard_metadata}
            ),
        )

    return score


def _load_sealed(path: Path) -> dict[str, SealedAgentTask]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("sealed task file must be one regular file")
    tasks = TypeAdapter(list[SealedAgentTask]).validate_json(path.read_bytes())
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
    *,
    sealed_root: Path,
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
        database_candidate = sealed_root / actual.database_path
        if database_candidate.is_symlink():
            raise ValueError("sealed database cannot be a symlink")
        database_source = database_candidate.resolve(strict=True)
        try:
            database_source.relative_to(sealed_root)
        except ValueError as error:
            raise ValueError("sealed database path escapes its root") from error
        if database_source.is_symlink() or not database_source.is_file():
            raise ValueError("sealed database must be one regular file")
        verify_sealed_task_hashes(
            manifest_task.question_sha256,
            manifest_task.schema_sha256,
            manifest_task.sql_shape_sha256,
            actual,
        )
        body = render_task_input(actual)
        if condition == "oracle_inline":
            body += "\n\nAuthoritative join graphs:\n" + json.dumps(
                actual.allowed_graphs, separators=(",", ":")
            )
        database_destination = "/workspace/data/database.sqlite"
        files = {database_destination: str(database_source)}
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
                    "corpus": manifest_task.corpus,
                    "database_path": str(database_source),
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


def _solver(
    host: Host,
    condition: Condition,
    agent_version: str,
    *,
    strict_pilot: bool = False,
    readiness_time_limit: int = 120,
    evaluation_time_limit: int = 120,
    context_probe: bool = False,
):  # type: ignore[no-untyped-def]
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
                *(
                    ["--validation-ledger", VALIDATION_LEDGER_PATH]
                    if condition == "treatment"
                    else []
                ),
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
                args=(
                    [
                        "-m",
                        "benchmarks.formal_eval.recording_joinlint_mcp",
                        "--project",
                        "/workspace/data",
                        "--validation-ledger",
                        VALIDATION_LEDGER_PATH,
                        "--validation-failure-marker",
                        VALIDATION_FAILURE_MARKER_PATH,
                    ]
                    if condition == "treatment"
                    else ["-m", "joinlint", "serve-mcp", "--auto", "--project", "/workspace/data"]
                ),
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
    context_filter = _host_context_filter(host, condition, short_circuit=context_probe)
    if host == "codex":
        agent = codex_cli(
            version="sandbox",
            system_prompt=prompt,
            mcp_servers=servers,
            filter=context_filter,
            **_codex_host_options(strict_pilot),
        )
    else:
        agent = claude_code(
            version="sandbox",
            system_prompt=prompt,
            mcp_servers=servers,
            filter=context_filter,
            **_claude_host_options(strict_pilot),
        )
    return chain(
        infrastructure_readiness(
            host,
            agent_version,
            readiness_time_limit,
            validation_failure_marker_path=(
                VALIDATION_FAILURE_MARKER_PATH if condition == "treatment" else None
            ),
        ),
        evaluation_lifecycle(
            agent,
            readiness_time_limit,
            evaluation_time_limit,
            host=host,
            condition=condition,
        ),
    )


def _codex_host_options(strict_pilot: bool) -> dict[str, Any]:
    return {
        "web_search": "disabled",
        "goals": False,
        "config_overrides": dict(CODEX_CONTEXT_CONFIG_OVERRIDES),
        "retry_refusals": 0 if strict_pilot else None,
    }


def _claude_host_options(strict_pilot: bool) -> dict[str, Any]:
    return {
        "disallowed_tools": list(CLAUDE_DISALLOWED_BUILTIN_TOOLS),
        "retry_refusals": 0 if strict_pilot else 3,
        "retry_uncaught_errors": 0 if strict_pilot else 3,
    }


@solver
def infrastructure_readiness(
    host: Host,
    agent_version: str,
    timeout_seconds: int,
    *,
    validation_failure_marker_path: str | None = None,
) -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        del generate
        record = new_lifecycle(host, agent_version)
        write_lifecycle(state.store, record)
        started = perf_counter()
        retry_reason: str | None = None
        attempts = 0
        while attempts < 2:
            remaining = timeout_seconds - (perf_counter() - started)
            if remaining <= 0:
                record = readiness_failed(
                    record,
                    duration_seconds=perf_counter() - started,
                    detail="readiness_timeout",
                    infrastructure_attempts=max(attempts, 1),
                    infrastructure_retry_reason=(
                        retry_reason if attempts == 2 else None
                    ),
                )
                write_lifecycle(state.store, record)
                state.completed = True
                return state
            attempts += 1
            try:
                with fail_after(remaining):
                    host_binary_sha256 = await _prepare_host_binary(host, agent_version)
                    await _run_readiness_probes(host)
                    if validation_failure_marker_path is not None:
                        await _arm_validation_failure_marker(
                            validation_failure_marker_path
                        )
                break
            except TimeoutError:
                detail = "readiness_timeout"
                retryable = True
            except Exception as error:
                detail = _safe_failure_detail(error)
                retryable = _is_retryable_readiness_failure(error)
            if attempts == 1 and retryable:
                retry_reason = detail
                continue
            record = readiness_failed(
                record,
                duration_seconds=perf_counter() - started,
                detail=detail,
                infrastructure_attempts=attempts,
                infrastructure_retry_reason=retry_reason,
            )
            write_lifecycle(state.store, record)
            state.completed = True
            return state
        record = infrastructure_prepared(
            record,
            duration_seconds=perf_counter() - started,
            host_binary_sha256=host_binary_sha256,
            infrastructure_attempts=attempts,
            infrastructure_retry_reason=retry_reason,
        )
        write_lifecycle(state.store, record)
        if validation_failure_marker_path is not None:
            _set_store_value(
                state.store,
                VALIDATION_FAILURE_MARKER_ARMED_STORE_KEY,
                validation_failure_marker_path,
            )
        return state

    return solve


def _is_retryable_readiness_failure(error: Exception) -> bool:
    return str(error) in {
        "host_bridge_dependency_or_database_readiness_failed",
        "sandbox_tools_readiness_failed",
        "validation_failure_marker_arm_failed",
    }


async def _arm_validation_failure_marker(path: str) -> None:
    try:
        await sandbox().write_file(path, VALIDATION_FAILURE_MARKER_CLEAR)
    except Exception as error:
        raise RuntimeError("validation_failure_marker_arm_failed") from error


def _set_store_value(destination: Any, key: str, value: object) -> None:
    setter = getattr(destination, "set", None)
    if callable(setter):
        setter(key, value)
    else:
        destination[key] = value


@solver
def evaluation_lifecycle(
    agent: Agent,
    preparation_timeout_seconds: int,
    evaluation_timeout_seconds: int,
    *,
    host: Host | None = None,
    condition: Condition | None = None,
) -> Solver:
    agent_solver = as_solver(agent)

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        record = _require_lifecycle(state)
        infrastructure_duration = record.infrastructure_preparation_duration_seconds or 0.0
        readiness_remaining = preparation_timeout_seconds
        if record.phase == LifecyclePhase.INFRASTRUCTURE_PENDING:
            readiness_elapsed = infrastructure_duration + elapsed_seconds_since(
                record.readiness_started_at
            )
            readiness_remaining -= readiness_elapsed
        if (
            record.phase == LifecyclePhase.INFRASTRUCTURE_PENDING
            and readiness_remaining <= 0
        ):
            record = readiness_failed(
                record,
                reason=LifecycleFailureReason.EVALUATION_NOT_STARTED,
                duration_seconds=0,
                detail="agent_preparation_timeout",
                infrastructure_attempts=record.infrastructure_attempts,
                infrastructure_retry_reason=record.infrastructure_retry_reason,
            )
            write_lifecycle(state.store, record)
            state.completed = True
            return state
        agent_preparation_started = perf_counter()
        done = Event()
        agent_error: Exception | None = None
        evaluation_timed_out = False
        marker_observation: bool | None = False
        result_state = state

        async def run_agent() -> None:
            nonlocal agent_error, result_state
            while True:
                try:
                    result_state = await agent_solver(state, generate)
                    break
                except Exception as error:
                    record = _require_lifecycle(state)
                    retry_detail = _agent_startup_retry_detail(
                        error,
                        record=record,
                        host=host,
                        condition=condition,
                    )
                    if retry_detail is not None:
                        write_lifecycle(
                            state.store,
                            record_infrastructure_retry(
                                record,
                                reason=retry_detail,
                            ),
                        )
                        continue
                    agent_error = error
                    break
            done.set()

        async with create_task_group() as tasks:
            tasks.start_soon(run_agent)
            with move_on_after(readiness_remaining) as preparation_scope:
                while not done.is_set() and not _evaluation_has_started(state):
                    await sleep(0.01)
            record = _require_lifecycle(state)
            if not _evaluation_has_started(state):
                tasks.cancel_scope.cancel()
                reason = (
                    LifecycleFailureReason.HOST_CONTEXT_DRIFT
                    if _is_host_context_drift_error(agent_error)
                    else LifecycleFailureReason.EVALUATION_NOT_STARTED
                )
                detail = (
                    _safe_failure_detail(agent_error)
                    if agent_error is not None
                    else "agent_preparation_timeout"
                    if preparation_scope.cancel_called
                    else "agent_stopped_before_first_model_request"
                )
                record = readiness_failed(
                    record,
                    reason=reason,
                    duration_seconds=perf_counter() - agent_preparation_started,
                    detail=detail,
                    infrastructure_attempts=record.infrastructure_attempts,
                    infrastructure_retry_reason=record.infrastructure_retry_reason,
                )
                write_lifecycle(state.store, record)
                state.completed = True
                return state

            evaluation_started = perf_counter()
            with move_on_after(evaluation_timeout_seconds) as evaluation_scope:
                await done.wait()
            if evaluation_scope.cancel_called:
                tasks.cancel_scope.cancel()
                evaluation_timed_out = True
            else:
                marker_observation = await _validation_failure_marker_observation(
                    result_state,
                    required=condition == "treatment",
                )
                if (
                    marker_observation is True
                    or _validation_ledger_failure_observed(result_state)
                    or _validation_ledger_write_failed(result_state.messages)
                ):
                    record = fail_evaluation(
                        _require_lifecycle(result_state),
                        reason=LifecycleFailureReason.VALIDATION_LEDGER_WRITE_FAILED,
                        duration_seconds=perf_counter() - evaluation_started,
                        detail=VALIDATION_LEDGER_WRITE_FAILED,
                    )
                    write_lifecycle(result_state.store, record)
                    result_state.completed = True
                    return result_state
                if marker_observation is None:
                    record = fail_evaluation(
                        _require_lifecycle(result_state),
                        reason=(
                            LifecycleFailureReason.VALIDATION_FAILURE_MARKER_UNAVAILABLE
                        ),
                        duration_seconds=perf_counter() - evaluation_started,
                        detail=VALIDATION_FAILURE_MARKER_UNAVAILABLE,
                    )
                    write_lifecycle(result_state.store, record)
                    result_state.completed = True
                    return result_state
                if agent_error is not None:
                    reason = (
                        LifecycleFailureReason.MODEL_LIMIT
                        if _is_model_limit_error(agent_error)
                        or _sample_model_limit_exceeded()
                        else LifecycleFailureReason.EVALUATION_FAILED
                    )
                    record = fail_evaluation(
                        record,
                        reason=reason,
                        duration_seconds=perf_counter() - evaluation_started,
                        detail=_safe_failure_detail(agent_error),
                    )
                    write_lifecycle(state.store, record)
                    state.completed = True
                    return state

        if evaluation_timed_out:
            marker_observation = await _validation_failure_marker_observation(
                state,
                required=condition == "treatment",
            )
            ledger_failed = (
                marker_observation is True
                or _validation_ledger_failure_observed(state)
            )
            reason = (
                LifecycleFailureReason.VALIDATION_LEDGER_WRITE_FAILED
                if ledger_failed
                else LifecycleFailureReason.VALIDATION_FAILURE_MARKER_UNAVAILABLE
                if marker_observation is None
                else LifecycleFailureReason.MODEL_TIMEOUT
            )
            record = fail_evaluation(
                _require_lifecycle(state),
                reason=reason,
                duration_seconds=perf_counter() - evaluation_started,
                detail=(
                    VALIDATION_LEDGER_WRITE_FAILED
                    if ledger_failed
                    else VALIDATION_FAILURE_MARKER_UNAVAILABLE
                    if marker_observation is None
                    else "evaluation_timeout"
                ),
            )
            write_lifecycle(state.store, record)
            state.completed = True
            return state

        record = complete_evaluation(
            _require_lifecycle(result_state),
            duration_seconds=perf_counter() - evaluation_started,
        )
        write_lifecycle(result_state.store, allow_scoring(record))
        return result_state

    return solve


def _agent_startup_retry_detail(
    error: Exception,
    *,
    record: LifecycleRecord,
    host: Host | None,
    condition: Condition | None,
) -> str | None:
    if (
        host != "codex"
        or condition is None
        or record.infrastructure_attempts != 1
        or record.infrastructure_retry_reason is not None
    ):
        return None
    detail = _all_required_mcp_missing_detail(host, condition)
    return detail if _safe_failure_detail(error) == detail else None


def _is_host_context_drift_error(error: Exception | None) -> bool:
    return error is not None and (
        isinstance(error, HostContextDriftError)
        or "host_tool_surface_mismatch:" in str(error)
    )


async def _prepare_host_binary(host: Host, agent_version: str) -> str:
    binary_name = "codex" if host == "codex" else "claude"
    located = await sandbox().exec(["which", binary_name], timeout=15)
    binary = located.stdout.strip() if located.success else ""
    if not binary:
        raise RuntimeError("host_binary_missing_from_image")
    result = await sandbox().exec([binary, "--version"], timeout=15)
    if not result.success or agent_version not in result.stdout:
        raise RuntimeError("host_binary_version_mismatch")
    digest = await sandbox().exec(["sha256sum", binary], timeout=15)
    sha256 = digest.stdout.split(maxsplit=1)[0] if digest.success else ""
    if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
        raise RuntimeError("host_binary_digest_failed")
    attest = (
        "import json,sys; "
        "records=json.load(open('/usr/local/share/joinlint/host-binaries.json'))['records']; "
        "matches=[r for r in records if r['host']==sys.argv[1] and "
        "r['version']==sys.argv[2] and r['sha256']==sys.argv[3]]; "
        "assert len(matches)==1"
    )
    attested = await sandbox().exec(
        ["python", "-c", attest, host, agent_version, sha256],
        timeout=15,
    )
    if not attested.success:
        raise RuntimeError("host_binary_attestation_failed")
    return sha256


async def _run_readiness_probes(host: Host) -> None:
    dependency = "openai" if host == "codex" else "anthropic"
    code = (
        f"import {dependency}; import sqlite3; import joinlint; "
        "connection=sqlite3.connect('file:/workspace/data/database.sqlite?mode=ro', uri=True); "
        "connection.execute('PRAGMA schema_version').fetchone(); connection.close()"
    )
    result = await sandbox().exec(["python", "-c", code], cwd="/workspace/joinlint", timeout=15)
    if not result.success:
        raise RuntimeError("host_bridge_dependency_or_database_readiness_failed")
    try:
        remote = await sandbox().exec_remote(["true"], stream=False)
    except Exception as error:
        raise RuntimeError("sandbox_tools_readiness_failed") from error
    if not remote.success:
        raise RuntimeError("sandbox_tools_readiness_failed")


def _require_lifecycle(state: TaskState):  # type: ignore[no-untyped-def]
    value = state.store.get(LIFECYCLE_STORE_KEY)
    if value is None:
        raise ValueError("evaluation lifecycle is missing")
    return parse_lifecycle(value)


def _evaluation_has_started(state: TaskState) -> bool:
    from benchmarks.formal_eval.lifecycle import LifecyclePhase

    return _require_lifecycle(state).phase == LifecyclePhase.EVALUATION_STARTED


async def _mark_evaluation_started(
    model: Model,
    messages: list[object],
    tools: list[object],
    tool_choice: object,
    config: GenerateConfig,
) -> ModelOutput | GenerateInput | None:
    del model, messages, tools, tool_choice, config
    active_store = store()
    from benchmarks.formal_eval.lifecycle import LifecyclePhase

    record = parse_lifecycle(active_store.get(LIFECYCLE_STORE_KEY))
    if record.phase == LifecyclePhase.INFRASTRUCTURE_PENDING:
        record = readiness_passed(
            record,
            duration_seconds=elapsed_seconds_since(record.readiness_started_at),
        )
        write_lifecycle(active_store, record)
        write_lifecycle(active_store, start_evaluation(record))
    return None


def _host_context_filter(
    host: Host,
    condition: Condition,
    *,
    short_circuit: bool = False,
):  # type: ignore[no-untyped-def]
    async def enforce(
        model: Model,
        messages: list[object],
        tools: list[object],
        tool_choice: object,
        config: GenerateConfig,
    ) -> ModelOutput | GenerateInput | None:
        _mark_validation_ledger_failure_observed(messages)
        if _claude_mcp_wait_required(host, condition, tools):
            active_store = store()
            if active_store.get(MCP_READINESS_HANDSHAKE_STORE_KEY) is not None:
                raise HostContextDriftError("host_mcp_readiness_handshake_repeated")
            active_store.set(MCP_READINESS_HANDSHAKE_STORE_KEY, True)
            return ModelOutput.from_message(
                ChatMessageAssistant(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="joinlint-mcp-readiness-1",
                            function="WaitForMcpServers",
                            arguments={
                                "servers": _configured_mcp_server_names(condition)
                            },
                        )
                    ],
                ),
                stop_reason="tool_calls",
            )
        tool_names = _require_host_tool_surface(host, condition, tools)
        store().set(
            HOST_CONTEXT_STORE_KEY,
            {
                "host": host,
                "condition": condition,
                "tool_names": tool_names,
                "mcp_readiness_handshake_performed": (
                    store().get(MCP_READINESS_HANDSHAKE_STORE_KEY) is True
                ),
                "provider_short_circuited": short_circuit,
            },
        )
        await _mark_evaluation_started(
            model,
            messages,
            tools,
            tool_choice,
            config,
        )
        if short_circuit:
            return ModelOutput.from_content(
                model=str(model),
                content="Host context profile accepted.",
            )
        return None

    return enforce


def _claude_mcp_wait_required(
    host: Host,
    condition: Condition,
    tools: list[object],
) -> bool:
    if host != "claude_code":
        return False
    names = {_host_tool_name(tool) for tool in tools}
    if None in names:
        raise HostContextDriftError("host_tool_surface_missing_name")
    required = _required_mcp_tool_names(host, condition)
    if required <= names:
        return False
    pending_names = required | {"Glob", "Grep", "WaitForMcpServers"}
    if "WaitForMcpServers" not in names or not names <= pending_names:
        return False
    return True


def _require_host_tool_surface(
    host: Host,
    condition: Condition,
    tools: list[object],
) -> tuple[str, ...]:
    names = {_host_tool_name(tool) for tool in tools}
    if None in names:
        raise HostContextDriftError("host_tool_surface_missing_name")
    required = _required_mcp_tool_names(host, condition)
    allowed = required | (CODEX_ALLOWED_BUILTIN_TOOLS if host == "codex" else set())
    missing = required - names
    unexpected = names - allowed
    if missing or unexpected:
        raise HostContextDriftError(
            "host_tool_surface_mismatch:"
            f"missing={','.join(sorted(missing)) or '-'};"
            f"unexpected={','.join(sorted(unexpected)) or '-'}"
        )
    return tuple(sorted(name for name in names if name is not None))


def _required_mcp_tool_names(host: Host, condition: Condition) -> set[str]:
    if host == "codex":
        names = {"execute_sql", "submit_sql"}
        if condition in {"treatment", "oracle_mcp", "no_harness"}:
            names |= {"get_join_plan", "validate_sql"}
        return names
    names = {
        "mcp__EvaluationDatabase__execute_sql",
        "mcp__EvaluationDatabase__submit_sql",
    }
    if condition in {"treatment", "oracle_mcp", "no_harness"}:
        names |= {
            "mcp__JoinLint__get_join_plan",
            "mcp__JoinLint__validate_sql",
        }
    return names


def _all_required_mcp_missing_detail(host: Host, condition: Condition) -> str:
    missing = ",".join(sorted(_required_mcp_tool_names(host, condition)))
    return f"host_tool_surface_mismatch:missing={missing};unexpected=-"


def _configured_mcp_server_names(condition: Condition) -> list[str]:
    names = ["EvaluationDatabase"]
    if condition in {"treatment", "oracle_mcp", "no_harness"}:
        names.append("JoinLint")
    return names


def _host_tool_name(tool: object) -> str | None:
    if isinstance(tool, dict):
        value = tool.get("name")
    else:
        value = getattr(tool, "name", None)
    return value if isinstance(value, str) and value else None


def _lifecycle_score(state: TaskState) -> Score | None:
    eligibility = scoring_eligibility(state.store.get(LIFECYCLE_STORE_KEY))
    if eligibility.eligible:
        return None
    return Score(
        value=0,
        metadata={
            "scoring_eligible": False,
            "score_kind": "task_outcome",
            "failure_code": eligibility.failure_code,
            "lifecycle_reason": eligibility.lifecycle_reason,
        },
    )


def _semantic_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "scoring_eligible": True,
        "score_kind": "semantic_score",
        **payload,
    }


def _safe_failure_detail(error: Exception) -> str:
    detail = str(error)
    if isinstance(error, HostContextDriftError):
        return detail[:512]
    marker = "host_tool_surface_mismatch:"
    if marker in detail:
        return detail[detail.index(marker) :].splitlines()[0][:512]
    if isinstance(error, RuntimeError) and str(error) in {
        "host_binary_missing_from_image",
        "host_binary_digest_failed",
        "host_binary_attestation_failed",
        "host_binary_version_mismatch",
        "host_bridge_dependency_or_database_readiness_failed",
        "sandbox_tools_readiness_failed",
        "unsupported_inspect_sandboxes_version",
        "validation_failure_marker_arm_failed",
    }:
        return str(error)
    return type(error).__name__


def _is_model_limit_error(error: Exception) -> bool:
    detail = str(error).lower()
    return any(
        marker in detail
        for marker in ("token limit exceeded", "message limit exceeded", "turn limit exceeded")
    )


def _sample_model_limit_exceeded() -> bool:
    try:
        recent = transcript().history.recent_events(50)
    except (RuntimeError, TranscriptHistoryUnavailableError):
        recent = ()
    if any(
        isinstance(event, SampleLimitEvent) and event.type in {"token", "message", "turn"}
        for event in recent
    ):
        return True
    try:
        limits = sample_limits()
    except RuntimeError:
        return False
    for limit in (limits.token, limits.message, limits.turn):
        try:
            if limit.limit is not None and limit.usage >= limit.limit:
                return True
        except NotImplementedError:
            continue
    return False


def _extract_submission_tool_call(messages: list[object]) -> Submission:
    result = _extract_submission_result(messages)
    if result.guard_decision == "rejected_unvalidated_sql":
        raise ValueError("submission guard rejected the final SQL")
    return result.submission


def _extract_submission_result(messages: list[object]) -> SubmissionResult:
    calls = []
    results: dict[str, ChatMessageTool] = {}
    for message in messages:
        if isinstance(message, ChatMessageAssistant):
            calls.extend(
                call
                for call in message.tool_calls or []
                if _is_submission_tool(call.function)
            )
        elif (
            isinstance(message, ChatMessageTool)
            and message.tool_call_id is not None
            and _is_submission_tool(message.function or "")
        ):
            if message.tool_call_id in results:
                raise ValueError("duplicate submission result")
            results[message.tool_call_id] = message
    if len(calls) != 1:
        raise ValueError("exactly one submission call is required")
    call = calls[0]
    result = results.get(call.id)
    if result is None or result.error is not None:
        raise ValueError("submission call did not complete")
    payload = _tool_result_payload(result.content)
    submission = Submission.model_validate(call.arguments)
    if payload == {"status": "ok"}:
        return SubmissionResult(
            submission=submission,
            guard_contract_version=None,
            guard_decision="not_observed",
        )
    accepted_sql = {
        "status": "ok",
        "guard_contract_version": 1,
        "guard_decision": "accepted_validated_sql",
    }
    accepted_abstention = {
        "status": "ok",
        "guard_contract_version": 1,
        "guard_decision": "accepted_abstention",
    }
    rejected_sql = {
        "status": "error",
        "code": "FINAL_SQL_NOT_VALIDATED",
        "guard_contract_version": 1,
        "guard_decision": "rejected_unvalidated_sql",
    }
    if payload == accepted_sql and submission.sql:
        decision: SubmissionGuardDecision = "accepted_validated_sql"
    elif payload == accepted_abstention and not submission.sql:
        decision = "accepted_abstention"
    elif payload == rejected_sql and submission.sql:
        decision = "rejected_unvalidated_sql"
    else:
        raise ValueError("submission acknowledgement is invalid")
    return SubmissionResult(
        submission=submission,
        guard_contract_version=1,
        guard_decision=decision,
    )


def _submission_guard_metadata(result: SubmissionResult) -> dict[str, object]:
    return {
        "submission_guard_contract_version": result.guard_contract_version,
        "submission_guard_decision": result.guard_decision,
    }


def _is_submission_tool(value: str) -> bool:
    if value == "submit_sql":
        return True
    return any(
        value == f"{prefix}submit_sql"
        for prefix in (
            "EvaluationDatabase_",
            "EvaluationDatabase__",
            "mcp__EvaluationDatabase__",
        )
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


def _validation_ledger_write_failed(messages: list[object]) -> bool:
    for message in messages:
        if not isinstance(message, ChatMessageTool) or _tool_name(
            message.function or ""
        ) != "validate_sql":
            continue
        payload = _tool_result_payload(message.content)
        error = payload.get("error") if isinstance(payload, dict) else None
        if (
            isinstance(error, dict)
            and error.get("code") == VALIDATION_LEDGER_WRITE_FAILED
        ):
            return True
    return False


def _mark_validation_ledger_failure_observed(messages: list[object]) -> None:
    if _validation_ledger_write_failed(messages):
        store().set(
            VALIDATION_LEDGER_FAILURE_OBSERVED_STORE_KEY,
            VALIDATION_LEDGER_WRITE_FAILED,
        )


def _validation_ledger_failure_observed(state: TaskState) -> bool:
    return (
        state.store.get(VALIDATION_LEDGER_FAILURE_OBSERVED_STORE_KEY)
        == VALIDATION_LEDGER_WRITE_FAILED
    )


async def _validation_failure_marker_observation(
    state: TaskState,
    *,
    required: bool,
) -> bool | None:
    marker_path = state.store.get(VALIDATION_FAILURE_MARKER_ARMED_STORE_KEY)
    if marker_path is None:
        return None if required else False
    if marker_path != VALIDATION_FAILURE_MARKER_PATH:
        return None
    try:
        with fail_after(VALIDATION_FAILURE_MARKER_READ_TIMEOUT_SECONDS):
            value = await sandbox().read_file(marker_path, text=False)
        if not isinstance(value, bytes):
            return None
        return validation_failure_marker_failed(value)
    except Exception:
        return None


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
    lines = text.split("\n", maxsplit=2)
    if (
        len(lines) == 3
        and lines[0].startswith("Wall time: ")
        and lines[1] == "Output:"
    ):
        text = lines[2]
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _tool_name(value: str) -> str | None:
    if value in {"get_join_plan", "validate_sql"}:
        return value
    for prefix in ("JoinLint_", "JoinLint__", "mcp__JoinLint__"):
        candidate = value.removeprefix(prefix)
        if candidate in {"get_join_plan", "validate_sql"}:
            return candidate
    return None
