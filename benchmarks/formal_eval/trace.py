from __future__ import annotations

from typing import Any, Literal

from pydantic import model_validator

from benchmarks.agent_join.sql_edges import canonical_edge
from benchmarks.formal_eval.contracts import Edge, FailureCode, ProtocolViolation, StrictModel
from benchmarks.formal_eval.validation_ledger import VALIDATION_LEDGER_WRITE_FAILED
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
    protocol_compliant: bool
    protocol_violation: ProtocolViolation | None
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
    usable_planned_entities: set[str] = set()
    returned_edges: set[Edge] = set()
    usable_plan_ids: set[str] = set()
    plan_usable = False
    plan_inconclusive = False
    no_verified_path = False
    for call in plan_calls:
        call_entities = _planned_entities(call.arguments or {})
        planned_entities.update(call_entities)
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
            if not _permits_plan_replan(response):
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
            usable_planned_entities.update(call_entities)
            returned_edges.update(edges)
            usable_plan_ids.add(response.data.proof.plan_id)

    canonical_final = {canonical_edge(*edge) for edge in final_edges}
    completeness_basis = usable_planned_entities if plan_usable else planned_entities
    complete_entities = expected_entities <= completeness_basis
    bypassed = bool(returned_edges) and not canonical_final <= returned_edges

    validation_calls = [
        event for event in events if event.kind == "call" and event.tool == "validate_sql"
    ]
    exact_validation_call = False
    validation_passed = False
    blocking = False
    evaluation_infrastructure_failure = any(
        _is_validation_ledger_write_failure(results.get(call.call_id))
        for call in validation_calls
    )
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
        if _is_validation_ledger_write_failure(result_event):
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

    protocol_violation = None if tool_error else _protocol_violation(events, results)

    grounded = (
        plan_usable
        and complete_entities
        and canonical_final <= returned_edges
        and exact_validation_call
        and validation_passed
        and not tool_error
        and protocol_violation is None
    )
    blocking_compliant = (not submitted_sql) if blocking else None
    failure: FailureCode | None = None
    if evaluation_infrastructure_failure:
        failure = "INFRASTRUCTURE_FAILURE"
    elif tool_error:
        failure = "MCP_TOOL_ERROR"
    elif not plan_called:
        failure = "TOOL_NOT_CALLED"
    elif protocol_violation is not None:
        failure = "AGENT_BYPASS"
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
    elif submitted_sql and validation_calls and not exact_validation_call:
        failure = "FINAL_SQL_NOT_VALIDATED"
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
        protocol_compliant=not tool_error and protocol_violation is None,
        protocol_violation=protocol_violation,
        returned_edges=tuple(sorted(returned_edges)),
        failure_code=failure,
    )


