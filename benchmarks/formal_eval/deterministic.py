from __future__ import annotations

import argparse
import asyncio
import os
import resource
import statistics
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from pydantic import Field, field_validator, model_validator

from benchmarks.formal_eval.contracts import (
    AdversarialType,
    DeterministicEvidenceBundle,
    Edge,
    EvidenceMode,
    FKState,
    PerformanceMeasurement,
    Provenance,
    RelationshipScope,
    RelationshipResultRow,
    SQLClass,
    SQLDangerType,
    SQLShape,
    SQLValidationResultRow,
    SourceType,
    Split,
    StrictModel,
)
from benchmarks.formal_eval.lineage import EvaluationLineage
from benchmarks.formal_eval.lineage import digest_value
from benchmarks.formal_eval.manifest import load_document
from benchmarks.formal_eval.trace import PlanResponse, ValidationResponse
from joinlint.contracts import canonical_json


class RelationshipEvidenceCase(StrictModel):
    case_id: str
    database_id: str
    variant_group: str
    project_path: str
    source: str | None = None
    entities: tuple[str, ...]
    expected_edges: tuple[Edge, ...]
    corpus: Literal["natural", "adversarial"]
    source_type: SourceType
    domain: str
    split: Split
    provenance: Provenance
    eligible: bool
    expected_relation: bool
    fk_state: FKState | None = None
    adversarial_type: AdversarialType | None = None
    reviewer_labels: tuple[bool, bool] | None = None
    adjudicated_label: bool | None = None

    @field_validator("project_path", "source")
    @classmethod
    def validate_project_path(cls, value: str | None) -> str | None:
        return _validate_relative_path(value) if value is not None else None


class SQLValidationCase(StrictModel):
    case_id: str
    project_path: str
    source: str | None = None
    sql: str
    sql_class: SQLClass
    supported_shape: bool
    sql_shape: SQLShape
    danger_type: SQLDangerType | None = None
    expected_edges: tuple[Edge, ...]
    auto_usable_edges: tuple[Edge, ...]
    expected_grain_ref: str | None = None
    plan_entities: tuple[str, ...] = ()

    @field_validator("project_path", "source")
    @classmethod
    def validate_project_path(cls, value: str | None) -> str | None:
        return _validate_relative_path(value) if value is not None else None


class PerformanceSpec(StrictModel):
    project_path: str
    source: str | None = None
    entities: tuple[str, ...]
    safe_sql: str
    million_row_project_path: str
    million_row_source: str | None = None
    million_row_sql: str
    repeats: int = Field(default=20, ge=5, le=100)

    @field_validator(
        "project_path",
        "source",
        "million_row_project_path",
        "million_row_source",
    )
    @classmethod
    def validate_project_path(cls, value: str | None) -> str | None:
        return _validate_relative_path(value) if value is not None else None


class DeterministicSuite(StrictModel):
    schema_version: Literal[2] = 2
    relationship_scope: RelationshipScope
    relationship_cases: tuple[RelationshipEvidenceCase, ...]
    sql_validation_cases: tuple[SQLValidationCase, ...]
    performance: PerformanceSpec

    @model_validator(mode="after")
    def require_unique_cases(self) -> DeterministicSuite:
        relationship_ids = [case.case_id for case in self.relationship_cases]
        sql_ids = [case.case_id for case in self.sql_validation_cases]
        if len(relationship_ids) != len(set(relationship_ids)):
            raise ValueError("deterministic suite contains duplicate relationship cases")
        if len(sql_ids) != len(set(sql_ids)):
            raise ValueError("deterministic suite contains duplicate SQL cases")
        variant_splits: dict[str, set[str]] = {}
        for case in self.relationship_cases:
            variant_splits.setdefault(case.variant_group, set()).add(case.split)
        if any(len(splits) > 1 for splits in variant_splits.values()):
            raise ValueError("relationship database variants cannot cross splits")
        return self


async def run_deterministic_evidence(
    suite: DeterministicSuite,
    lineage: EvaluationLineage,
) -> DeterministicEvidenceBundle:
    # Run the million-row subprocess first so RUSAGE_CHILDREN is attributable
    # to that isolated child rather than a previous MCP case.
    performance = await _measure_performance(suite.performance)
    relationships = [await _run_relationship_case(case) for case in suite.relationship_cases]
    sql_rows = [await _run_sql_case(case) for case in suite.sql_validation_cases]
    return DeterministicEvidenceBundle(
        lineage_id=lineage.lineage_id,
        deterministic_suite_sha256=digest_value(suite.model_dump(mode="json")),
        producer="joinlint-formal-deterministic-v2",
        relationship_rows=tuple(relationships),
        sql_validation_rows=tuple(sql_rows),
        performance=performance,
    )


