from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Iterable

from joinlint.errors import JoinLintError
from joinlint.runtime.domain import (
    POLICY_VERSION,
    AuthorizedRelationship,
    Cardinality,
    EntityDefinition,
    EntityRef,
    JoinProof,
    ProofEdge,
    ProofLifecycleProjection,
    plan_id_for,
)


@dataclass(frozen=True)
class CandidateEdge:
    from_ref: str
    to_ref: str
    relationship: AuthorizedRelationship
    traversal: str
    cardinality: Cardinality
    max_matches: int


def plan_join(
    entity_refs: tuple[EntityRef, ...],
    start_ref: str,
    expected_grain_ref: str | None,
    max_depth: int,
    include_alternatives: bool,
    graph: tuple[AuthorizedRelationship, ...],
    *,
    policy_version: str = POLICY_VERSION,
    entity_definitions: tuple[EntityDefinition, ...] = (),
) -> JoinProof:
    refs = {item.ref: item for item in entity_refs}
    if not 2 <= len(refs) <= 8 or len(refs) != len(entity_refs):
        raise JoinLintError("INVALID_ARGUMENT", "entity_refs must contain 2 to 8 unique refs", 2)
    if not 1 <= max_depth <= 4 or start_ref not in refs:
        raise JoinLintError("INVALID_ARGUMENT", "start_ref or max_depth is invalid", 2)
    expected = expected_grain_ref or start_ref
    if expected not in refs:
        raise JoinLintError("INVALID_ARGUMENT", "expected_grain_ref is not present", 2)
    if entity_definitions and not _entity_has_stable_grain(
        refs[expected].entity,
        entity_definitions,
    ):
        raise JoinLintError("NO_VERIFIED_PATH", "expected grain has no stable unique key", 3)
    source_ids = {relationship.definition.source_id for relationship in graph}
    if not source_ids:
        raise JoinLintError("NO_VERIFIED_PATH", "no current authorized join proof exists", 3)
    if len(source_ids) != 1:
        raise JoinLintError("CROSS_SOURCE_UNSUPPORTED", "one source is required", 2)
    candidates = _candidate_edges(entity_refs, graph)
    trees = _spanning_trees(refs, start_ref, max_depth, candidates, limit=4)
    safe_trees = [tree for tree in trees if _tree_is_safe(expected, tree)]
    if not safe_trees:
        raise JoinLintError("NO_VERIFIED_PATH", "no current authorized join proof exists", 3)
    now = datetime.now(UTC).isoformat()
    snapshot_ids = {item.relationship.evidence.snapshot_id for item in safe_trees[0]}
    if len(snapshot_ids) != 1:
        raise JoinLintError("EVIDENCE_UNVERIFIABLE", "proof evidence spans snapshots", 3)
    source_id = next(iter(source_ids))
    snapshot_id = next(iter(snapshot_ids))
    proofs = [
        _proof(
            source_id,
            snapshot_id,
            policy_version,
            entity_refs,
            start_ref,
            expected,
            max_depth,
            tree,
            now,
        )
        for tree in safe_trees
    ]
    primary = proofs[0]
    alternatives = tuple(proofs[1:4]) if include_alternatives else ()
    return primary.model_copy(update={"alternatives": alternatives})


def proof_lifecycle(
    proof: JoinProof,
    *,
    current_snapshot_id: str,
    current_policy_version: str,
    available_evidence_ids: Iterable[str],
    freshness_checked_at: str | None = None,
) -> ProofLifecycleProjection:
    checked = freshness_checked_at or datetime.now(UTC).isoformat()
    if proof.snapshot_id != current_snapshot_id:
        return ProofLifecycleProjection(
            status="stale", reason="SOURCE_SNAPSHOT_CHANGED", freshness_checked_at=checked
        )
    if proof.policy_version != current_policy_version:
        return ProofLifecycleProjection(
            status="stale", reason="POLICY_CHANGED", freshness_checked_at=checked
        )
    available = set(available_evidence_ids)
    if any(edge.evidence_id not in available for edge in proof.edges):
        return ProofLifecycleProjection(
            status="unverifiable", reason="EVIDENCE_UNAVAILABLE", freshness_checked_at=checked
        )
    return ProofLifecycleProjection(status="current", reason=None, freshness_checked_at=checked)


def grain_compatible(
    expected_grain_ref: str,
    tree: tuple[CandidateEdge, ...],
) -> bool:
    adjacency: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for edge in tree:
        adjacency[edge.from_ref].append((edge.to_ref, edge.max_matches))
        reverse_matches = _reverse_matches(edge)
        adjacency[edge.to_ref].append((edge.from_ref, reverse_matches))
    queue = deque([(expected_grain_ref, None)])
    while queue:
        ref, parent = queue.popleft()
        for other, matches in adjacency[ref]:
            if other == parent:
                continue
            if matches > 1:
                return False
            queue.append((other, ref))
    return True


