from __future__ import annotations

from typing import Any, Literal

from pydantic import model_validator

from benchmarks.agent_join.sql_edges import canonical_edge
from benchmarks.formal_eval.contracts import Edge, FailureCode, StrictModel
from joinlint.mcp_contracts import (
    GetJoinPlanResponse as PlanResponse,
)
from joinlint.mcp_contracts import (
    ValidateSQLResponse as ValidationResponse,
)


ToolName = Literal["get_join_plan", "validate_sql"]


class ToolEvent(StrictModel):
    kind: Literal["call", "result"]
    call_id: str
    tool: ToolName
    arguments: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    transport_error: bool = False

    @model_validator(mode="after")
    def require_event_payload(self) -> ToolEvent:
        if self.kind == "call" and (self.arguments is None or self.result is not None):
            raise ValueError("tool calls require arguments only")
        if self.kind == "result" and (self.result is None or self.arguments is not None):
            raise ValueError("tool results require result only")
        if not self.call_id or len(self.call_id) > 256:
            raise ValueError("tool call ID must be non-empty and bounded")
        return self


class TraceAssessment(StrictModel):
    plan_called: bool
    plan_usable: bool
    complete_entity_planning: bool
    final_sql_validated: bool
    validation_passed: bool
    mcp_grounded: bool
    blocking_applicable: bool
    blocking_compliant: bool | None
    bypassed: bool
    tool_error: bool
    returned_edges: tuple[Edge, ...]
    failure_code: FailureCode | None


def assess_trace(
    events: list[ToolEvent],
    *,
    expected_entities: set[str],
    final_sql: str,
    final_edges: set[Edge],
    submitted_sql: bool,
) -> TraceAssessment:
    calls: dict[str, ToolEvent] = {}
    results: dict[str, ToolEvent] = {}
    tool_error = False
    for event in events:
        target = calls if event.kind == "call" else results
        if event.call_id in target:
            tool_error = True
            continue
        target[event.call_id] = event
    for call_id, result in results.items():
        call = calls.get(call_id)
        if call is None or call.tool != result.tool or result.transport_error:
            tool_error = True
    if set(calls) != set(results):
        tool_error = True

    plan_calls = [event for event in events if event.kind == "call" and event.tool == "get_join_plan"]
    plan_called = bool(plan_calls)
    planned_entities: set[str] = set()
    returned_edges: set[Edge] = set()
    usable_plan_ids: set[str] = set()
    plan_usable = False
    plan_inconclusive = False
    no_verified_path = False
    for call in plan_calls:
        planned_entities.update(_planned_entities(call.arguments or {}))
        result_event = results.get(call.call_id)
        if result_event is None:
            continue
        try:
            response = PlanResponse.model_validate(result_event.result or {})
        except ValueError:
            tool_error = True
            continue
        if response.status == "inconclusive":
            if response.error is not None and response.error.code == "NO_VERIFIED_PATH":
                no_verified_path = True
            if not _permits_entity_ref_replan(response):
                plan_inconclusive = True
        edges = _plan_edges(response)
        if (
            response.status == "ok"
            and response.data is not None
            and response.data.lifecycle.status == "current"
            and edges
            and not _has_blocking(response.findings)
        ):
            plan_usable = True
            returned_edges.update(edges)
            usable_plan_ids.add(response.data.proof.plan_id)

    canonical_final = {canonical_edge(*edge) for edge in final_edges}
    complete_entities = expected_entities <= planned_entities
    bypassed = bool(returned_edges) and not canonical_final <= returned_edges

    validation_calls = [
        event for event in events if event.kind == "call" and event.tool == "validate_sql"
    ]
    exact_validation_call = False
    validation_passed = False
    blocking = False
    for call in validation_calls:
        arguments = call.arguments or {}
        if submitted_sql and arguments.get("sql") != final_sql:
            continue
        if submitted_sql:
            exact_validation_call = True
        result_event = results.get(call.call_id)
        if result_event is None:
            continue
        try:
            response = ValidationResponse.model_validate(result_event.result or {})
        except ValueError:
            tool_error = True
            continue
        response_edges = _validation_edges(response)
        blocking = blocking or _has_blocking(response.findings) or response.status == "inconclusive"
        plan_bound = arguments.get("plan_id") in usable_plan_ids
        if (
            submitted_sql
            and response.status == "ok"
            and not _has_blocking(response.findings)
            and response_edges == canonical_final
            and response.data is not None
            and response.data.proof_matched is True
            and plan_bound
        ):
            validation_passed = True

    grounded = (
        plan_usable
        and complete_entities
        and canonical_final <= returned_edges
        and exact_validation_call
        and validation_passed
        and not tool_error
    )
    blocking_compliant = (not submitted_sql) if blocking else None
    failure: FailureCode | None = None
    if tool_error:
        failure = "MCP_TOOL_ERROR"
    elif not plan_called:
        failure = "TOOL_NOT_CALLED"
    elif not complete_entities:
        failure = "ENTITY_SET_INCOMPLETE"
    elif no_verified_path:
        failure = "NO_VERIFIED_PATH"
    elif plan_inconclusive or not plan_usable:
        failure = "PLAN_INCONCLUSIVE"
    elif bypassed:
        failure = "AGENT_BYPASS"
    elif canonical_final and not canonical_final <= returned_edges:
        failure = "SQL_NOT_GROUNDED"
    elif submitted_sql and not exact_validation_call:
        failure = "VALIDATION_NOT_CALLED"
    elif blocking:
        failure = "VALIDATION_BLOCKED"
    elif not exact_validation_call:
        failure = "VALIDATION_NOT_CALLED"
    elif not grounded:
        failure = "SQL_NOT_GROUNDED"

    return TraceAssessment(
        plan_called=plan_called,
        plan_usable=plan_usable,
        complete_entity_planning=complete_entities,
        final_sql_validated=exact_validation_call,
        validation_passed=validation_passed,
        mcp_grounded=grounded,
        blocking_applicable=blocking,
        blocking_compliant=blocking_compliant,
        bypassed=bypassed,
        tool_error=tool_error,
        returned_edges=tuple(sorted(returned_edges)),
        failure_code=failure,
    )