async def _run_relationship_case(case: RelationshipEvidenceCase) -> RelationshipResultRow:
    raw = await _call_mcp(
        Path(case.project_path),
        case.source,
        "get_join_plan",
        _plan_arguments(case.entities),
    )
    response = PlanResponse.model_validate(raw)
    predicted = _ordered_plan_edges(response)
    edges = response.data.proof.edges if response.data is not None else ()
    auto_usable = (
        response.status == "ok"
        and bool(edges)
        and not _blocking(response.findings)
        and response.data is not None
        and response.data.lifecycle.status == "current"
        and all(
            edge.provenance == case.provenance
            for edge in edges
        )
    )
    evidence_mode: EvidenceMode = "exact"
    extracted = bool(predicted)
    direction_correct = predicted == case.expected_edges if extracted else None
    prediction_correct = direction_correct if extracted else None
    return RelationshipResultRow(
        case_id=case.case_id,
        database_id=case.database_id,
        variant_group=case.variant_group,
        corpus=case.corpus,
        source_type=case.source_type,
        domain=case.domain,
        split=case.split,
        provenance=case.provenance,
        eligible=case.eligible,
        expected_relation=case.expected_relation,
        extracted=extracted,
        auto_usable=auto_usable,
        prediction_correct=prediction_correct,
        expected_edges=case.expected_edges,
        predicted_edges=predicted,
        direction_correct=direction_correct,
        evidence_mode=evidence_mode,
        stale=response.data is not None and response.data.lifecycle.status != "current",
        fk_state=case.fk_state,
        adversarial_type=case.adversarial_type,
        reviewer_labels=case.reviewer_labels,
        adjudicated_label=case.adjudicated_label,
    )


async def _run_sql_case(case: SQLValidationCase) -> SQLValidationResultRow:
    validation_arguments: dict[str, Any] = {
        "sql": case.sql,
        "dialect": "sqlite",
        **(
            {"expected_grain_ref": case.expected_grain_ref}
            if case.expected_grain_ref is not None
            else {}
        ),
    }
    if case.plan_entities:
        plan_raw = await _call_mcp(
            Path(case.project_path),
            case.source,
            "get_join_plan",
            _plan_arguments(case.plan_entities),
        )
        plan_response = PlanResponse.model_validate(plan_raw)
        if plan_response.data is None:
            raise ValueError("planned SQL case did not produce a proof")
        validation_arguments["plan_id"] = plan_response.data.proof.plan_id
    raw = await _call_mcp(
        Path(case.project_path),
        case.source,
        "validate_sql",
        validation_arguments,
    )
    response = ValidationResponse.model_validate(raw)
    findings_block = _blocking(response.findings)
    decision = (
        "block"
        if findings_block
        else "pass"
        if response.status in {"ok", "findings"}
        else "inconclusive"
        if response.status == "inconclusive"
        else "error"
    )
    extracted = _ordered_validation_edges(response)
    repairs = _repair_edges(response)
    return SQLValidationResultRow(
        case_id=case.case_id,
        sql_class=case.sql_class,
        supported_shape=case.supported_shape,
        sql_shape=case.sql_shape,
        danger_type=case.danger_type,
        decision=decision,
        extracted_edges_correct=set(extracted) == set(case.expected_edges),
        repair_uses_only_auto_usable=repairs <= set(case.auto_usable_edges),
        execution_count=(response.data.execution_count if response.data is not None else 0),
    )


async def _measure_performance(spec: PerformanceSpec) -> PerformanceMeasurement:
    project = Path(spec.project_path)
    million_start = time.perf_counter()
    await _call_mcp(
        Path(spec.million_row_project_path),
        spec.million_row_source,
        "validate_sql",
        {"sql": spec.million_row_sql, "dialect": "sqlite"},
    )
    million_seconds = time.perf_counter() - million_start
    peak_rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    if sys.platform != "darwin":
        peak_rss *= 1024
    async with _mcp_session(project, spec.source) as session:
        await _session_call(session, "get_join_plan", _plan_arguments(spec.entities))
        plan_times = [
            await _timed_session_call(
                session,
                "get_join_plan",
                _plan_arguments(spec.entities),
            )
            for _ in range(spec.repeats)
        ]
        await _session_call(
            session,
            "validate_sql",
            {"sql": spec.safe_sql, "dialect": "sqlite"},
        )
        validation_times = [
            await _timed_session_call(
                session,
                "validate_sql",
                {"sql": spec.safe_sql, "dialect": "sqlite"},
            )
            for _ in range(spec.repeats)
        ]
    metadata_times = []
    for _ in range(spec.repeats):
        metadata_start = time.perf_counter()
        await _call_mcp(
            project,
            spec.source,
            "get_join_plan",
            _plan_arguments(spec.entities),
        )
        metadata_times.append((time.perf_counter() - metadata_start) * 1_000)
    return PerformanceMeasurement(
        cached_plan_p95_ms=_p95(plan_times),
        cached_validation_p95_ms=_p95(validation_times),
        metadata_plan_p95_ms=_p95(metadata_times),
        million_row_seconds=million_seconds,
        million_row_peak_rss_bytes=peak_rss,
    )


