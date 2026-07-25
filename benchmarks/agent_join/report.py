from __future__ import annotations

import hashlib
import json
import math
import random
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from statistics import fmean
from typing import Any, Literal

import yaml
from inspect_ai.dataset import Sample
from inspect_ai.log import EvalSample, read_eval_log
from pydantic import Field, field_validator, model_validator

from benchmarks.agent_join.contracts import (
    Arm,
    JoinScore,
    PilotSample,
    ResultRow,
    StrictModel,
    TraceScore,
)
from joinlint.contracts import canonical_json


SCORER_NAMES = (
    "join_outcome_scorer",
    "mcp_trace_scorer",
    "execution_scorer",
)
COMPARISONS: tuple[tuple[Arm, Arm], ...] = (
    ("A", "B"),
    ("B", "C"),
    ("C", "D"),
    ("A", "D"),
)
FORBIDDEN_FIELD_NAMES = {
    "database_path",
    "gold_sql",
    "messages",
    "prompt",
    "question",
    "schema_text",
    "submitted_sql",
    "tool_content",
    "warning",
}
REPORT_HEADING = (
    "> Exploratory Spider pilot. These results validate the evaluation protocol "
    "and are not a general product or marketing claim."
)


class SanitizedRowsDocument(StrictModel):
    schema_version: Literal[1] = 1
    requested_model: str
    returned_model: str | None
    input_hashes: dict[str, str]
    log_sha256: str
    rows: list[ResultRow] = Field(min_length=200, max_length=200)

    @field_validator("requested_model")
    @classmethod
    def require_requested_model(cls, value: str) -> str:
        if not value or value.startswith(("/", "\\")):
            raise ValueError("requested_model must be a non-empty model identifier")
        return value

    @field_validator("returned_model")
    @classmethod
    def validate_returned_model(cls, value: str | None) -> str | None:
        if value is not None and (not value or value.startswith(("/", "\\"))):
            raise ValueError("returned_model must be a model identifier")
        return value

    @field_validator("input_hashes")
    @classmethod
    def validate_input_hashes(cls, value: dict[str, str]) -> dict[str, str]:
        if not value:
            raise ValueError("input_hashes cannot be empty")
        for name, digest in value.items():
            path = PurePosixPath(name)
            if (
                not name
                or name.startswith(("/", "\\"))
                or name.startswith("~/")
                or "\\" in name
                or ".." in path.parts
                or (len(name) >= 3 and name[1:3] == ":/")
                or not _is_sha256(digest)
            ):
                raise ValueError("input hashes require relative names and SHA-256 values")
        return value

    @field_validator("log_sha256")
    @classmethod
    def validate_log_hash(cls, value: str) -> str:
        if not _is_sha256(value):
            raise ValueError("log_sha256 must be a lowercase SHA-256 value")
        return value

    @model_validator(mode="after")
    def require_unique_rows(self) -> SanitizedRowsDocument:
        sample_ids = [row.sample_id for row in self.rows]
        if len(sample_ids) != len(set(sample_ids)):
            raise ValueError("sanitized rows contain duplicate sample IDs")
        return self


class ConfidenceInterval(StrictModel):
    lower: float | None
    upper: float | None


class BootstrapResult(StrictModel):
    seed: int
    draw_count: int
    intervals: dict[str, dict[str, ConfidenceInterval]]


class RelationshipReviewSummary(StrictModel):
    candidate_count: int = Field(ge=0)
    confirmed_count: int = Field(ge=0)
    graph_precision: float = Field(ge=0.0, le=1.0)
    graph_recall: float = Field(ge=0.0, le=1.0)
    graph_f1: float = Field(ge=0.0, le=1.0)
    accepted_entity_count: int = Field(ge=0)
    rejected_entity_count: int = Field(ge=0)
    accepted_relationship_count: int = Field(ge=0)
    rejected_relationship_count: int = Field(ge=0)
    elapsed_human_review_seconds: float = Field(ge=0.0)

    @model_validator(mode="after")
    def require_finite_values(self) -> RelationshipReviewSummary:
        if not all(
            math.isfinite(value)
            for value in (
                self.graph_precision,
                self.graph_recall,
                self.graph_f1,
                self.elapsed_human_review_seconds,
            )
        ):
            raise ValueError("relationship review metrics must be finite")
        return self


