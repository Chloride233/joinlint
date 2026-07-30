from __future__ import annotations

import json
from pathlib import Path
from time import perf_counter
from typing import Any

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
from inspect_ai.tool import MCPServerConfigStdio
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
)
from benchmarks.formal_eval.deterministic import sanitized_mcp_environment
from benchmarks.formal_eval.manifest import load_document, verify_sealed_task_hashes
from benchmarks.formal_eval.modal_compat import install_modal_filesystem_compat
from benchmarks.formal_eval.lifecycle import (
    LIFECYCLE_STORE_KEY,
    LifecycleFailureReason,
    allow_scoring,
    complete_evaluation,
    elapsed_seconds_since,
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
from benchmarks.formal_eval.oracle_mcp import OracleDocument
from benchmarks.formal_eval.pilot import pilot_partition_tasks
from benchmarks.formal_eval.trace import ToolEvent, assess_trace
from joinlint.contracts import canonical_json


BASE_PROMPT = """You are evaluating one SQLite question. Use the EvaluationDatabase execute_sql tool for data access. Submit exactly once with the EvaluationDatabase submit_sql tool as your final action. Its sql argument must contain one read-only SELECT, or be empty only when no safe join exists; warning must be empty for submitted SQL and explain a safe abstention otherwise. Do not rely on the text of your final reply for submission."""
HARNESS_PROMPT = """The schema is already provided, so your first tool call must be JoinLint get_join_plan. Call get_join_plan exactly once after choosing every intended physical table instance. Each entity_refs item must be exactly {"ref":"orders","entity":"orders"}: ref is a unique request-local alias and entity is the physical table name; repeat an entity only for a genuine self join. Set expected_grain_ref to the instance whose unique key must remain one row per output row before aggregation. For a many-to-one join, the referencing child normally preserves its grain; aggregation, DISTINCT, and GROUP BY do not restore grain in Stage 1. If planning returns GRAIN_INCOMPATIBLE, COMPOUND_FANOUT, NO_VERIFIED_PATH, or any other non-ok status, do not retry variants or guess: submit empty SQL with the stable code in warning. Use only returned proof predicates. Call JoinLint validate_sql with the exact final SQL and returned plan_id, then call execute_sql only after validation passes. If validation is not ok, do not execute and submit empty SQL with the stable code. JoinLint proof is not query correctness."""


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
    sandbox_timeout: int = 150,
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
        or sandbox_timeout <= time_limit
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
        readiness_time_limit=sandbox_timeout - time_limit,
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
def formal_modal_readiness_eval(
    database: str,
    host: Host,
    agent_version: str,
    dockerfile: str,
    readiness_time_limit: int = 60,
    sandbox_timeout: int = 120,
) -> Task:
    if readiness_time_limit <= 0 or sandbox_timeout <= readiness_time_limit:
        raise ValueError("readiness resource limits must be positive")
    database_path = Path(database).resolve(strict=True)
    if database_path.is_symlink() or not database_path.is_file():
        raise ValueError("readiness database must be one regular file")
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
                id="modal-readiness",
                input="Infrastructure readiness only; no model request is permitted.",
                target="",
                files={"/workspace/data/database.sqlite": str(database_path)},
            )
        ],
        solver=infrastructure_readiness(host, agent_version, readiness_time_limit),
        scorer=formal_modal_readiness_scorer(),
        config=GenerateConfig(max_tokens=1, cache=False),
        sandbox=("modal", _compose_config(service, sandbox_timeout)),
        token_limit=1,
        time_limit=None,
        name="jl-readiness",
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
def formal_modal_readiness_scorer() -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        del target
        record = _require_lifecycle(state)
        passed = (
            record.infrastructure_prepared_at is not None
            and record.host_binary_sha256 is not None
            and record.failure_reason is None
        )
        return Score(
            value=1 if passed else 0,
            metadata={
                "score_kind": "infrastructure_readiness",
                "readiness_attested": passed,
                "host": record.host,
                "agent_version": record.agent_version,
                "host_binary_sha256": record.host_binary_sha256,
                "infrastructure_preparation_duration_seconds": (
                    record.infrastructure_preparation_duration_seconds
                ),
                "failure_reason": record.failure_reason,
                "failure_detail": record.failure_detail,
            },
        )

    return score