def _is_validation_ledger_write_failure(event: ToolEvent | None) -> bool:
    result = event.result if event is not None else None
    error = result.get("error") if isinstance(result, dict) else None
    return (
        isinstance(error, dict)
        and error.get("code") == VALIDATION_LEDGER_WRITE_FAILED
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


def _permits_plan_replan(response: PlanResponse) -> bool:
    error = response.error
    return bool(
        response.status == "inconclusive"
        and error is not None
        and error.guidance.retryable
        and (
            (error.code == "UNCONNECTED_ENTITY_REF" and error.guidance.next_action == "fix_entity_refs")
            or (
                error.code == "GRAIN_INCOMPATIBLE"
                and error.guidance.next_action == "change_expected_grain"
            )
        )
    )


def _protocol_violation(
    events: list[ToolEvent],
    results: dict[str, ToolEvent],
) -> ProtocolViolation | None:
    indexed_calls = [
        (index, event)
        for index, event in enumerate(events)
        if event.kind == "call"
    ]
    plan_calls = [item for item in indexed_calls if item[1].tool == "get_join_plan"]
    validation_calls = [item for item in indexed_calls if item[1].tool == "validate_sql"]
    if len(plan_calls) > 2:
        return "PLAN_RETRY_LIMIT"
    if len(validation_calls) > 2:
        return "VALIDATION_RETRY_LIMIT"
    if validation_calls and any(index > validation_calls[0][0] for index, _ in plan_calls):
        return "PLAN_AFTER_VALIDATION"
    if len(plan_calls) == 2:
        first_index, first = plan_calls[0]
        second_index, second = plan_calls[1]
        first_result = results.get(first.call_id)
        if first_result is None or _event_index(events, first_result) > second_index:
            return "PLAN_RETRY_NOT_ALLOWED"
        response = PlanResponse.model_validate(first_result.result or {})
        if not _permits_plan_replan(response):
            return "PLAN_RETRY_NOT_ALLOWED"
        if not _valid_plan_retry(
            first.arguments or {},
            second.arguments or {},
            response,
        ):
            return "PLAN_RETRY_NOT_CHANGED"
        if first_index >= second_index:
            return "PLAN_RETRY_NOT_ALLOWED"
    if validation_calls and plan_calls:
        last_plan_result = results.get(plan_calls[-1][1].call_id)
        if last_plan_result is None or _event_index(events, last_plan_result) > validation_calls[0][0]:
            return "VALIDATION_BEFORE_PLAN_RESULT"
    if len(validation_calls) == 2:
        _, first = validation_calls[0]
        second_index, second = validation_calls[1]
        first_result = results.get(first.call_id)
        if first_result is None or _event_index(events, first_result) > second_index:
            return "VALIDATION_RETRY_NOT_ALLOWED"
        response = ValidationResponse.model_validate(first_result.result or {})
        if not _permits_validation_retry(response):
            return "VALIDATION_RETRY_NOT_ALLOWED"
        if not _valid_validation_retry(first.arguments or {}, second.arguments or {}):
            return "VALIDATION_RETRY_NOT_CHANGED"
    return None


def _event_index(events: list[ToolEvent], target: ToolEvent) -> int:
    return next(index for index, event in enumerate(events) if event is target)


def _valid_plan_retry(
    before: dict[str, Any],
    after: dict[str, Any],
    response: PlanResponse,
) -> bool:
    error = response.error
    if error is None:
        return False
    before_refs = _ref_map(before)
    after_refs = _ref_map(after)
    if before_refs is None or after_refs is None:
        return False
    if _plan_option(before, "max_depth", 4) != _plan_option(after, "max_depth", 4):
        return False
    if _plan_option(before, "include_alternatives", False) != _plan_option(
        after,
        "include_alternatives",
        False,
    ):
        return False
    if error.code == "GRAIN_INCOMPATIBLE":
        return bool(
            before_refs == after_refs
            and before.get("start_ref") == after.get("start_ref")
            and before.get("expected_grain_ref") != after.get("expected_grain_ref")
        )
    if error.code != "UNCONNECTED_ENTITY_REF":
        return False
    affected = set(error.guidance.affected_refs)
    if not affected:
        return False
    unaffected = set(before_refs) - affected
    if any(after_refs.get(ref) != before_refs[ref] for ref in unaffected):
        return False
    changed_affected = {
        ref for ref in affected if ref in before_refs and after_refs.get(ref) != before_refs[ref]
    }
    added = set(after_refs) - set(before_refs)
    if not changed_affected or len(added) > len(changed_affected):
        return False
    for field in ("start_ref", "expected_grain_ref"):
        original = before.get(field)
        if original is not None and original not in affected and after.get(field) != original:
            return False
        replacement = after.get(field)
        if replacement is not None and replacement not in after_refs:
            return False
    return True


def _ref_map(arguments: dict[str, Any]) -> dict[str, str] | None:
    values = arguments.get("entity_refs")
    if not isinstance(values, list):
        return None
    refs: dict[str, str] = {}
    for value in values:
        if not isinstance(value, dict):
            return None
        ref = value.get("ref")
        entity = value.get("entity")
        if not isinstance(ref, str) or not isinstance(entity, str) or ref in refs:
            return None
        refs[ref] = entity
    return refs


def _plan_option(arguments: dict[str, Any], key: str, default: object) -> object:
    return arguments.get(key, default)


def _permits_validation_retry(response: ValidationResponse) -> bool:
    guidance = (
        (response.error.guidance,)
        if response.error is not None
        else tuple(finding.guidance for finding in response.findings)
    )
    return bool(guidance) and all(
        item.retryable and item.next_action == "revise_sql" for item in guidance
    )


def _valid_validation_retry(before: dict[str, Any], after: dict[str, Any]) -> bool:
    before_sql = before.get("sql")
    after_sql = after.get("sql")
    if not isinstance(before_sql, str) or not isinstance(after_sql, str) or before_sql == after_sql:
        return False
    keys = ("dialect", "source_id", "plan_id", "expected_grain_ref")
    defaults = {"dialect": "sqlite", "source_id": None, "plan_id": None, "expected_grain_ref": None}
    return all(before.get(key, defaults[key]) == after.get(key, defaults[key]) for key in keys)


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
