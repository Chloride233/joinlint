from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from joinlint.runtime.domain import (
    AuthorizationProjection,
    Endpoint,
    EntityRef,
    EvidenceMeasurements,
    ProofEdge,
    ProofLifecycleProjection,
    RuntimeFinding,
    evidence_id_for,
    plan_id_for,
    relationship_id_for,
)


def measurements(*, orphans: int = 0) -> EvidenceMeasurements:
    return EvidenceMeasurements(
        child_row_count=10,
        parent_row_count=5,
        child_non_null_count=10,
        child_null_count=0,
        child_distinct_count=5,
        parent_distinct_count=5,
        inclusion_numerator=10 - orphans,
        inclusion_denominator=10,
        orphan_count=orphans,
        parent_unique=True,
        observed_cardinality="many_to_one",
        max_children_per_parent=2,
        max_parents_per_child=1,
    )


def proof_edge(relationship_id: str, evidence_id: str) -> ProofEdge:
    return ProofEdge(
        from_ref="orders",
        to_ref="customers",
        relationship_id=relationship_id,
        evidence_id=evidence_id,
        child=Endpoint(entity_id="sales.orders", columns=("customer_id",)),
        parent=Endpoint(entity_id="sales.customers", columns=("id",)),
        traversal="forward",
        cardinality="many_to_one",
        provenance="declared",
        predicates=("orders.customer_id = customers.id",),
    )


def test_relationship_identity_depends_only_on_source_and_ordered_endpoints() -> None:
    child = Endpoint(entity_id="sales.orders", columns=("tenant_id", "customer_id"))
    parent = Endpoint(entity_id="sales.customers", columns=("tenant_id", "id"))

    original = relationship_id_for("sales", child, parent)

    assert original == relationship_id_for("sales", child, parent)
    assert original != relationship_id_for("other", child, parent)
    assert original != relationship_id_for("sales", parent, child)


def test_evidence_identity_changes_with_snapshot_measurements_and_provenance() -> None:
    relationship_id = relationship_id_for(
        "sales",
        Endpoint(entity_id="sales.orders", columns=("customer_id",)),
        Endpoint(entity_id="sales.customers", columns=("id",)),
    )
    original = evidence_id_for(
        relationship_id,
        "1" * 64,
        "declared",
        "sqlite-exact-v1",
        measurements(),
        (),
    )

    assert original != evidence_id_for(
        relationship_id, "2" * 64, "declared", "sqlite-exact-v1", measurements(), ()
    )
    assert original != evidence_id_for(
        relationship_id, "1" * 64, "curated", "sqlite-exact-v1", measurements(), ()
    )
    assert original != evidence_id_for(
        relationship_id,
        "1" * 64,
        "declared",
        "sqlite-exact-v1",
        measurements(orphans=1),
        (RuntimeFinding(code="ORPHAN_CHILD_ROW", severity="blocking", message="orphan"),),
    )


def test_plan_identity_is_order_independent_but_binds_refs_policy_and_graph() -> None:
    relationship_id = relationship_id_for(
        "sales",
        Endpoint(entity_id="sales.orders", columns=("customer_id",)),
        Endpoint(entity_id="sales.customers", columns=("id",)),
    )
    evidence_id = evidence_id_for(
        relationship_id, "1" * 64, "declared", "sqlite-exact-v1", measurements(), ()
    )
    refs = (
        EntityRef(ref="orders", entity="sales.orders"),
        EntityRef(ref="customers", entity="sales.customers"),
    )
    edge = proof_edge(relationship_id, evidence_id)
    original = plan_id_for(
        "sales", "1" * 64, "policy-v1", refs, "orders", "orders", 4, (edge,)
    )

    assert original == plan_id_for(
        "sales", "1" * 64, "policy-v1", tuple(reversed(refs)), "orders", "orders", 4, (edge,)
    )
    assert original != plan_id_for(
        "sales", "1" * 64, "policy-v2", refs, "orders", "orders", 4, (edge,)
    )
    renamed = (EntityRef(ref="facts", entity="sales.orders"), refs[1])
    assert original != plan_id_for(
        "sales", "1" * 64, "policy-v1", renamed, "facts", "facts", 4, (edge,)
    )


def test_authorization_and_lifecycle_are_derived_and_strict() -> None:
    AuthorizationProjection(
        policy_version="policy-v1",
        current=True,
        exact=True,
        usable=True,
        blocking_reasons=(),
    )
    with pytest.raises(ValidationError, match="evidence-derived"):
        AuthorizationProjection(
            policy_version="policy-v1",
            current=False,
            exact=True,
            usable=True,
            blocking_reasons=(),
        )

    now = datetime.now(UTC).isoformat()
    ProofLifecycleProjection(status="current", reason=None, freshness_checked_at=now)
    ProofLifecycleProjection(status="stale", reason="SOURCE_CHANGED", freshness_checked_at=now)
    with pytest.raises(ValidationError, match="only current"):
        ProofLifecycleProjection(status="stale", reason=None, freshness_checked_at=now)