def _planned_entities(arguments: dict[str, Any]) -> set[str]:
    values = arguments.get("entity_refs")
    if not isinstance(values, list):
        return set()
    entities = {
        value.get("entity")
        for value in values
        if isinstance(value, dict) and isinstance(value.get("entity"), str)
    }
    return {str(value) for value in entities}


def _permits_entity_ref_replan(response: PlanResponse) -> bool:
    error = response.error
    return bool(
        response.status == "inconclusive"
        and error is not None
        and error.code == "UNCONNECTED_ENTITY_REF"
        and error.guidance.retryable
        and error.guidance.next_action == "fix_entity_refs"
    )


def _plan_edges(response: PlanResponse) -> set[Edge]:
    if response.data is None:
        return set()
    edges: set[Edge] = set()
    for edge in response.data.proof.edges:
        for child, parent in zip(edge.child.columns, edge.parent.columns, strict=True):
            edges.add(
                canonical_edge(
                    _public_endpoint(edge.child.entity_id, child),
                    _public_endpoint(edge.parent.entity_id, parent),
                )
            )
    return edges


def _validation_edges(response: ValidationResponse) -> set[Edge]:
    if response.data is None:
        return set()
    return {
        canonical_edge(_strip_source(left), _strip_source(right))
        for edge in response.data.normalized_join_graph.edges
        for left, right in edge.endpoint_pairs
    }


def _public_endpoint(entity_id: str, column: str) -> str:
    entity = entity_id.split(".", 1)[1] if entity_id.startswith("source_") and "." in entity_id else entity_id
    return f"{entity}.{column}"


def _strip_source(endpoint: str) -> str:
    return endpoint.split(".", 1)[1] if endpoint.startswith("source_") and "." in endpoint else endpoint


def _has_blocking(findings: tuple[Any, ...]) -> bool:
    return any(finding.severity == "blocking" for finding in findings)