@scorer(metrics=[mean()])
def formal_join_scorer() -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        del target
        blocked = _lifecycle_score(state)
        if blocked is not None:
            return blocked
        metadata = state.metadata or {}
        condition = str(metadata["condition"])
        try:
            submission = _extract_submission_tool_call(state.messages)
        except ValueError:
            payload: dict[str, Any] = {"failure_code": "SQL_PARSE_FAILED"}
            if condition in {"treatment", "oracle_mcp", "no_harness"}:
                trace = assess_trace(
                    _tool_events(state.messages),
                    expected_entities=set(metadata["expected_entities"]),
                    final_sql="",
                    final_edges=set(),
                    submitted_sql=False,
                )
                payload["trace"] = trace.model_dump(mode="json")
            return Score(
                value=0,
                metadata=_semantic_metadata(payload),
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
                metadata=_semantic_metadata({"failure_code": "SQL_PARSE_FAILED"}),
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
            sql = _extract_submission_tool_call(state.messages).sql
        except ValueError:
            return Score(
                value=0,
                metadata=_semantic_metadata({"error_code": "SQL_PARSE_FAILED"}),
            )
        if not sql:
            return Score(
                value=0,
                metadata=_semantic_metadata({"error_code": "NO_SQL"}),
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
            metadata=_semantic_metadata({"error_code": result.error_code}),
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
        body = f"Question:\n{actual.question}\n\nSchema:\n{actual.schema_text}"
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
        agent = codex_cli(
            version="sandbox",
            system_prompt=prompt,
            mcp_servers=servers,
            web_search="disabled",
            goals=False,
            retry_refusals=0 if strict_pilot else None,
            filter=_mark_evaluation_started,
        )
    else:
        agent = claude_code(
            version="sandbox",
            system_prompt=prompt,
            mcp_servers=servers,
            disallowed_tools=["WebSearch"],
            retry_refusals=0 if strict_pilot else 3,
            retry_uncaught_errors=0 if strict_pilot else 3,
            filter=_mark_evaluation_started,
        )
    return chain(
        infrastructure_readiness(host, agent_version, readiness_time_limit),
        evaluation_lifecycle(agent, readiness_time_limit, evaluation_time_limit),
    )


@solver
def infrastructure_readiness(
    host: Host,
    agent_version: str,
    timeout_seconds: int,
) -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        del generate
        record = new_lifecycle(host, agent_version)
        write_lifecycle(state.store, record)
        started = perf_counter()
        try:
            with fail_after(timeout_seconds):
                host_binary_sha256 = await _prepare_host_binary(host, agent_version)
                await _run_readiness_probes(host)
        except TimeoutError:
            record = readiness_failed(
                record,
                duration_seconds=perf_counter() - started,
                detail="readiness_timeout",
            )
            write_lifecycle(state.store, record)
            state.completed = True
            return state
        except Exception as error:
            record = readiness_failed(
                record,
                duration_seconds=perf_counter() - started,
                detail=_safe_failure_detail(error),
            )
            write_lifecycle(state.store, record)
            state.completed = True
            return state
        record = infrastructure_prepared(
            record,
            duration_seconds=perf_counter() - started,
            host_binary_sha256=host_binary_sha256,
        )
        write_lifecycle(state.store, record)
        return state

    return solve


@solver
def evaluation_lifecycle(
    agent: Agent,
    preparation_timeout_seconds: int,
    evaluation_timeout_seconds: int,
) -> Solver:
    agent_solver = as_solver(agent)

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        done = Event()
        agent_error: Exception | None = None
        result_state = state

        async def run_agent() -> None:
            nonlocal agent_error, result_state
            try:
                result_state = await agent_solver(state, generate)
            except Exception as error:
                agent_error = error
            finally:
                done.set()

        async with create_task_group() as tasks:
            tasks.start_soon(run_agent)
            readiness_elapsed = elapsed_seconds_since(
                _require_lifecycle(state).readiness_started_at
            )
            readiness_remaining = max(0.001, preparation_timeout_seconds - readiness_elapsed)
            with move_on_after(readiness_remaining) as preparation_scope:
                while not done.is_set() and not _evaluation_has_started(state):
                    await sleep(0.01)
            record = _require_lifecycle(state)
            if not _evaluation_has_started(state):
                tasks.cancel_scope.cancel()
                detail = (
                    _safe_failure_detail(agent_error)
                    if agent_error is not None
                    else "agent_preparation_timeout"
                    if preparation_scope.cancel_called
                    else "agent_stopped_before_first_model_request"
                )
                record = readiness_failed(
                    record,
                    reason=LifecycleFailureReason.EVALUATION_NOT_STARTED,
                    duration_seconds=elapsed_seconds_since(record.readiness_started_at),
                    detail=detail,
                )
                write_lifecycle(state.store, record)
                state.completed = True
                return state

            evaluation_started = perf_counter()
            with move_on_after(evaluation_timeout_seconds) as evaluation_scope:
                await done.wait()
            if evaluation_scope.cancel_called:
                tasks.cancel_scope.cancel()
                record = fail_evaluation(
                    record,
                    reason=LifecycleFailureReason.MODEL_TIMEOUT,
                    duration_seconds=perf_counter() - evaluation_started,
                    detail="evaluation_timeout",
                )
                write_lifecycle(state.store, record)
                state.completed = True
                return state
            if agent_error is not None:
                reason = (
                    LifecycleFailureReason.MODEL_LIMIT
                    if _is_model_limit_error(agent_error) or _sample_model_limit_exceeded()
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

        record = complete_evaluation(
            _require_lifecycle(result_state),
            duration_seconds=perf_counter() - evaluation_started,
        )
        write_lifecycle(result_state.store, allow_scoring(record))
        return result_state

    return solve


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
    if isinstance(error, RuntimeError) and str(error) in {
        "host_binary_missing_from_image",
        "host_binary_digest_failed",
        "host_binary_attestation_failed",
        "host_binary_version_mismatch",
        "host_bridge_dependency_or_database_readiness_failed",
        "sandbox_tools_readiness_failed",
        "unsupported_inspect_sandboxes_version",
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
    if payload != {"status": "ok"}:
        raise ValueError("submission acknowledgement is invalid")
    return Submission.model_validate(call.arguments)


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
