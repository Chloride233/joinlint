from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from joinlint.model import ModelV1, entity_table_name
from joinlint.runtime.domain import (
    POLICY_VERSION,
    VERIFIER_VERSION,
    AuthorizationProjection,
    AuthorizedRelationship,
    Cardinality,
    Endpoint,
    EntityDefinition,
    EvidenceMeasurements,
    EvidenceRecord,
    RelationshipDefinition,
    RelationshipSeed,
    RuntimeFinding,
    SourceCatalog,
    evidence_id_for,
    relationship_id_for,
)
from joinlint.runtime.sources import SQLiteSnapshot


RowExecutor = Callable[[str], tuple[object, ...]]


def relationship_definitions(
    catalog: SourceCatalog,
    legacy_model: ModelV1 | None = None,
    *,
    curated_source_ids: Iterable[str] = (),
) -> tuple[RelationshipSeed, ...]:
    by_id = {seed.definition.relationship_id: seed for seed in catalog.declared_relationships}
    if legacy_model is None:
        return tuple(by_id[key] for key in sorted(by_id))
    allowed_sources = set(curated_source_ids)
    entities_by_physical = {entity.physical_name: entity for entity in catalog.entities}
    for relationship in legacy_model.relationships:
        child_legacy_id, child_column = relationship.from_.split(".", maxsplit=1)
        parent_legacy_id, parent_column = relationship.to.split(".", maxsplit=1)
        child_legacy = legacy_model.entities.get(child_legacy_id)
        parent_legacy = legacy_model.entities.get(parent_legacy_id)
        if child_legacy is None or parent_legacy is None:
            continue
        if child_legacy.source not in allowed_sources or parent_legacy.source not in allowed_sources:
            continue
        child_physical = entity_table_name(child_legacy, "sqlite")
        parent_physical = entity_table_name(parent_legacy, "sqlite")
        child_entity = entities_by_physical.get(child_physical)
        parent_entity = entities_by_physical.get(parent_physical)
        if child_entity is None or parent_entity is None:
            continue
        if child_column not in _column_names(child_entity) or parent_column not in _column_names(parent_entity):
            continue
        child = Endpoint(entity_id=child_entity.entity_id, columns=(child_column,))
        parent = Endpoint(entity_id=parent_entity.entity_id, columns=(parent_column,))
        relationship_id = relationship_id_for(catalog.source_id, child, parent)
        by_id[relationship_id] = RelationshipSeed(
            definition=RelationshipDefinition(
                relationship_id=relationship_id,
                source_id=catalog.source_id,
                child=child,
                parent=parent,
            ),
            provenance="curated",
            declared_cardinality=relationship.cardinality,
        )
    return tuple(by_id[key] for key in sorted(by_id))


def verify_relationship(
    snapshot: SQLiteSnapshot,
    catalog: SourceCatalog,
    seed: RelationshipSeed,
    *,
    query_observer: Callable[[str], None] | None = None,
) -> EvidenceRecord:
    entities = {entity.entity_id: entity for entity in catalog.entities}
    child_entity = entities[seed.definition.child.entity_id]
    parent_entity = entities[seed.definition.parent.entity_id]
    connection = sqlite3.connect(f"file:{snapshot.path}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")

    def query(statement: str) -> tuple[object, ...]:
        if query_observer is not None:
            query_observer(statement)
        row = connection.execute(statement).fetchone()
        if row is None:
            raise ValueError("evidence query returned no row")
        return row

    try:
        measurements = _measure(query, child_entity, parent_entity, seed)
    finally:
        connection.close()
    findings = _findings(measurements, seed.declared_cardinality)
    evidence_id = evidence_id_for(
        seed.definition.relationship_id,
        snapshot.document.snapshot_id,
        seed.provenance,
        VERIFIER_VERSION,
        measurements,
        findings,
    )
    return EvidenceRecord(
        evidence_id=evidence_id,
        relationship_id=seed.definition.relationship_id,
        snapshot_id=snapshot.document.snapshot_id,
        provenance=seed.provenance,
        verifier_version=VERIFIER_VERSION,
        measurements=measurements,
        findings=findings,
        verified_at=datetime.now(UTC).isoformat(),
    )


def authorize(
    record: EvidenceRecord,
    current_snapshot_id: str,
    policy_version: str = POLICY_VERSION,
) -> AuthorizationProjection:
    blocking = tuple(
        finding.code
        for finding in sorted(record.findings, key=lambda item: item.code.encode("utf-8"))
        if finding.severity == "blocking"
    )
    current = record.snapshot_id == current_snapshot_id
    exact = record.evidence_mode == "exact"
    return AuthorizationProjection(
        policy_version=policy_version,
        current=current,
        exact=exact,
        usable=current and exact and not blocking,
        blocking_reasons=blocking,
    )


def build_authorized_graph(
    seeds: Iterable[RelationshipSeed],
    evidence: Iterable[EvidenceRecord],
    current_snapshot_id: str,
    policy_version: str = POLICY_VERSION,
) -> tuple[AuthorizedRelationship, ...]:
    records = {record.relationship_id: record for record in evidence}
    authorized: list[AuthorizedRelationship] = []
    for seed in seeds:
        record = records.get(seed.definition.relationship_id)
        if record is None:
            continue
        projection = authorize(record, current_snapshot_id, policy_version)
        if projection.usable:
            authorized.append(
                AuthorizedRelationship(
                    definition=seed.definition,
                    evidence=record,
                    authorization=projection,
                    declared_cardinality=seed.declared_cardinality,
                )
            )
    return tuple(
        sorted(authorized, key=lambda item: item.definition.relationship_id.encode("utf-8"))
    )


