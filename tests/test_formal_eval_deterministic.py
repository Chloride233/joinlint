from __future__ import annotations

import asyncio

import pytest

from benchmarks.formal_eval import deterministic


def plan_response(provenance: str) -> dict[str, object]:
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
                        "child": {"entity_id": "orders", "columns": ["customer_id"]},
                        "parent": {"entity_id": "customers", "columns": ["id"]},
                        "predicates": ["orders.customer_id = customers.id"],
                        "traversal": "forward",
                        "cardinality": "many_to_one",
                        "provenance": provenance,
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


def validation_response(left: str, right: str) -> dict[str, object]:
    return {
        "schema_version": 3,
        "command": "validate_sql",
        "status": "ok",
        "data": {
            "normalized_join_graph": {
                "source_id": "sales",
                "entity_refs": [],
                "edges": [
                    {
                        "left_ref": "i0",
                        "right_ref": "i1",
                        "endpoint_pairs": [[left, right]],
                        "join_kind": "inner",
                    }
                ],
            },
            "matched_relationship_ids": [],
            "matched_evidence_ids": [],
            "proof_matched": None,
            "repair_proof": None,
            "validated_scope": ["physical_join_endpoints"],
            "not_validated_scope": ["answer_correctness", "proof_binding"],
            "execution_count": 0,
        },
        "findings": [],
        "error": None,
    }


def test_relationship_evidence_is_derived_from_strict_mcp_response(monkeypatch) -> None:
    async def fake_call(project, source, tool, arguments):  # type: ignore[no-untyped-def]
        assert source is None
        assert tool == "get_join_plan"
        return plan_response("declared")

    monkeypatch.setattr(deterministic, "_call_mcp", fake_call)
    case = deterministic.RelationshipEvidenceCase(
        case_id="case-1",
        database_id="database-1",
        variant_group="database-1",
        project_path="projects/database-1",
        entities=("orders", "customers"),
        expected_edges=(("orders.customer_id", "customers.id"),),
        corpus="natural",
        source_type="sqlite",
        domain="commerce",
        split="confirmatory",
        provenance="declared",
        eligible=True,
        expected_relation=True,
        fk_state="visible",
        reviewer_labels=(True, True),
        adjudicated_label=True,
    )

    row = asyncio.run(deterministic._run_relationship_case(case))

    assert row.prediction_correct is True
    assert row.auto_usable is True
    assert row.predicted_edges == case.expected_edges


def test_sql_evidence_is_not_accepted_without_matching_extracted_edges(monkeypatch) -> None:
    async def fake_call(project, source, tool, arguments):  # type: ignore[no-untyped-def]
        assert source == "data.sqlite"
        assert tool == "validate_sql"
        assert arguments["expected_grain_ref"] == "orders"
        return validation_response("customers.id", "orders.id")

    monkeypatch.setattr(deterministic, "_call_mcp", fake_call)
    case = deterministic.SQLValidationCase(
        case_id="sql-1",
        project_path="projects/database-1",
        source="data.sqlite",
        sql="SELECT * FROM orders JOIN customers ON orders.id = customers.id",
        sql_class="safe",
        supported_shape=True,
        sql_shape="alias",
        expected_edges=(("customers.id", "orders.customer_id"),),
        auto_usable_edges=(("customers.id", "orders.customer_id"),),
        expected_grain_ref="orders",
    )

    row = asyncio.run(deterministic._run_sql_case(case))

    assert row.decision == "pass"
    assert row.extracted_edges_correct is False