async def _timed_session_call(
    session: ClientSession,
    tool: str,
    arguments: dict[str, Any],
) -> float:
    started = time.perf_counter()
    await _session_call(session, tool, arguments)
    return (time.perf_counter() - started) * 1_000


async def _call_mcp(
    project: Path,
    source: str | None,
    tool: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    async with _mcp_session(project, source) as session:
        return await _session_call(session, tool, arguments)


@asynccontextmanager
async def _mcp_session(
    project: Path,
    source: str | None = None,
) -> AsyncIterator[ClientSession]:
    project = project.resolve(strict=True)
    source_arguments = ["--source", source] if source is not None else ["--auto"]
    server = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "joinlint",
            "serve-mcp",
            *source_arguments,
            "--project",
            str(project),
        ],
        cwd=project,
        env=sanitized_mcp_environment(),
    )
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def _session_call(
    session: ClientSession,
    tool: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    result = await session.call_tool(tool, arguments)
    if result.isError or not isinstance(result.structuredContent, dict):
        raise ValueError(f"deterministic MCP call failed: {tool}")
    return result.structuredContent


def _ordered_plan_edges(response: PlanResponse) -> tuple[Edge, ...]:
    if response.data is None:
        return ()
    return tuple(
        (
            _public_endpoint(edge.child.entity_id, left),
            _public_endpoint(edge.parent.entity_id, right),
        )
        for edge in response.data.proof.edges
        for left, right in zip(
            edge.child.columns,
            edge.parent.columns,
            strict=True,
        )
    )


def _ordered_validation_edges(response: ValidationResponse) -> tuple[Edge, ...]:
    if response.data is None:
        return ()
    return tuple(
        _canonical_edge(_strip_source(left), _strip_source(right))
        for edge in response.data.normalized_join_graph.edges
        for left, right in edge.endpoint_pairs
    )


def _repair_edges(response: ValidationResponse) -> set[Edge]:
    if response.data is None or response.data.repair_proof is None:
        return set()
    return {
        (
            _public_endpoint(edge.child.entity_id, left),
            _public_endpoint(edge.parent.entity_id, right),
        )
        for edge in response.data.repair_proof.edges
        for left, right in zip(edge.child.columns, edge.parent.columns, strict=True)
    }


def _plan_arguments(entities: tuple[str, ...]) -> dict[str, Any]:
    refs = [
        {"ref": f"entity_{index}", "entity": entity}
        for index, entity in enumerate(entities)
    ]
    return {
        "entity_refs": refs,
        "start_ref": refs[0]["ref"],
        "expected_grain_ref": refs[0]["ref"],
    }


def _public_endpoint(entity_id: str, column: str) -> str:
    entity = entity_id.split(".", 1)[1] if entity_id.startswith("source_") and "." in entity_id else entity_id
    return f"{entity}.{column}"


def _strip_source(endpoint: str) -> str:
    return endpoint.split(".", 1)[1] if endpoint.startswith("source_") and "." in endpoint else endpoint


def _canonical_edge(left: str, right: str) -> Edge:
    first, second = sorted((left, right), key=lambda value: value.encode("utf-8"))
    return first, second


def _blocking(findings: list[Any]) -> bool:
    return any(finding.severity == "blocking" for finding in findings)


def _p95(values: list[float]) -> float:
    return statistics.quantiles(values, n=100, method="inclusive")[94]


def _validate_relative_path(value: str) -> str:
    path = Path(value)
    if (
        not value
        or path.is_absolute()
        or "\\" in value
        or ".." in path.parts
        or len(value) > 512
    ):
        raise ValueError("deterministic project paths must be bounded relative paths")
    return value


def sanitized_mcp_environment() -> dict[str, str]:
    allowed = (
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "XDG_CACHE_HOME",
    )
    return {name: os.environ[name] for name in allowed if name in os.environ}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate lineage-bound deterministic evidence")
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--lineage", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    suite = load_document(arguments.suite, DeterministicSuite)
    lineage = load_document(arguments.lineage, EvaluationLineage)
    evidence = asyncio.run(run_deterministic_evidence(suite, lineage))
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_bytes(canonical_json(evidence.model_dump(mode="json")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
