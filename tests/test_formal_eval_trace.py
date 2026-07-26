from __future__ import annotations

import pytest
from pydantic import ValidationError

from benchmarks.agent_join.sql_edges import canonical_edge
from benchmarks.formal_eval.trace import ToolEvent, ValidationResponse, assess_trace


def _plan_result(*, columns: tuple[list[str], list[str]] | None = None) -> dict[str, object]:
    left, right = columns or (["customer_id"], ["id"])
    return {
        "schema_version": 2,
        "command": "get_join_plan",
        "status": "ok",
        "data": {
            "proof": {
                "schema_version": 2,
                "plan_id": "1" * 64,
                "claim_scope": "physical_join_only",
                "source_id": "sales",
                "snapshot_id": "2" * 64,
                "policy_version": "declared-curated-v1",
                "entity_refs": [
                    {"ref": "orders", "entity": "orders"},
                    {"ref": "customers", "entity": "customers"},
                ],
                "start_ref": "orders",
                "expected_grain_ref": "orders",
                "max_depth": 4,
                "edges": [
                    {
                        "from_ref": "orders",
                        "to_ref": "customers",
                        "relationship_id": "3" * 64,
                        "evidence_id": "4" * 64,
                        "child": {"entity_id": "orders", "columns": left},
                        "parent": {"entity_id": "customers", "columns": right},
                        "predicates": [
                            f"orders.{left_column} = customers.{right_column}"
                            for left_column, right_column in zip(left, right, strict=True)
                        ],
                        "traversal": "forward",
                        "cardinality": "many_to_one",
                        "provenance": "declared",
                    }
                ],
                "alternatives": [],
                "verified_at": "2026-07-26T00:00:00Z",
                "freshness_checked_at": "2026-07-26T00:00:00Z",
            },
            "lifecycle": {
                "status": "current",
                "reason": None,
                "freshness_checked_at": "2026-07-26T00:00:00Z",
            },
            "validated_scope": ["physical_join_endpoints", "proof_binding"],
            "not_validated_scope": ["answer_correctness"],
            "execution_count": 0,
        },
        "findings": [],
        "error": None,
    }


def _validation_result(
    *, blocking: bool = False, edges: list[list[str]] | None = None
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "command": "validate_sql",
        "status": "findings" if blocking else "ok",
        "data": {
            "normalized_join_graph": {
                "source_id": "sales",
                "entity_refs": [],
                "edges": [
                    {
                        "left_ref": f"i{index}",
                        "right_ref": f"j{index}",
                        "endpoint_pairs": [edge],
                        "join_kind": "inner",
                    }
                    for index, edge in enumerate(edges or [])
                ],
            },
            "matched_relationship_ids": [],
            "matched_evidence_ids": [],
            "proof_matched": True,
            "repair_proof": None,
            "validated_scope": ["physical_join_endpoints", "proof_binding"],
            "not_validated_scope": ["answer_correctness"],
            "execution_count": 0,
        },
        "findings": (
            [{"severity": "blocking", "code": "GRAIN_CHANGE", "message": "GRAIN_CHANGE"}]
            if blocking
            else []
        ),
        "error": None,
    }


def test_trace_requires_complete_plan_grounding_and_exact_final_validation() -> None:
    sql = "SELECT * FROM orders JOIN customers ON orders.customer_id = customers.id"
    events = [
        ToolEvent(
            kind="call",
            call_id="plan",
            tool="get_join_plan",
            arguments={
                "entity_refs": [
                    {"ref": "orders", "entity": "orders"},
                    {"ref": "customers", "entity": "customers"},
                ],
                "start_ref": "orders",
            },
        ),
        ToolEvent(
            kind="result",
            call_id="plan",
            tool="get_join_plan",
            result=_plan_result(),
        ),
        ToolEvent(
            kind="call",
            call_id="validate",
            tool="validate_sql",
            arguments={"sql": sql, "dialect": "sqlite", "plan_id": "1" * 64},
        ),
        ToolEvent(
            kind="result",
            call_id="validate",
            tool="validate_sql",
            result=_validation_result(edges=[["customers.id", "orders.customer_id"]]),
        ),
    ]

    result = assess_trace(
        events,
        expected_entities={"orders", "customers"},
        final_sql=sql,
        final_edges={canonical_edge("orders.customer_id", "customers.id")},
        submitted_sql=True,
    )

    assert result.mcp_grounded
    assert result.final_sql_validated
    assert result.failure_code is None


