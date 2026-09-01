from __future__ import annotations

import pytest
from pydantic import ValidationError

from benchmarks.agent_join.sql_edges import canonical_edge
from benchmarks.formal_eval.trace import ToolEvent, ValidationResponse, assess_trace
from benchmarks.formal_eval.validation_ledger import VALIDATION_LEDGER_WRITE_FAILED


def _guidance(
    next_action: str = "stop",
    *,
    retryable: bool = False,
) -> dict[str, object]:
    return {
        "retryable": retryable,
        "next_action": next_action,
        "affected_refs": [],
        "blocking_relationship_ids": [],
        "freshness_reason": None,
    }


def _plan_result(*, columns: tuple[list[str], list[str]] | None = None) -> dict[str, object]:
    left, right = columns or (["customer_id"], ["id"])
    return {
        "schema_version": 3,
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


def _unconnected_entity_ref_result() -> dict[str, object]:
    return {
        "schema_version": 3,
        "command": "get_join_plan",
        "status": "inconclusive",
        "data": None,
        "findings": [],
        "error": {
            "code": "UNCONNECTED_ENTITY_REF",
            "message": "UNCONNECTED_ENTITY_REF",
            "guidance": {
                **_guidance("fix_entity_refs", retryable=True),
                "affected_refs": ["orphan"],
            },
        },
    }


def _grain_incompatible_result() -> dict[str, object]:
    return {
        "schema_version": 3,
        "command": "get_join_plan",
        "status": "inconclusive",
        "data": None,
        "findings": [],
        "error": {
            "code": "GRAIN_INCOMPATIBLE",
            "message": "GRAIN_INCOMPATIBLE",
            "guidance": {
                **_guidance("change_expected_grain", retryable=True),
                "affected_refs": ["customers"],
            },
        },
    }


def _validation_result(
    *,
    blocking: bool = False,
    retryable: bool = False,
    edges: list[list[str]] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 3,
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
            [
                {
                    "severity": "blocking",
                    "code": "GRAIN_INCOMPATIBLE",
                    "message": "GRAIN_INCOMPATIBLE",
                    "guidance": _guidance(
                        "revise_sql" if retryable else "stop",
                        retryable=retryable,
                    ),
                }
            ]
            if blocking
            else []
        ),
        "error": None,
    }


def _ledger_write_failure_result() -> dict[str, object]:
    return {
        "schema_version": 3,
        "command": "validate_sql",
        "status": "error",
        "data": None,
        "findings": [],
        "error": {
            "code": VALIDATION_LEDGER_WRITE_FAILED,
            "message": VALIDATION_LEDGER_WRITE_FAILED,
            "guidance": _guidance(),
        },
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


def test_trace_distinguishes_final_sql_drift_from_missing_validation() -> None:
    validated_sql = "SELECT * FROM orders JOIN customers ON orders.customer_id = customers.id"
    final_sql = validated_sql + " WHERE orders.id > 0"
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
        ToolEvent(kind="result", call_id="plan", tool="get_join_plan", result=_plan_result()),
        ToolEvent(
            kind="call",
            call_id="validate",
            tool="validate_sql",
            arguments={"sql": validated_sql, "dialect": "sqlite", "plan_id": "1" * 64},
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
        final_sql=final_sql,
        final_edges={canonical_edge("orders.customer_id", "customers.id")},
        submitted_sql=True,
    )

    assert result.final_sql_validated is False
    assert result.failure_code == "FINAL_SQL_NOT_VALIDATED"


def test_trace_classifies_ledger_write_failure_as_evaluation_infrastructure() -> None:
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
        ToolEvent(kind="result", call_id="plan", tool="get_join_plan", result=_plan_result()),
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
            result=_ledger_write_failure_result(),
        ),
    ]

    result = assess_trace(
        events,
        expected_entities={"orders", "customers"},
        final_sql=sql,
        final_edges={canonical_edge("orders.customer_id", "customers.id")},
        submitted_sql=True,
    )

    assert result.failure_code == "INFRASTRUCTURE_FAILURE"
    assert result.tool_error is False