def load_result_rows(
    log_path: Path,
    planned_samples: Sequence[Sample],
    preregistration_path: Path,
    *,
    input_hashes: Mapping[str, str],
) -> SanitizedRowsDocument:
    preregistration = _read_preregistration(preregistration_path)
    requested_model = _required_string(preregistration, "model", "id")
    pricing = _pricing(preregistration)
    if not log_path.is_file() or log_path.is_symlink():
        raise ValueError("Inspect log must be one regular file")
    log_sha256 = _sha256(log_path)
    log = read_eval_log(
        log_path,
        exclude_fields={
            "attachments",
            "events",
            "messages",
            "output",
            "store",
        },
    )
    if log.eval.model != requested_model:
        raise ValueError("Inspect log requested model differs from preregistration")

    planned: dict[str, PilotSample] = {}
    for candidate in planned_samples:
        sample = PilotSample.model_validate_json(
            json.dumps(candidate.metadata, ensure_ascii=False)
        )
        if candidate.id != sample.sample_id or sample.sample_id in planned:
            raise ValueError("planned samples contain duplicate or mismatched IDs")
        planned[sample.sample_id] = sample
    if len(planned) != 200:
        raise ValueError("the frozen pilot must contain exactly 200 planned samples")

    logged: dict[str, EvalSample] = {}
    returned_models: set[str] = set()
    for sample in log.samples or []:
        sample_id = str(sample.id)
        if sample_id in logged:
            raise ValueError("Inspect log contains duplicate sample IDs")
        if sample_id not in planned:
            raise ValueError("Inspect log contains an unplanned sample ID")
        logged_metadata = PilotSample.model_validate_json(
            json.dumps(sample.metadata, ensure_ascii=False)
        )
        if logged_metadata != planned[sample_id]:
            raise ValueError("Inspect log sample metadata differs from the frozen plan")
        logged[sample_id] = sample
        returned_models.update(sample.model_usage)
    if len(returned_models) > 1:
        raise ValueError("Inspect log contains mixed returned model identities")
    returned_model = next(iter(returned_models), None)

    rows = [
        _row_from_log(planned[sample_id], logged.get(sample_id), pricing)
        for sample_id in sorted(planned, key=_utf8_key)
    ]
    document = SanitizedRowsDocument(
        requested_model=requested_model,
        returned_model=returned_model,
        input_hashes=dict(input_hashes),
        log_sha256=log_sha256,
        rows=rows,
    )
    if _sha256(log_path) != log_sha256:
        raise ValueError("Inspect log changed while sanitized rows were exported")
    return document


def write_sanitized_rows(path: Path, document: SanitizedRowsDocument) -> None:
    ordered = document.model_copy(
        update={"rows": sorted(document.rows, key=lambda row: _utf8_key(row.sample_id))}
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(ordered.model_dump(mode="json")))


def read_sanitized_rows(
    path: Path,
    *,
    expected_sample_ids: set[str],
    expected_requested_model: str,
    expected_returned_model: str | None,
    expected_input_hashes: Mapping[str, str],
    expected_log_sha256: str,
) -> SanitizedRowsDocument:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        document = SanitizedRowsDocument.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("invalid sanitized result document") from error
    sample_ids = [row.sample_id for row in document.rows]
    if sample_ids != sorted(sample_ids, key=_utf8_key):
        raise ValueError("sanitized result rows are not UTF-8 sorted")
    if set(sample_ids) != expected_sample_ids:
        raise ValueError("sanitized result rows differ from the frozen sample IDs")
    if (
        document.requested_model != expected_requested_model
        or document.returned_model != expected_returned_model
    ):
        raise ValueError("sanitized result model metadata differs from the frozen lock")
    if (
        document.input_hashes != dict(expected_input_hashes)
        or document.log_sha256 != expected_log_sha256
    ):
        raise ValueError("sanitized result hashes differ from the frozen lock")
    return document


