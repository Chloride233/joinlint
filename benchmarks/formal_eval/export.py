from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal

from benchmarks.formal_eval.contracts import (
    AgentResultBundle,
    AgentResultRow,
    FailureCode,
    StrictModel,
)
from benchmarks.formal_eval.lineage import digest_value
from benchmarks.formal_eval.run_plan import RunPlanV2, sample_id_for
from joinlint.contracts import canonical_json


class ExportSummary(StrictModel):
    log_count: int
    row_count: int
    artifact_incomplete_count: int
    model_ids: tuple[str, ...]
    conditions: tuple[str, ...]


def export_agent_rows(
    log_directory: Path,
    output: Path,
    *,
    expected_model_ids: set[str],
    lineage_id: str,
    run_plan: RunPlanV2,
    phase: Literal["all", "confirmatory", "diagnostic"] = "all",
    summary_output: Path | None = None,
) -> AgentResultBundle:
    from inspect_ai.log import list_eval_logs, read_eval_log

    log_infos = list_eval_logs(str(log_directory), recursive=True)
    if not log_infos:
        raise ValueError("formal log directory contains no Inspect logs")
    rows: list[AgentResultRow] = []
    for log_info in log_infos:
        log = read_eval_log(log_info.name, header_only=False)
        require_expected_model_identity(log.eval.model, expected_model_ids)
        for sample in log.samples or []:
            unexpected_usage = set(sample.model_usage) - expected_model_ids
            if unexpected_usage:
                raise ValueError(
                    f"unexpected provider-returned model identity: {sorted(unexpected_usage)}"
                )
        rows.extend(_rows_from_log(log, expected_lineage_id=lineage_id))
    sample_ids = [row.sample_id for row in rows]
    if len(sample_ids) != len(set(sample_ids)):
        raise ValueError("formal logs contain duplicate run identities")
    rows.sort(key=lambda row: row.sample_id.encode("utf-8"))
    expected_ids = {
        run.sample_id
        for run in run_plan.runs
        if phase == "all"
        or (phase == "confirmatory" and run.confirmatory)
        or (phase == "diagnostic" and not run.confirmatory)
    }
    if set(sample_ids) != expected_ids:
        raise ValueError("formal logs do not match the frozen run plan")
    run_plan_sha256 = digest_value(run_plan.model_dump(mode="json"))
    bundle = AgentResultBundle(
        lineage_id=lineage_id,
        run_plan_sha256=run_plan_sha256,
        rows=tuple(rows),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(canonical_json(bundle.model_dump(mode="json")))
    if summary_output is not None:
        summary = ExportSummary(
            log_count=len(log_infos),
            row_count=len(rows),
            artifact_incomplete_count=sum(not row.artifact_complete for row in rows),
            model_ids=tuple(sorted({row.model_id for row in rows})),
            conditions=tuple(sorted({row.condition for row in rows})),
        )
        summary_output.parent.mkdir(parents=True, exist_ok=True)
        summary_output.write_bytes(canonical_json(summary.model_dump(mode="json")))
    return bundle


def require_expected_model_identity(actual: str, expected: set[str]) -> None:
    if actual not in expected:
        raise ValueError(f"unexpected returned model identity: {actual}")


def _rows_from_log(log: Any, *, expected_lineage_id: str) -> list[AgentResultRow]:
    if log.samples is None:
        return []
    rows: list[AgentResultRow] = []
    for sample in log.samples:
        if sample.metadata.get("lineage_id") != expected_lineage_id:
            raise ValueError("Inspect sample lineage mismatch")
        rows.append(_row(log, sample))
    return rows


def _row(log: Any, sample: Any) -> AgentResultRow:
    metadata = sample.metadata
    condition = str(metadata["condition"])
    host = str(metadata["host"])
    model_id = log.eval.model
    repetition = max(0, sample.epoch - 1)
    completion_text = sample.output.completion if sample.output is not None else ""
    join_score = (sample.scores or {}).get("formal_join_scorer")
    execution_score = (sample.scores or {}).get("formal_execution_scorer")
    if join_score is None or execution_score is None:
        raise ValueError("formal scorer artifact is missing")
    artifact_complete = True
    join_metadata: dict[str, Any] = (
        join_score.metadata if join_score is not None and isinstance(join_score.metadata, dict) else {}
    )
    trace = join_metadata.get("trace") if isinstance(join_metadata.get("trace"), dict) else {}
    failure = join_metadata.get("failure_code")
    runtime_failure = _runtime_failure_code(sample.error)
    if runtime_failure is not None:
        failure = runtime_failure
    failure_code: FailureCode | None = failure if isinstance(failure, str) else None  # type: ignore[assignment]
    completion = bool(join_metadata.get("join_correct_task_completion")) and artifact_complete
    if runtime_failure is not None:
        completion = False
    if not completion and failure_code is None:
        failure_code = "INFRASTRUCTURE_FAILURE"

    mcp_condition = condition in {"treatment", "oracle_mcp", "no_harness"}
    blocking_applicable = bool(trace.get("blocking_applicable")) if mcp_condition else None
    blocking_compliant = (
        bool(trace.get("blocking_compliant")) if blocking_applicable else None
    )
    usage = list(sample.model_usage.values())
    input_tokens = sum(value.input_tokens for value in usage)
    output_tokens = sum(value.output_tokens for value in usage)
    cost = sum(value.total_cost or 0.0 for value in usage)
    return AgentResultRow(
        sample_id=sample_id_for(
            task_id=str(metadata["task_id"]),
            database_id=str(metadata["database_id"]),
            corpus="natural" if condition in {"control", "treatment"} else "semantic_join_failure",
            condition=condition,
            model_id=model_id,
            host=host,
            repetition=repetition,
        ),
        task_id=str(metadata["task_id"]),
        database_id=str(metadata["database_id"]),
        corpus="natural" if condition in {"control", "treatment"} else "semantic_join_failure",
        condition=condition,  # type: ignore[arg-type]
        model_id=model_id,
        host=host,  # type: ignore[arg-type]
        domain=str(metadata["domain"]),
        source_type=str(metadata["source_type"]),  # type: ignore[arg-type]
        database_scale=str(metadata["database_scale"]),  # type: ignore[arg-type]
        join_depth=int(metadata["join_depth"]),
        ambiguity=str(metadata["ambiguity"]),  # type: ignore[arg-type]
        fanout_type=str(metadata["fanout_type"]),  # type: ignore[arg-type]
        repetition=repetition,
        output_sha256=hashlib.sha256(completion_text.encode("utf-8")).hexdigest(),
        artifact_complete=artifact_complete,
        oracle_has_safe_path=bool(metadata["oracle_has_safe_path"]),
        submitted_sql=bool(join_metadata.get("submitted_sql")),
        safe_abstention=bool(join_metadata.get("safe_abstention")),
        join_graph_correct=bool(join_metadata.get("join_graph_correct")),
        evaluator_validation_passed=bool(join_metadata.get("evaluator_validation_passed")),
        join_correct_task_completion=completion,
        dangerous_sql_submitted=bool(join_metadata.get("dangerous_sql_submitted")),
        execution_correct=(
            bool(execution_score.value == 1) if execution_score is not None else None
        ),
        plan_called=bool(trace.get("plan_called")) if mcp_condition else None,
        plan_usable=bool(trace.get("plan_usable")) if mcp_condition else None,
        complete_entity_planning=(
            bool(trace.get("complete_entity_planning")) if mcp_condition else None
        ),
        final_sql_validated=(
            bool(trace.get("final_sql_validated")) if mcp_condition else None
        ),
        mcp_grounded=bool(trace.get("mcp_grounded")) if mcp_condition else None,
        blocking_applicable=blocking_applicable,
        blocking_compliant=blocking_compliant,
        bypassed=bool(trace.get("bypassed")) if mcp_condition else None,
        tool_error=bool(trace.get("tool_error")) if mcp_condition else None,
        total_time_seconds=sample.total_time or 0.0,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
        failure_code=failure_code,
    )


def _runtime_failure_code(error: object) -> FailureCode | None:
    if error is None:
        return None
    text = str(error).lower()
    if "timeout" in text or "time limit" in text:
        return "MODEL_TIMEOUT"
    return "INFRASTRUCTURE_FAILURE"
