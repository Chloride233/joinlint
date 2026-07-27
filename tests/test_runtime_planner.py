from __future__ import annotations

from datetime import UTC, datetime

import pytest

from joinlint.errors import JoinLintError
from joinlint.runtime.domain import (
    AuthorizationProjection,
    AuthorizedRelationship,
    Endpoint,
    EntityDefinition,
    ColumnDefinition,
    EntityRef,
    EvidenceMeasurements,
    EvidenceRecord,
    RelationshipDefinition,
)
from joinlint.runtime.planner import plan_join, proof_lifecycle


def authorized(
    relationship_id: str,
    child: str,
    parent: str,
    *,
    max_children: int,
    max_parents: int = 1,
    child_columns: tuple[str, ...] = ("parent_id",),
    parent_columns: tuple[str, ...] = ("id",),
) -> AuthorizedRelationship:
    cardinality = (
        "one_to_one"
        if max_children <= 1 and max_parents <= 1
        else "one_to_many"
        if max_children <= 1
        else "many_to_one"
        if max_parents <= 1
        else "many_to_many"
    )
    evidence = EvidenceRecord(
        evidence_id=relationship_id.replace("a", "b"),
        relationship_id=relationship_id,
        snapshot_id="1" * 64,
        provenance="declared",
        verifier_version="sqlite-exact-v1",
        measurements=EvidenceMeasurements(
            child_row_count=10,
            parent_row_count=5,
            child_non_null_count=10,
            child_null_count=0,
            child_distinct_count=5,
            parent_distinct_count=5,
            inclusion_numerator=10,
            inclusion_denominator=10,
            orphan_count=0,
            parent_unique=True,
            observed_cardinality=cardinality,
            max_children_per_parent=max_children,
            max_parents_per_child=max_parents,
        ),
        verified_at=datetime.now(UTC).isoformat(),
    )
    return AuthorizedRelationship(
        definition=RelationshipDefinition(
            relationship_id=relationship_id,
            source_id="sales",
            child=Endpoint(entity_id=child, columns=child_columns),
            parent=Endpoint(entity_id=parent, columns=parent_columns),
        ),
        evidence=evidence,
        authorization=AuthorizationProjection(
            policy_version="declared-curated-v1",
            current=True,
            exact=True,
            usable=True,
        ),
        declared_cardinality=cardinality,
    )


def test_planner_returns_deterministic_many_to_one_proof() -> None:
    graph = (authorized("a" * 64, "sales.orders", "sales.customers", max_children=2),)
    refs = (
        EntityRef(ref="orders", entity="sales.orders"),
        EntityRef(ref="customers", entity="sales.customers"),
    )

    first = plan_join(refs, "orders", "orders", 4, False, graph)
    second = plan_join(tuple(reversed(refs)), "orders", "orders", 4, False, graph)

    assert first.plan_id == second.plan_id
    assert first.edges[0].traversal == "forward"
    assert first.alternatives == ()


def test_planner_supports_request_local_refs_for_self_relationship() -> None:
    graph = (authorized("a" * 64, "hr.employees", "hr.employees", max_children=1),)
    refs = (
        EntityRef(ref="employee", entity="hr.employees"),
        EntityRef(ref="manager", entity="hr.employees"),
    )

    proof = plan_join(refs, "employee", "employee", 4, False, graph)

    assert {(edge.from_ref, edge.to_ref) for edge in proof.edges} == {("employee", "manager")}


def test_parent_grain_is_rejected_when_one_to_many_join_duplicates_it() -> None:
    graph = (authorized("a" * 64, "sales.orders", "sales.customers", max_children=3),)
    refs = (
        EntityRef(ref="customers", entity="sales.customers"),
        EntityRef(ref="orders", entity="sales.orders"),
    )

    with pytest.raises(JoinLintError) as captured:
        plan_join(refs, "customers", "customers", 4, False, graph)

    assert captured.value.code == "NO_VERIFIED_PATH"


def test_compound_fanout_path_is_not_proved() -> None:
    graph = (
        authorized("a" * 64, "sales.orders", "sales.customers", max_children=3),
        authorized("c" * 64, "sales.items", "sales.orders", max_children=4),
    )
    refs = (
        EntityRef(ref="customers", entity="sales.customers"),
        EntityRef(ref="orders", entity="sales.orders"),
        EntityRef(ref="items", entity="sales.items"),
    )

    with pytest.raises(JoinLintError) as captured:
        plan_join(refs, "customers", "items", 4, False, graph)

    assert captured.value.code == "NO_VERIFIED_PATH"