def paired_effects(rows: Sequence[ResultRow]) -> dict[str, dict[str, float | None]]:
    units = _primary_units(rows)
    return _effects_for_units(list(units.values()))


def cluster_bootstrap(
    rows: Sequence[ResultRow],
    *,
    draws: int,
    seed: int,
) -> BootstrapResult:
    if draws <= 0:
        raise ValueError("bootstrap draws must be positive")
    units = _primary_units(rows)
    hierarchy: dict[str, dict[str, dict[int, dict[Arm, ResultRow]]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    for (db_id, task_id, repetition), arms in units.items():
        hierarchy[db_id][task_id][repetition] = arms

    rng = random.Random(seed)
    samples: dict[str, dict[str, list[float]]] = {
        metric: {name: [] for name in _comparison_names()}
        for metric in _effect_metric_names()
    }
    database_ids = sorted(hierarchy, key=_utf8_key)
    for _ in range(draws):
        drawn_units: list[dict[Arm, ResultRow]] = []
        for db_id in rng.choices(database_ids, k=len(database_ids)):
            task_ids = sorted(hierarchy[db_id], key=_utf8_key)
            for task_id in rng.choices(task_ids, k=len(task_ids)):
                repetitions = sorted(hierarchy[db_id][task_id])
                for repetition in rng.choices(repetitions, k=len(repetitions)):
                    drawn_units.append(hierarchy[db_id][task_id][repetition])
        effects = _effects_for_units(drawn_units)
        for metric, comparisons in effects.items():
            for name, value in comparisons.items():
                if value is not None:
                    samples[metric][name].append(value)

    intervals = {
        metric: {
            name: ConfidenceInterval(
                lower=_nearest_rank(values, 0.025),
                upper=_nearest_rank(values, 0.975),
            )
            for name, values in comparisons.items()
        }
        for metric, comparisons in samples.items()
    }
    return BootstrapResult(seed=seed, draw_count=draws, intervals=intervals)


def summarize_relationship_review(
    records: Sequence[Mapping[str, object]],
) -> dict[str, dict[str, int | float]]:
    summaries: dict[str, dict[str, int | float]] = {}
    for record in records:
        db_id = str(record["db_id"])
        if db_id in summaries:
            raise ValueError("relationship review contains duplicate database IDs")
        candidates = _edge_set(record["candidate_edges"])
        confirmed = _edge_set(record["confirmed_edges"])
        oracle = _edge_set(record["oracle_edges"])
        if not confirmed <= candidates:
            raise ValueError("confirmed relationships must be candidate relationships")
        matched = confirmed & oracle
        precision = len(matched) / len(confirmed) if confirmed else 0.0
        recall = len(matched) / len(oracle) if oracle else 1.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        entity_decisions = Counter(str(value) for value in record["entity_decisions"])  # type: ignore[arg-type]
        relationship_decisions = Counter(
            str(value) for value in record["relationship_decisions"]  # type: ignore[arg-type]
        )
        if "pending" in entity_decisions or set(relationship_decisions) - {
            "accept",
            "reject",
        }:
            raise ValueError("relationship review contains a pending or invalid decision")
        elapsed = float(record["elapsed_human_review_seconds"])
        if elapsed < 0:
            raise ValueError("relationship review elapsed time must be non-negative")
        summaries[db_id] = {
            "candidate_count": len(candidates),
            "confirmed_count": len(confirmed),
            "graph_precision": precision,
            "graph_recall": recall,
            "graph_f1": f1,
            "accepted_entity_count": sum(
                count for decision, count in entity_decisions.items() if decision != "reject_table"
            ),
            "rejected_entity_count": entity_decisions["reject_table"],
            "accepted_relationship_count": relationship_decisions["accept"],
            "rejected_relationship_count": relationship_decisions["reject"],
            "elapsed_human_review_seconds": elapsed,
        }
    return {key: summaries[key] for key in sorted(summaries, key=_utf8_key)}


def evaluate_pilot_gates(
    document: SanitizedRowsDocument,
    *,
    manual_scorer_agreement: float | None,
    expected_sample_ids: set[str],
    expected_input_hashes: Mapping[str, str],
    expected_log_sha256: str,
    forbidden_values: Sequence[str] = (),
) -> dict[str, object]:
    if manual_scorer_agreement is not None and (
        not math.isfinite(manual_scorer_agreement)
        or not 0.0 <= manual_scorer_agreement <= 1.0
    ):
        raise ValueError("manual scorer agreement must be within [0, 1]")
    completion_rate = sum(row.artifact_complete for row in document.rows) / len(document.rows)
    agreement_passed = (
        manual_scorer_agreement is not None and manual_scorer_agreement >= 0.95
    )
    identifiers_match = {row.sample_id for row in document.rows} == expected_sample_ids
    hashes_match = (
        document.input_hashes == dict(expected_input_hashes)
        and document.log_sha256 == expected_log_sha256
    )
    leak_count = _forbidden_leak_count(document, forbidden_values)
    gates: dict[str, object] = {
        "artifact_completion": {
            "passed": completion_rate >= 0.95,
            "rate": completion_rate,
            "minimum": 0.95,
        },
        "manual_scorer_agreement": {
            "passed": agreement_passed,
            "rate": manual_scorer_agreement,
            "minimum": 0.95,
        },
        "exact_arm_repetition_balance": {
            "passed": _has_exact_primary_balance(document.rows),
        },
        "one_model_identity": {"passed": document.returned_model is not None},
        "frozen_sample_ids": {"passed": identifiers_match},
        "required_hashes": {"passed": hashes_match},
        "forbidden_field_leaks": {"passed": leak_count == 0, "count": leak_count},
    }
    gates["passed"] = all(
        bool(value["passed"])
        for value in gates.values()
        if isinstance(value, dict) and "passed" in value
    )
    return gates


def build_report(
    document: SanitizedRowsDocument,
    *,
    manual_scorer_agreement: float | None,
    expected_sample_ids: set[str],
    expected_input_hashes: Mapping[str, str],
    expected_log_sha256: str,
    draws: int,
    seed: int,
    relationship_review: Mapping[str, Mapping[str, int | float]] | None = None,
    forbidden_values: Sequence[str] = (),
) -> dict[str, object]:
    primary = [row for row in document.rows if row.suite == "primary"]
    diagnostics = [row for row in document.rows if row.suite != "primary"]
    safe_relationship_review = _validated_relationship_review(relationship_review or {})
    report: dict[str, object] = {
        "schema_version": 1,
        "notice": REPORT_HEADING[2:],
        "counts": {
            "planned": len(document.rows),
            "artifact_complete": sum(row.artifact_complete for row in document.rows),
            "primary": len(primary),
            "diagnostic": len(diagnostics),
        },
        "arm_summaries": _arm_summaries(primary),
        "paired_effects": paired_effects(primary),
        "exploratory_intervals": cluster_bootstrap(
            primary,
            draws=draws,
            seed=seed,
        ).model_dump(mode="json"),
        "mcp_behavior": _mcp_behavior(document.rows),
        "errors": _error_summaries(primary),
        "relationship_review": safe_relationship_review,
        "model": {
            "requested": document.requested_model,
            "returned": document.returned_model,
        },
        "hashes": {
            "inputs": document.input_hashes,
            "log_sha256": document.log_sha256,
        },
        "protocol_gates": evaluate_pilot_gates(
            document,
            manual_scorer_agreement=manual_scorer_agreement,
            expected_sample_ids=expected_sample_ids,
            expected_input_hashes=expected_input_hashes,
            expected_log_sha256=expected_log_sha256,
            forbidden_values=forbidden_values,
        ),
    }
    return _sort_mapping(report)


def render_markdown(report: Mapping[str, object]) -> str:
    counts = _mapping(report, "counts")
    gates = _mapping(report, "protocol_gates")
    effects = _mapping(report, "paired_effects")
    wrong_join = _mapping(effects, "wrong_join_rate_points")
    lines = [
        REPORT_HEADING,
        "",
        "# JoinLint MCP evaluation report",
        "",
        f"Protocol gates: **{'PASS' if gates['passed'] else 'FAIL'}**",
        "",
        f"Planned runs: {counts['planned']}",
        f"Complete artifacts: {counts['artifact_complete']}",
        f"Primary runs: {counts['primary']}",
        f"Diagnostic runs: {counts['diagnostic']}",
        "",
        "## Wrong Join Rate effects (percentage points)",
        "",
    ]
    for name in _comparison_names():
        value = wrong_join[name]
        rendered = "unavailable" if value is None else f"{float(value):.3f}"
        lines.append(f"- {name}: {rendered}")
    lines.extend(["", "Intervals are exploratory and database-clustered.", ""])
    return "\n".join(lines)


def write_report(
    json_path: Path,
    markdown_path: Path,
    report: Mapping[str, object],
) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_bytes(canonical_json(dict(report)))
    markdown_path.write_text(render_markdown(report), encoding="utf-8")


def _row_from_log(
    planned: PilotSample,
    logged: EvalSample | None,
    pricing: tuple[float, float, float],
) -> ResultRow:
    if (
        logged is None
        or logged.error is not None
        or not logged.scores
        or not logged.model_usage
        or logged.total_time is None
    ):
        return _missing_row(planned)
    if any(name not in logged.scores for name in SCORER_NAMES):
        return _missing_row(planned)

    join_score = logged.scores["join_outcome_scorer"]
    join_metadata = dict(join_score.metadata or {})
    join_metadata.pop("submitted_sql", None)
    join_metadata.pop("warning", None)
    outcome = JoinScore.model_validate_json(json.dumps(join_metadata, ensure_ascii=False))
    if join_score.value != (0 if outcome.wrong_join else 1):
        raise ValueError("join scorer value contradicts its metadata")

    execution_metadata = dict(
        logged.scores["execution_scorer"].metadata or {}
    )
    executed = execution_metadata.get("executed")
    equivalent = execution_metadata.get("equivalent")
    execution_error = execution_metadata.get("error_code")
    if not isinstance(executed, bool) or (
        equivalent is not None and not isinstance(equivalent, bool)
    ) or (execution_error is not None and not isinstance(execution_error, str)):
        raise ValueError("execution scorer metadata is invalid")
    execution_correct = None if execution_error == "GOLD_EXECUTION_FAILED" else bool(equivalent)

    trace_metadata = dict(logged.scores["mcp_trace_scorer"].metadata or {})
    if planned.arm in {"A", "B"}:
        if trace_metadata != {"applicable": False}:
            raise ValueError("non-MCP trace score must be explicitly inapplicable")
        trace: TraceScore | None = None
    else:
        if trace_metadata.pop("applicable", None) is not True:
            raise ValueError("MCP trace score must be applicable")
        trace_metadata.pop("submission_error", None)
        trace = TraceScore.model_validate_json(
            json.dumps(trace_metadata, ensure_ascii=False)
        )
        if logged.scores["mcp_trace_scorer"].value != (1 if trace.grounded else 0):
            raise ValueError("trace scorer value contradicts its metadata")

    execution_value = logged.scores["execution_scorer"].value
    if execution_error == "GOLD_EXECUTION_FAILED":
        if execution_value != "U":
            raise ValueError("gold execution failure must be unscored")
    elif execution_value != (1 if equivalent else 0):
        raise ValueError("execution scorer value contradicts its metadata")

    input_tokens = 0
    cache_read_tokens = 0
    output_tokens = 0
    for usage in logged.model_usage.values():
        input_tokens += usage.input_tokens
        cache_read_tokens += usage.input_tokens_cache_read or 0
        output_tokens += usage.output_tokens
    if cache_read_tokens > input_tokens:
        raise ValueError("Inspect cache-read usage exceeds total input usage")
    cache_hit_rate, cache_miss_rate, output_rate = pricing
    total_cost = (
        cache_read_tokens * cache_hit_rate
        + (input_tokens - cache_read_tokens) * cache_miss_rate
        + output_tokens * output_rate
    ) / 1_000_000
    error_code = outcome.error_code or (
        execution_error if isinstance(execution_error, str) else None
    )
    if error_code is None and trace is not None and trace.tool_error:
        error_code = "MCP_TOOL_ERROR"
    return ResultRow(
        sample_id=planned.sample_id,
        task_id=planned.task_id,
        db_id=planned.db_id,
        arm=planned.arm,
        suite=planned.suite,
        repetition=planned.repetition,
        artifact_complete=True,
        wrong_join=outcome.wrong_join,
        join_precision=outcome.precision,
        join_recall=outcome.recall,
        join_f1=outcome.f1,
        execution_correct=execution_correct,
        grounded=trace.grounded if trace is not None else None,
        tool_called=trace.tool_called if trace is not None else None,
        validated=trace.validated if trace is not None else None,
        blocking_compliant=trace.blocking_compliant if trace is not None else None,
        input_tokens=input_tokens,
        input_cache_read_tokens=cache_read_tokens,
        output_tokens=output_tokens,
        total_cost_usd=total_cost,
        total_time_seconds=float(logged.total_time or 0.0),
        error_code=error_code,
    )


def _missing_row(planned: PilotSample) -> ResultRow:
    is_mcp = planned.arm in {"C", "D"}
    return ResultRow(
        sample_id=planned.sample_id,
        task_id=planned.task_id,
        db_id=planned.db_id,
        arm=planned.arm,
        suite=planned.suite,
        repetition=planned.repetition,
        artifact_complete=False,
        wrong_join=True,
        join_precision=0.0,
        join_recall=0.0,
        join_f1=0.0,
        execution_correct=False,
        grounded=False if is_mcp else None,
        tool_called=False if is_mcp else None,
        validated=False if is_mcp else None,
        blocking_compliant=False if is_mcp else None,
        input_tokens=0,
        input_cache_read_tokens=0,
        output_tokens=0,
        total_cost_usd=0.0,
        total_time_seconds=0.0,
        error_code="MISSING_ARTIFACT",
    )


def _primary_units(
    rows: Sequence[ResultRow],
) -> dict[tuple[str, str, int], dict[Arm, ResultRow]]:
    units: dict[tuple[str, str, int], dict[Arm, ResultRow]] = defaultdict(dict)
    for row in rows:
        if row.suite != "primary":
            continue
        key = (row.db_id, row.task_id, row.repetition)
        if row.arm in units[key]:
            raise ValueError("primary results contain a duplicate paired arm")
        units[key][row.arm] = row
    if not units or any(set(arms) != {"A", "B", "C", "D"} for arms in units.values()):
        raise ValueError("primary results must contain complete four-arm pairs")
    return dict(units)


def _effects_for_units(
    units: Sequence[dict[Arm, ResultRow]],
) -> dict[str, dict[str, float | None]]:
    effects = {
        metric: {name: None for name in _comparison_names()}
        for metric in _effect_metric_names()
    }
    for baseline, treatment in COMPARISONS:
        name = f"{baseline}_vs_{treatment}"
        wrong = [
            (float(unit[baseline].wrong_join) - float(unit[treatment].wrong_join)) * 100
            for unit in units
        ]
        f1 = [
            (unit[treatment].join_f1 - unit[baseline].join_f1) * 100
            for unit in units
        ]
        execution = [
            (float(unit[treatment].execution_correct) - float(unit[baseline].execution_correct))
            * 100
            for unit in units
            if unit[baseline].execution_correct is not None
            and unit[treatment].execution_correct is not None
        ]
        effects["wrong_join_rate_points"][name] = fmean(wrong)
        effects["join_f1_points"][name] = fmean(f1)
        effects["execution_accuracy_points"][name] = (
            fmean(execution) if execution else None
        )
    return effects


def _has_exact_primary_balance(rows: Sequence[ResultRow]) -> bool:
    primary = [row for row in rows if row.suite == "primary"]
    try:
        units = _primary_units(primary)
    except ValueError:
        return False
    tasks_by_database: dict[str, set[str]] = defaultdict(set)
    repetitions_by_task: dict[tuple[str, str], set[int]] = defaultdict(set)
    for db_id, task_id, repetition in units:
        tasks_by_database[db_id].add(task_id)
        repetitions_by_task[(db_id, task_id)].add(repetition)
    return (
        len(primary) == 192
        and len(tasks_by_database) == 4
        and all(len(tasks) == 4 for tasks in tasks_by_database.values())
        and all(repetitions == {0, 1, 2} for repetitions in repetitions_by_task.values())
    )


def _arm_summaries(rows: Sequence[ResultRow]) -> dict[str, dict[str, float | int | None]]:
    summaries: dict[str, dict[str, float | int | None]] = {}
    for arm in ("A", "B", "C", "D"):
        selected = [row for row in rows if row.arm == arm]
        execution = [row.execution_correct for row in selected if row.execution_correct is not None]
        summaries[arm] = {
            "count": len(selected),
            "wrong_join_rate": _bool_rate(row.wrong_join for row in selected),
            "execution_accuracy": _bool_rate(execution),
            "mean_join_precision": _mean(row.join_precision for row in selected),
            "mean_join_recall": _mean(row.join_recall for row in selected),
            "mean_join_f1": _mean(row.join_f1 for row in selected),
            "input_tokens": sum(row.input_tokens for row in selected),
            "input_cache_read_tokens": sum(
                row.input_cache_read_tokens for row in selected
            ),
            "output_tokens": sum(row.output_tokens for row in selected),
            "total_cost_usd": sum(row.total_cost_usd for row in selected),
            "mean_time_seconds": _mean(row.total_time_seconds for row in selected),
        }
    return summaries


def _mcp_behavior(rows: Sequence[ResultRow]) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for suite in ("primary", "zero_config", "safety"):
        for arm in ("C", "D"):
            selected = [row for row in rows if row.suite == suite and row.arm == arm]
            if not selected:
                continue
            result[f"{suite}:{arm}"] = {
                "count": len(selected),
                "artifact_completion_rate": _bool_rate(
                    row.artifact_complete for row in selected
                ),
                "tool_call_rate": _optional_bool_rate(row.tool_called for row in selected),
                "grounded_rate": _optional_bool_rate(row.grounded for row in selected),
                "validation_rate": _optional_bool_rate(row.validated for row in selected),
                "blocking_compliance_rate": _optional_bool_rate(
                    row.blocking_compliant for row in selected
                ),
                "errors_by_code": dict(
                    Counter(row.error_code or "NONE" for row in selected)
                ),
            }
    return result


def _error_summaries(rows: Sequence[ResultRow]) -> dict[str, object]:
    by_code = Counter(row.error_code or "NONE" for row in rows)
    by_database: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        by_database[row.db_id][row.error_code or "NONE"] += 1
    return {
        "by_code": dict(by_code),
        "by_database": {db_id: dict(counts) for db_id, counts in by_database.items()},
    }


def _forbidden_leak_count(
    document: SanitizedRowsDocument,
    forbidden_values: Sequence[str],
) -> int:
    payload = document.model_dump(mode="json")
    leaks = 0

    def walk(value: object) -> None:
        nonlocal leaks
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in FORBIDDEN_FIELD_NAMES:
                    leaks += 1
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            if value.startswith(("/Users/", "/home/")):
                leaks += 1
            leaks += sum(bool(secret) and secret in value for secret in forbidden_values)

    walk(payload)
    return leaks


def _read_preregistration(path: Path) -> dict[str, object]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("preregistration must be a YAML object")
    return document


def _pricing(document: Mapping[str, object]) -> tuple[float, float, float]:
    pricing = document.get("pricing")
    if not isinstance(pricing, dict):
        raise ValueError("preregistration pricing is missing")
    names = (
        "input_cache_hit_per_million_usd",
        "input_cache_miss_per_million_usd",
        "output_per_million_usd",
    )
    values = tuple(float(pricing[name]) for name in names)
    if any(value < 0 for value in values):
        raise ValueError("preregistered pricing must be non-negative")
    return values  # type: ignore[return-value]


def _required_string(document: Mapping[str, object], section: str, key: str) -> str:
    container = document.get(section)
    value = container.get(key) if isinstance(container, dict) else None
    if not isinstance(value, str) or not value:
        raise ValueError(f"preregistration {section}.{key} is missing")
    return value


def _nearest_rank(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _edge_set(value: object) -> set[tuple[str, str]]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError("relationship review edges must be a sequence")
    edges: set[tuple[str, str]] = set()
    for edge in value:
        if not isinstance(edge, (list, tuple)) or len(edge) != 2:
            raise ValueError("relationship review edge must contain two endpoints")
        edges.add(tuple(sorted((str(edge[0]), str(edge[1])), key=_utf8_key)))
    return edges


def _validated_relationship_review(
    summaries: Mapping[str, Mapping[str, int | float]],
) -> dict[str, dict[str, object]]:
    validated: dict[str, dict[str, object]] = {}
    for db_id, summary in summaries.items():
        if (
            not db_id
            or db_id.startswith(("/", "\\", "~/"))
            or "\\" in db_id
            or ".." in PurePosixPath(db_id).parts
            or (len(db_id) >= 3 and db_id[1:3] == ":/")
            or len(db_id) > 256
        ):
            raise ValueError("relationship review database ID is not sanitized")
        validated[db_id] = RelationshipReviewSummary.model_validate(summary).model_dump(
            mode="json"
        )
    return validated


def _comparison_names() -> list[str]:
    return [f"{baseline}_vs_{treatment}" for baseline, treatment in COMPARISONS]


def _effect_metric_names() -> tuple[str, ...]:
    return (
        "wrong_join_rate_points",
        "join_f1_points",
        "execution_accuracy_points",
    )


def _bool_rate(values: Sequence[bool] | Any) -> float | None:
    materialized = list(values)
    return _mean(float(value) for value in materialized)


def _optional_bool_rate(values: Any) -> float | None:
    materialized = [value for value in values if value is not None]
    return _bool_rate(materialized)


def _mean(values: Any) -> float | None:
    materialized = list(values)
    return fmean(materialized) if materialized else None


def _mapping(document: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = document[key]
    if not isinstance(value, Mapping):
        raise ValueError(f"report field {key} must be an object")
    return value


def _sort_mapping(value: Mapping[str, object]) -> dict[str, object]:
    def sort(item: object) -> object:
        if isinstance(item, Mapping):
            return {
                str(key): sort(item[key])
                for key in sorted(item, key=lambda key: _utf8_key(str(key)))
            }
        if isinstance(item, list):
            return [sort(element) for element in item]
        return item

    return sort(value)  # type: ignore[return-value]


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utf8_key(value: str) -> bytes:
    return value.encode("utf-8")