def test_validation_contract_requires_zero_execution_attestation() -> None:
    missing = _validation_result()
    del missing["data"]["execution_count"]  # type: ignore[index]
    with pytest.raises(ValidationError):
        ValidationResponse.model_validate(missing)

    nonzero = _validation_result()
    nonzero["data"]["execution_count"] = 1  # type: ignore[index]
    with pytest.raises(ValidationError):
        ValidationResponse.model_validate(nonzero)


def test_trace_classifies_agent_bypass() -> None:
    sql = "SELECT * FROM orders JOIN customers ON orders.id = customers.id"
    events = [
        ToolEvent(
            kind="call",
            call_id="plan",
            tool="get_join_plan",
            arguments={
                "entity_refs": [
                    {"ref": "orders", "entity": "orders"},
                    {"ref": "customers", "entity": "customers"},
                ],
                "start_ref": "orders",
            },
        ),
        ToolEvent(
            kind="result",
            call_id="plan",
            tool="get_join_plan",
            result=_plan_result(),
        ),
    ]

    result = assess_trace(
        events,
        expected_entities={"orders", "customers"},
        final_sql=sql,
        final_edges={canonical_edge("orders.id", "customers.id")},
        submitted_sql=True,
    )

    assert result.bypassed
    assert result.failure_code == "AGENT_BYPASS"


def test_blocking_validation_requires_observable_abstention() -> None:
    sql = "SELECT * FROM orders JOIN customers ON orders.customer_id = customers.id"
    events = [
        ToolEvent(
            kind="call",
            call_id="plan",
            tool="get_join_plan",
            arguments={
                "entity_refs": [
                    {"ref": "orders", "entity": "orders"},
                    {"ref": "customers", "entity": "customers"},
                ],
                "start_ref": "orders",
            },
        ),
        ToolEvent(
            kind="result",
            call_id="plan",
            tool="get_join_plan",
            result=_plan_result(),
        ),
        ToolEvent(
            kind="call",
            call_id="validate",
            tool="validate_sql",
            arguments={"sql": sql, "plan_id": "1" * 64},
        ),
        ToolEvent(
            kind="result",
            call_id="validate",
            tool="validate_sql",
            result=_validation_result(blocking=True),
        ),
    ]

    result = assess_trace(
        events,
        expected_entities={"orders", "customers"},
        final_sql="",
        final_edges=set(),
        submitted_sql=False,
    )

    assert result.blocking_applicable
    assert result.blocking_compliant
    assert not result.final_sql_validated
    assert result.failure_code == "VALIDATION_BLOCKED"


def test_trace_rejects_blocking_or_non_current_plan_edges() -> None:
    plan = _plan_result()
    plan["status"] = "findings"
    plan["findings"] = [
        {"severity": "blocking", "code": "UNSAFE_PLAN", "message": "UNSAFE_PLAN"}
    ]
    events = [
        ToolEvent(
            kind="call",
            call_id="plan",
            tool="get_join_plan",
            arguments={
                "entity_refs": [
                    {"ref": "orders", "entity": "orders"},
                    {"ref": "customers", "entity": "customers"},
                ],
                "start_ref": "orders",
            },
        ),
        ToolEvent(kind="result", call_id="plan", tool="get_join_plan", result=plan),
    ]

    result = assess_trace(
        events,
        expected_entities={"orders", "customers"},
        final_sql="SELECT 1",
        final_edges=set(),
        submitted_sql=True,
    )

    assert not result.plan_usable
    assert result.failure_code == "PLAN_INCONCLUSIVE"


def test_trace_expands_compound_plan_edges() -> None:
    plan = _plan_result(columns=(["tenant_id", "customer_id"], ["tenant_id", "id"]))
    events = [
        ToolEvent(
            kind="call",
            call_id="plan",
            tool="get_join_plan",
            arguments={
                "entity_refs": [
                    {"ref": "orders", "entity": "orders"},
                    {"ref": "customers", "entity": "customers"},
                ],
                "start_ref": "orders",
            },
        ),
        ToolEvent(kind="result", call_id="plan", tool="get_join_plan", result=plan),
    ]

    result = assess_trace(
        events,
        expected_entities={"orders", "customers"},
        final_sql="SELECT 1",
        final_edges={
            canonical_edge("orders.tenant_id", "customers.tenant_id"),
            canonical_edge("orders.customer_id", "customers.id"),
        },
        submitted_sql=True,
    )

    assert result.plan_usable
    assert not result.bypassed