def test_trace_allows_a_grounded_replan_after_unconnected_entity_ref() -> None:
    sql = "SELECT * FROM orders JOIN customers ON orders.customer_id = customers.id"
    events = [
        ToolEvent(
            kind="call",
            call_id="first-plan",
            tool="get_join_plan",
            arguments={
                "entity_refs": [
                    {"ref": "orders", "entity": "orders"},
                    {"ref": "customers", "entity": "customers"},
                    {"ref": "orphan", "entity": "orphan"},
                ],
                "start_ref": "orphan",
            },
        ),
        ToolEvent(
            kind="result",
            call_id="first-plan",
            tool="get_join_plan",
            result=_unconnected_entity_ref_result(),
        ),
        ToolEvent(
            kind="call",
            call_id="replan",
            tool="get_join_plan",
            arguments={
                "entity_refs": [
                    {"ref": "orders", "entity": "orders"},
                    {"ref": "customers", "entity": "customers"},
                ],
                "start_ref": "orders",
            },
        ),
        ToolEvent(kind="result", call_id="replan", tool="get_join_plan", result=_plan_result()),
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
    assert result.failure_code is None


def test_trace_allows_a_grounded_replan_after_incompatible_grain() -> None:
    sql = "SELECT * FROM orders JOIN customers ON orders.customer_id = customers.id"
    events = [
        ToolEvent(
            kind="call",
            call_id="first-plan",
            tool="get_join_plan",
            arguments={
                "entity_refs": [
                    {"ref": "orders", "entity": "orders"},
                    {"ref": "customers", "entity": "customers"},
                ],
                "start_ref": "customers",
                "expected_grain_ref": "customers",
            },
        ),
        ToolEvent(
            kind="result",
            call_id="first-plan",
            tool="get_join_plan",
            result=_grain_incompatible_result(),
        ),
        ToolEvent(
            kind="call",
            call_id="replan",
            tool="get_join_plan",
            arguments={
                "entity_refs": [
                    {"ref": "orders", "entity": "orders"},
                    {"ref": "customers", "entity": "customers"},
                ],
                "start_ref": "customers",
                "expected_grain_ref": "orders",
            },
        ),
        ToolEvent(
            kind="result",
            call_id="replan",
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
    assert result.failure_code is None


def test_trace_rejects_an_unchanged_replan() -> None:
    arguments = {
        "entity_refs": [
            {"ref": "orders", "entity": "orders"},
            {"ref": "customers", "entity": "customers"},
        ],
        "start_ref": "customers",
        "expected_grain_ref": "customers",
    }
    events = [
        ToolEvent(
            kind="call",
            call_id="first-plan",
            tool="get_join_plan",
            arguments=arguments,
        ),
        ToolEvent(
            kind="result",
            call_id="first-plan",
            tool="get_join_plan",
            result=_grain_incompatible_result(),
        ),
        ToolEvent(
            kind="call",
            call_id="replan",
            tool="get_join_plan",
            arguments=arguments,
        ),
        ToolEvent(
            kind="result",
            call_id="replan",
            tool="get_join_plan",
            result=_grain_incompatible_result(),
        ),
    ]

    result = assess_trace(
        events,
        expected_entities={"orders", "customers"},
        final_sql="",
        final_edges=set(),
        submitted_sql=False,
    )

    assert not result.protocol_compliant
    assert result.protocol_violation == "PLAN_RETRY_NOT_CHANGED"
    assert result.failure_code == "AGENT_BYPASS"


def test_trace_requires_the_usable_replan_to_keep_every_expected_entity() -> None:
    first_result = _unconnected_entity_ref_result()
    first_result["error"]["guidance"]["affected_refs"] = ["customers"]  # type: ignore[index]
    replan_result = _plan_result()
    proof = replan_result["data"]["proof"]  # type: ignore[index]
    proof["entity_refs"] = [  # type: ignore[index]
        {"ref": "orders", "entity": "orders"},
        {"ref": "orphan", "entity": "orphan"},
    ]
    proof["edges"][0]["to_ref"] = "orphan"  # type: ignore[index]
    proof["edges"][0]["parent"] = {"entity_id": "orphan", "columns": ["id"]}  # type: ignore[index]
    proof["edges"][0]["predicates"] = ["orders.customer_id = orphan.id"]  # type: ignore[index]
    events = [
        ToolEvent(
            kind="call",
            call_id="first-plan",
            tool="get_join_plan",
            arguments={
                "entity_refs": [
                    {"ref": "orders", "entity": "orders"},
                    {"ref": "customers", "entity": "customers"},
                    {"ref": "orphan", "entity": "orphan"},
                ],
                "start_ref": "orders",
            },
        ),
        ToolEvent(
            kind="result",
            call_id="first-plan",
            tool="get_join_plan",
            result=first_result,
        ),
        ToolEvent(
            kind="call",
            call_id="replan",
            tool="get_join_plan",
            arguments={
                "entity_refs": [
                    {"ref": "orders", "entity": "orders"},
                    {"ref": "orphan", "entity": "orphan"},
                ],
                "start_ref": "orders",
            },
        ),
        ToolEvent(
            kind="result",
            call_id="replan",
            tool="get_join_plan",
            result=replan_result,
        ),
    ]

    result = assess_trace(
        events,
        expected_entities={"orders", "customers"},
        final_sql="",
        final_edges=set(),
        submitted_sql=False,
    )

    assert result.protocol_compliant
    assert result.plan_usable
    assert not result.complete_entity_planning
    assert result.failure_code == "ENTITY_SET_INCOMPLETE"


def test_trace_allows_one_changed_sql_only_validation_retry() -> None:
    first_sql = "SELECT * FROM orders JOIN customers ON orders.id = customers.id"
    final_sql = "SELECT * FROM orders JOIN customers ON orders.customer_id = customers.id"
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
        ToolEvent(kind="result", call_id="plan", tool="get_join_plan", result=_plan_result()),
        ToolEvent(
            kind="call",
            call_id="first-validation",
            tool="validate_sql",
            arguments={"sql": first_sql, "plan_id": "1" * 64},
        ),
        ToolEvent(
            kind="result",
            call_id="first-validation",
            tool="validate_sql",
            result=_validation_result(blocking=True, retryable=True),
        ),
        ToolEvent(
            kind="call",
            call_id="final-validation",
            tool="validate_sql",
            arguments={"sql": final_sql, "plan_id": "1" * 64},
        ),
        ToolEvent(
            kind="result",
            call_id="final-validation",
            tool="validate_sql",
            result=_validation_result(edges=[["customers.id", "orders.customer_id"]]),
        ),
    ]

    result = assess_trace(
        events,
        expected_entities={"orders", "customers"},
        final_sql=final_sql,
        final_edges={canonical_edge("orders.customer_id", "customers.id")},
        submitted_sql=True,
    )

    assert result.protocol_compliant
    assert result.mcp_grounded
    assert result.failure_code is None


def test_trace_rejects_changing_grain_during_validation_retry() -> None:
    first_sql = "SELECT * FROM orders JOIN customers ON orders.id = customers.id"
    final_sql = "SELECT * FROM orders JOIN customers ON orders.customer_id = customers.id"
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
        ToolEvent(kind="result", call_id="plan", tool="get_join_plan", result=_plan_result()),
        ToolEvent(
            kind="call",
            call_id="first-validation",
            tool="validate_sql",
            arguments={"sql": first_sql, "plan_id": "1" * 64},
        ),
        ToolEvent(
            kind="result",
            call_id="first-validation",
            tool="validate_sql",
            result=_validation_result(blocking=True, retryable=True),
        ),
        ToolEvent(
            kind="call",
            call_id="final-validation",
            tool="validate_sql",
            arguments={
                "sql": final_sql,
                "plan_id": "1" * 64,
                "expected_grain_ref": "customers",
            },
        ),
        ToolEvent(
            kind="result",
            call_id="final-validation",
            tool="validate_sql",
            result=_validation_result(edges=[["customers.id", "orders.customer_id"]]),
        ),
    ]

    result = assess_trace(
        events,
        expected_entities={"orders", "customers"},
        final_sql=final_sql,
        final_edges={canonical_edge("orders.customer_id", "customers.id")},
        submitted_sql=True,
    )

    assert result.protocol_violation == "VALIDATION_RETRY_NOT_CHANGED"
    assert not result.mcp_grounded
    assert result.failure_code == "AGENT_BYPASS"


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
        {
            "severity": "blocking",
            "code": "UNSAFE_PLAN",
            "message": "UNSAFE_PLAN",
            "guidance": _guidance(),
        }
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