def test_proof_lifecycle_is_current_stale_or_unverifiable() -> None:
    graph = (authorized("a" * 64, "sales.orders", "sales.customers", max_children=2),)
    proof = plan_join(
        (
            EntityRef(ref="orders", entity="sales.orders"),
            EntityRef(ref="customers", entity="sales.customers"),
        ),
        "orders",
        "orders",
        4,
        False,
        graph,
    )
    evidence_ids = {edge.evidence_id for edge in proof.edges}

    assert proof_lifecycle(
        proof,
        current_snapshot_id=proof.snapshot_id,
        current_policy_version=proof.policy_version,
        available_evidence_ids=evidence_ids,
    ).status == "current"
    assert proof_lifecycle(
        proof,
        current_snapshot_id="9" * 64,
        current_policy_version=proof.policy_version,
        available_evidence_ids=evidence_ids,
    ).status == "stale"
    assert proof_lifecycle(
        proof,
        current_snapshot_id=proof.snapshot_id,
        current_policy_version=proof.policy_version,
        available_evidence_ids=(),
    ).status == "unverifiable"


def test_empty_graph_is_no_verified_path_not_cross_source() -> None:
    with pytest.raises(JoinLintError) as captured:
        plan_join(
            (
                EntityRef(ref="orders", entity="sales.orders"),
                EntityRef(ref="customers", entity="sales.customers"),
            ),
            "orders",
            "orders",
            4,
            False,
            (),
        )

    assert captured.value.code == "NO_VERIFIED_PATH"


def test_expected_grain_requires_a_non_null_unique_key() -> None:
    graph = (authorized("a" * 64, "sales.orders", "sales.customers", max_children=2),)
    definitions = (
        EntityDefinition(
            entity_id="sales.orders",
            source_id="sales",
            physical_name="orders",
            columns=(ColumnDefinition(name="customer_id", physical_type="integer", nullable=True),),
        ),
        EntityDefinition(
            entity_id="sales.customers",
            source_id="sales",
            physical_name="customers",
            columns=(ColumnDefinition(name="id", physical_type="integer", nullable=False),),
            primary_key=("id",),
            unique_keys=(("id",),),
        ),
    )

    with pytest.raises(JoinLintError) as captured:
        plan_join(
            (
                EntityRef(ref="orders", entity="sales.orders"),
                EntityRef(ref="customers", entity="sales.customers"),
            ),
            "orders",
            "orders",
            4,
            False,
            graph,
            entity_definitions=definitions,
        )

    assert captured.value.code == "NO_VERIFIED_PATH"


def test_planner_preserves_composite_relationship_grouping() -> None:
    graph = (
        authorized(
            "a" * 64,
            "sales.orders",
            "sales.customers",
            max_children=2,
            child_columns=("tenant_id", "customer_id"),
            parent_columns=("tenant_id", "id"),
        ),
    )

    proof = plan_join(
        (
            EntityRef(ref="orders", entity="sales.orders"),
            EntityRef(ref="customers", entity="sales.customers"),
        ),
        "orders",
        "orders",
        4,
        False,
        graph,
    )

    assert proof.edges[0].child.columns == ("tenant_id", "customer_id")
    assert proof.edges[0].parent.columns == ("tenant_id", "id")
    assert len(proof.edges[0].predicates) == 2


def test_planner_supports_multihop_and_enforces_max_depth() -> None:
    graph = (
        authorized("a" * 64, "sales.orders", "sales.customers", max_children=2),
        authorized("c" * 64, "sales.items", "sales.orders", max_children=3),
    )
    refs = (
        EntityRef(ref="items", entity="sales.items"),
        EntityRef(ref="orders", entity="sales.orders"),
        EntityRef(ref="customers", entity="sales.customers"),
    )

    proof = plan_join(refs, "items", "items", 2, False, graph)

    assert len(proof.edges) == 2
    with pytest.raises(JoinLintError) as captured:
        plan_join(refs, "items", "items", 1, False, graph)
    assert captured.value.code == "NO_VERIFIED_PATH"


def test_planner_returns_at_most_three_explicit_alternatives() -> None:
    graph = tuple(
        authorized(
            character * 64,
            "sales.orders",
            "sales.customers",
            max_children=1,
            child_columns=(f"customer_{character}",),
        )
        for character in ("a", "c", "d", "e", "f")
    )
    refs = (
        EntityRef(ref="orders", entity="sales.orders"),
        EntityRef(ref="customers", entity="sales.customers"),
    )

    proof = plan_join(refs, "orders", "orders", 4, True, graph)

    assert len(proof.alternatives) == 3