def _measure(
    query: RowExecutor,
    child_entity: EntityDefinition,
    parent_entity: EntityDefinition,
    seed: RelationshipSeed,
) -> EvidenceMeasurements:
    child_table = _quote(child_entity.physical_name)
    parent_table = _quote(parent_entity.physical_name)
    child_columns = tuple(_quote(column) for column in seed.definition.child.columns)
    parent_columns = tuple(_quote(column) for column in seed.definition.parent.columns)
    child_non_null = " AND ".join(f"c.{column} IS NOT NULL" for column in child_columns)
    parent_non_null = " AND ".join(f"p.{column} IS NOT NULL" for column in parent_columns)
    equality = " AND ".join(
        f"c.{child} = p.{parent}"
        for child, parent in zip(child_columns, parent_columns, strict=True)
    )
    child_group = ", ".join(f"c.{column}" for column in child_columns)
    parent_group = ", ".join(f"p.{column}" for column in parent_columns)

    child_row_count = int(query(f"SELECT COUNT(*) FROM {child_table} AS c")[0])
    parent_row_count = int(query(f"SELECT COUNT(*) FROM {parent_table} AS p")[0])
    child_non_null_count = int(
        query(f"SELECT COUNT(*) FROM {child_table} AS c WHERE {child_non_null}")[0]
    )
    parent_non_null_count = int(
        query(f"SELECT COUNT(*) FROM {parent_table} AS p WHERE {parent_non_null}")[0]
    )
    child_distinct_count = int(
        query(
            "SELECT COUNT(*) FROM (SELECT 1 FROM "
            f"{child_table} AS c WHERE {child_non_null} GROUP BY {child_group})"
        )[0]
    )
    parent_distinct_count = int(
        query(
            "SELECT COUNT(*) FROM (SELECT 1 FROM "
            f"{parent_table} AS p WHERE {parent_non_null} GROUP BY {parent_group})"
        )[0]
    )
    inclusion_numerator = int(
        query(
            f"SELECT COUNT(*) FROM {child_table} AS c WHERE {child_non_null} "
            f"AND EXISTS (SELECT 1 FROM {parent_table} AS p WHERE {equality})"
        )[0]
    )
    max_children = int(
        query(
            "SELECT COALESCE(MAX(n), 0) FROM (SELECT COUNT(*) AS n FROM "
            f"{child_table} AS c WHERE {child_non_null} GROUP BY {child_group})"
        )[0]
    )
    max_parents = int(
        query(
            "SELECT COALESCE(MAX(n), 0) FROM (SELECT COUNT(*) AS n FROM "
            f"{parent_table} AS p WHERE {parent_non_null} GROUP BY {parent_group})"
        )[0]
    )
    observed = _cardinality(max_children, max_parents)
    return EvidenceMeasurements(
        child_row_count=child_row_count,
        parent_row_count=parent_row_count,
        child_non_null_count=child_non_null_count,
        child_null_count=child_row_count - child_non_null_count,
        child_distinct_count=child_distinct_count,
        parent_distinct_count=parent_distinct_count,
        inclusion_numerator=inclusion_numerator,
        inclusion_denominator=child_non_null_count,
        orphan_count=child_non_null_count - inclusion_numerator,
        parent_unique=parent_non_null_count == parent_distinct_count,
        observed_cardinality=observed,
        max_children_per_parent=max_children,
        max_parents_per_child=max_parents,
    )


def _findings(
    measurements: EvidenceMeasurements,
    declared_cardinality: Cardinality,
) -> tuple[RuntimeFinding, ...]:
    findings: list[RuntimeFinding] = []
    if not measurements.parent_unique:
        findings.append(_finding("REFERENCED_KEY_NOT_UNIQUE", "blocking"))
    if measurements.child_null_count:
        findings.append(_finding("CHILD_KEY_NULL", "warning"))
    if measurements.orphan_count:
        findings.append(_finding("ORPHAN_CHILD_ROW", "blocking"))
    if not _cardinality_is_compatible(
        measurements.observed_cardinality,
        declared_cardinality,
    ):
        findings.append(_finding("CARDINALITY_DRIFT", "blocking"))
    if measurements.observed_cardinality == "many_to_many":
        findings.append(_finding("MANY_TO_MANY_FANOUT", "blocking"))
    return tuple(sorted(findings, key=lambda item: item.code.encode("utf-8")))


def _cardinality(max_children: int, max_parents: int) -> Cardinality:
    if max_children <= 1 and max_parents <= 1:
        return "one_to_one"
    if max_children <= 1:
        return "one_to_many"
    if max_parents <= 1:
        return "many_to_one"
    return "many_to_many"


def _cardinality_is_compatible(observed: Cardinality, declared: Cardinality) -> bool:
    allowed_observations: dict[Cardinality, frozenset[Cardinality]] = {
        "one_to_one": frozenset({"one_to_one"}),
        "one_to_many": frozenset({"one_to_one", "one_to_many"}),
        "many_to_one": frozenset({"one_to_one", "many_to_one"}),
        "many_to_many": frozenset(
            {"one_to_one", "one_to_many", "many_to_one", "many_to_many"}
        ),
    }
    return observed in allowed_observations[declared]


def _finding(code: str, severity: str) -> RuntimeFinding:
    return RuntimeFinding(code=code, severity=severity, message=code)  # type: ignore[arg-type]


def _column_names(entity: EntityDefinition) -> set[str]:
    return {column.name for column in entity.columns}


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'