def test_sql_case_can_bind_validation_to_a_frozen_plan(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_call(project, source, tool, arguments):  # type: ignore[no-untyped-def]
        calls.append(tool)
        if tool == "get_join_plan":
            assert arguments["entity_refs"] == [
                {"ref": "entity_0", "entity": "orders"},
                {"ref": "entity_1", "entity": "customers"},
            ]
            return plan_response("declared")
        assert arguments["plan_id"] == "1" * 64
        return validation_response("orders.customer_id", "customers.id")

    monkeypatch.setattr(deterministic, "_call_mcp", fake_call)
    case = deterministic.SQLValidationCase(
        case_id="sql-planned",
        project_path="projects/database-1",
        sql="SELECT * FROM orders",
        sql_class="dangerous",
        supported_shape=True,
        sql_shape="wrong_edge",
        danger_type="missing_required_edge",
        expected_edges=(("customers.id", "orders.customer_id"),),
        auto_usable_edges=(("customers.id", "orders.customer_id"),),
        plan_entities=("orders", "customers"),
    )

    row = asyncio.run(deterministic._run_sql_case(case))

    assert calls == ["get_join_plan", "validate_sql"]
    assert row.extracted_edges_correct is True


def test_relationship_evidence_requires_matching_runtime_provenance(monkeypatch) -> None:
    async def fake_call(project, source, tool, arguments):  # type: ignore[no-untyped-def]
        return plan_response("declared")

    monkeypatch.setattr(deterministic, "_call_mcp", fake_call)
    case = deterministic.RelationshipEvidenceCase(
        case_id="case-1",
        database_id="database-1",
        variant_group="database-1",
        project_path="projects/database-1",
        entities=("orders", "customers"),
        expected_edges=(("orders.customer_id", "customers.id"),),
        corpus="natural",
        source_type="sqlite",
        domain="commerce",
        split="confirmatory",
        provenance="verified",
        eligible=True,
        expected_relation=True,
        fk_state="ablated",
        reviewer_labels=(True, True),
        adjudicated_label=True,
    )

    row = asyncio.run(deterministic._run_relationship_case(case))

    assert row.prediction_correct is True
    assert row.auto_usable is False


def test_deterministic_project_paths_must_be_relative_and_bounded() -> None:
    payload = {
        "case_id": "case-1",
        "database_id": "database-1",
        "variant_group": "database-1",
        "project_path": "/private/unlocked",
        "entities": ("orders", "customers"),
        "expected_edges": (("orders.customer_id", "customers.id"),),
        "corpus": "natural",
        "source_type": "sqlite",
        "domain": "commerce",
        "split": "confirmatory",
        "provenance": "verified",
        "eligible": True,
        "expected_relation": True,
        "fk_state": "absent",
        "reviewer_labels": (True, True),
        "adjudicated_label": True,
    }

    with pytest.raises(ValueError, match="bounded relative paths"):
        deterministic.RelationshipEvidenceCase.model_validate(payload)
    payload["project_path"] = "sealed/../unlocked"
    with pytest.raises(ValueError, match="bounded relative paths"):
        deterministic.RelationshipEvidenceCase.model_validate(payload)


def test_deterministic_mcp_environment_drops_credentials(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sentinel-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sentinel-anthropic")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "sentinel-modal")
    monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/joinlint-cache")

    environment = deterministic.sanitized_mcp_environment()

    assert "OPENAI_API_KEY" not in environment
    assert "ANTHROPIC_API_KEY" not in environment
    assert "MODAL_TOKEN_SECRET" not in environment
    assert environment["XDG_CACHE_HOME"] == "/tmp/joinlint-cache"
    assert not any("sentinel" in value for value in environment.values())


def test_relationship_database_variants_cannot_cross_splits() -> None:
    base = {
        "database_id": "database-1",
        "variant_group": "database-family-1",
        "project_path": "sealed/database-1",
        "entities": ("orders", "customers"),
        "expected_edges": (("orders.customer_id", "customers.id"),),
        "corpus": "natural",
        "source_type": "sqlite",
        "domain": "commerce",
        "provenance": "declared",
        "eligible": True,
        "expected_relation": True,
        "fk_state": "visible",
    }
    first = deterministic.RelationshipEvidenceCase(
        case_id="case-1", split="development", **base
    )
    second = deterministic.RelationshipEvidenceCase(
        case_id="case-2", split="confirmatory", **base
    )

    with pytest.raises(ValueError, match="cannot cross splits"):
        deterministic.DeterministicSuite(
            relationship_scope="declared_only",
            relationship_cases=(first, second),
            sql_validation_cases=(),
            performance=deterministic.PerformanceSpec(
                project_path="sealed/project",
                entities=("orders", "customers"),
                safe_sql="SELECT 1",
                million_row_project_path="sealed/million",
                million_row_sql="SELECT 1",
                repeats=5,
            ),
        )