def _candidate_edges(
    entity_refs: tuple[EntityRef, ...],
    graph: tuple[AuthorizedRelationship, ...],
) -> tuple[CandidateEdge, ...]:
    candidates: list[CandidateEdge] = []
    for relationship in graph:
        child_entity = relationship.definition.child.entity_id
        parent_entity = relationship.definition.parent.entity_id
        for left in entity_refs:
            for right in entity_refs:
                if left.ref == right.ref:
                    continue
                if left.entity == child_entity and right.entity == parent_entity:
                    candidates.append(
                        CandidateEdge(
                            from_ref=left.ref,
                            to_ref=right.ref,
                            relationship=relationship,
                            traversal="forward",
                            cardinality=relationship.evidence.measurements.observed_cardinality,
                            max_matches=relationship.evidence.measurements.max_parents_per_child,
                        )
                    )
                if left.entity == parent_entity and right.entity == child_entity:
                    candidates.append(
                        CandidateEdge(
                            from_ref=left.ref,
                            to_ref=right.ref,
                            relationship=relationship,
                            traversal="reverse",
                            cardinality=_invert_cardinality(
                                relationship.evidence.measurements.observed_cardinality
                            ),
                            max_matches=relationship.evidence.measurements.max_children_per_parent,
                        )
                    )
    return tuple(sorted(candidates, key=_candidate_key))


def _spanning_trees(
    refs: dict[str, EntityRef],
    start_ref: str,
    max_depth: int,
    candidates: tuple[CandidateEdge, ...],
    *,
    limit: int,
) -> list[tuple[CandidateEdge, ...]]:
    results: list[tuple[CandidateEdge, ...]] = []

    def grow(connected: set[str], depths: dict[str, int], edges: tuple[CandidateEdge, ...]) -> None:
        if len(results) >= limit:
            return
        if len(connected) == len(refs):
            results.append(edges)
            return
        options = [
            edge
            for edge in candidates
            if edge.from_ref in connected
            and edge.to_ref not in connected
            and depths[edge.from_ref] + 1 <= max_depth
        ]
        for edge in options:
            grow(
                connected | {edge.to_ref},
                {**depths, edge.to_ref: depths[edge.from_ref] + 1},
                (*edges, edge),
            )

    grow({start_ref}, {start_ref: 0}, ())
    return results


def _tree_is_safe(expected_grain_ref: str, tree: tuple[CandidateEdge, ...]) -> bool:
    fanout_edges = sum(edge.max_matches > 1 for edge in tree)
    return fanout_edges <= 1 and grain_compatible(expected_grain_ref, tree)


def _proof(
    source_id: str,
    snapshot_id: str,
    policy_version: str,
    entity_refs: tuple[EntityRef, ...],
    start_ref: str,
    expected_grain_ref: str,
    max_depth: int,
    tree: tuple[CandidateEdge, ...],
    timestamp: str,
) -> JoinProof:
    edges = tuple(_proof_edge(edge) for edge in tree)
    plan_id = plan_id_for(
        source_id,
        snapshot_id,
        policy_version,
        entity_refs,
        start_ref,
        expected_grain_ref,
        max_depth,
        edges,
    )
    return JoinProof(
        plan_id=plan_id,
        source_id=source_id,
        snapshot_id=snapshot_id,
        policy_version=policy_version,
        entity_refs=tuple(sorted(entity_refs, key=lambda item: item.ref.encode("utf-8"))),
        start_ref=start_ref,
        expected_grain_ref=expected_grain_ref,
        max_depth=max_depth,
        edges=edges,
        verified_at=timestamp,
        freshness_checked_at=timestamp,
    )


def _proof_edge(edge: CandidateEdge) -> ProofEdge:
    definition = edge.relationship.definition
    predicates = tuple(
        f"{definition.child.entity_id}.{child} = {definition.parent.entity_id}.{parent}"
        for child, parent in zip(
            definition.child.columns,
            definition.parent.columns,
            strict=True,
        )
    )
    return ProofEdge(
        from_ref=edge.from_ref,
        to_ref=edge.to_ref,
        relationship_id=definition.relationship_id,
        evidence_id=edge.relationship.evidence.evidence_id,
        child=definition.child,
        parent=definition.parent,
        traversal=edge.traversal,  # type: ignore[arg-type]
        cardinality=edge.cardinality,
        provenance=edge.relationship.evidence.provenance,
        predicates=predicates,
    )


def _candidate_key(edge: CandidateEdge) -> tuple[object, ...]:
    provenance_rank = 0 if edge.relationship.evidence.provenance == "curated" else 1
    fanout_rank = int(edge.max_matches > 1)
    return (
        provenance_rank,
        fanout_rank,
        edge.relationship.definition.relationship_id.encode("utf-8"),
        edge.from_ref.encode("utf-8"),
        edge.to_ref.encode("utf-8"),
    )


def _reverse_matches(edge: CandidateEdge) -> int:
    measurements = edge.relationship.evidence.measurements
    return (
        measurements.max_children_per_parent
        if edge.traversal == "forward"
        else measurements.max_parents_per_child
    )


def _invert_cardinality(cardinality: Cardinality) -> Cardinality:
    return {
        "one_to_one": "one_to_one",
        "one_to_many": "many_to_one",
        "many_to_one": "one_to_many",
        "many_to_many": "many_to_many",
    }[cardinality]


def _entity_has_stable_grain(
    entity_id: str,
    definitions: tuple[EntityDefinition, ...],
) -> bool:
    entity = next((item for item in definitions if item.entity_id == entity_id), None)
    if entity is None:
        return False
    if entity.primary_key:
        return True
    nullable = {column.name for column in entity.columns if column.nullable}
    return any(not (set(key) & nullable) for key in entity.unique_keys)
