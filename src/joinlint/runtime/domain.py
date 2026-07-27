from __future__ import annotations

import hashlib
import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from joinlint.contracts import canonical_json


POLICY_VERSION = "declared-curated-v1"
VERIFIER_VERSION = "sqlite-exact-v1"
CLAIM_SCOPE = "physical_join_only"
VALIDATED_SCOPE = (
    "physical_join_endpoints",
    "relationship_evidence",
    "join_cardinality",
    "join_grain",
    "join_fanout",
)
NOT_VALIDATED_SCOPE = (
    "selected_columns",
    "filters",
    "aggregations",
    "metric_definitions",
    "business_semantics",
    "answer_correctness",
)

Digest = str
Provenance = Literal["declared", "curated"]
Cardinality = Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"]
FindingSeverity = Literal["info", "warning", "blocking"]

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _digest(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("value must be a lowercase SHA-256 digest")
    return value


def _identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError("identifier contains unsupported characters or length")
    return value


class RuntimeFinding(StrictModel):
    code: str
    severity: FindingSeverity
    message: str

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        return _identifier(value)


class SourceIdentity(StrictModel):
    source_id: str
    boundary_digest: Digest
    kind: Literal["sqlite"] = "sqlite"
    relative_locator: str

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        return _identifier(value)

    @field_validator("boundary_digest")
    @classmethod
    def validate_boundary_digest(cls, value: str) -> str:
        return _digest(value)

    @field_validator("relative_locator")
    @classmethod
    def validate_relative_locator(cls, value: str) -> str:
        if (
            not value
            or value.startswith(("/", "\\", "~/"))
            or "\\" in value
            or "\x00" in value
            or ".." in value.split("/")
            or len(value) > 512
        ):
            raise ValueError("source locator must be a bounded project-relative path")
        return value


class SourceSnapshot(StrictModel):
    source_id: str
    snapshot_id: Digest
    schema_digest: Digest
    content_digest: Digest

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        return _identifier(value)

    @field_validator("snapshot_id", "schema_digest", "content_digest")
    @classmethod
    def validate_digests(cls, value: str) -> str:
        return _digest(value)


class ColumnDefinition(StrictModel):
    name: str
    physical_type: str
    nullable: bool
    primary_key_position: int = Field(default=0, ge=0)
    unique: bool = False

    @field_validator("name", "physical_type")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not value or "\x00" in value or len(value) > 256:
            raise ValueError("column metadata must be bounded text")
        return value


class EntityDefinition(StrictModel):
    entity_id: str
    source_id: str
    physical_name: str
    columns: tuple[ColumnDefinition, ...]
    primary_key: tuple[str, ...] = ()
    unique_keys: tuple[tuple[str, ...], ...] = ()

    @field_validator("entity_id", "source_id")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        return _identifier(value)

    @model_validator(mode="after")
    def validate_keys(self) -> EntityDefinition:
        names = [column.name for column in self.columns]
        if not names or len(names) != len(set(names)):
            raise ValueError("entity columns must be non-empty and unique")
        allowed = set(names)
        if not set(self.primary_key) <= allowed or any(not set(key) <= allowed for key in self.unique_keys):
            raise ValueError("entity key references an unknown column")
        return self


class Endpoint(StrictModel):
    entity_id: str
    columns: tuple[str, ...]

    @field_validator("entity_id")
    @classmethod
    def validate_entity_id(cls, value: str) -> str:
        return _identifier(value)

    @model_validator(mode="after")
    def validate_columns(self) -> Endpoint:
        if not self.columns or len(self.columns) != len(set(self.columns)):
            raise ValueError("endpoint columns must be non-empty and unique")
        if any(not column or "\x00" in column or len(column) > 256 for column in self.columns):
            raise ValueError("endpoint columns must be bounded text")
        return self


class RelationshipDefinition(StrictModel):
    relationship_id: Digest
    source_id: str
    child: Endpoint
    parent: Endpoint

    @field_validator("relationship_id")
    @classmethod
    def validate_relationship_id(cls, value: str) -> str:
        return _digest(value)

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        return _identifier(value)

    @model_validator(mode="after")
    def validate_arity(self) -> RelationshipDefinition:
        if len(self.child.columns) != len(self.parent.columns):
            raise ValueError("relationship endpoint arity must match")
        return self


class RelationshipSeed(StrictModel):
    definition: RelationshipDefinition
    provenance: Provenance
    declared_cardinality: Cardinality


class SourceCatalog(StrictModel):
    source_id: str
    snapshot_id: Digest
    entities: tuple[EntityDefinition, ...]
    declared_relationships: tuple[RelationshipSeed, ...]

    @model_validator(mode="after")
    def validate_catalog(self) -> SourceCatalog:
        entity_ids = [entity.entity_id for entity in self.entities]
        if not entity_ids or len(entity_ids) != len(set(entity_ids)):
            raise ValueError("catalog entities must be non-empty and unique")
        relationships = [seed.definition.relationship_id for seed in self.declared_relationships]
        if len(relationships) != len(set(relationships)):
            raise ValueError("catalog relationships must be unique")
        return self


class EvidenceMeasurements(StrictModel):
    child_row_count: int = Field(ge=0)
    parent_row_count: int = Field(ge=0)
    child_non_null_count: int = Field(ge=0)
    child_null_count: int = Field(ge=0)
    child_distinct_count: int = Field(ge=0)
    parent_distinct_count: int = Field(ge=0)
    inclusion_numerator: int = Field(ge=0)
    inclusion_denominator: int = Field(ge=0)
    orphan_count: int = Field(ge=0)
    parent_unique: bool
    observed_cardinality: Cardinality
    max_children_per_parent: int = Field(ge=0)
    max_parents_per_child: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> EvidenceMeasurements:
        values = self.model_dump()
        if any(isinstance(value, float) and not math.isfinite(value) for value in values.values()):
            raise ValueError("evidence measurements must be finite")
        if self.child_non_null_count + self.child_null_count != self.child_row_count:
            raise ValueError("child null and non-null counts must sum to child rows")
        if self.inclusion_denominator != self.child_non_null_count:
            raise ValueError("inclusion denominator must equal child non-null count")
        if self.inclusion_numerator + self.orphan_count != self.inclusion_denominator:
            raise ValueError("inclusion and orphan counts must cover the denominator")
        return self


class EvidenceRecord(StrictModel):
    evidence_id: Digest
    relationship_id: Digest
    snapshot_id: Digest
    provenance: Provenance
    verifier_version: str
    evidence_mode: Literal["exact"] = "exact"
    measurements: EvidenceMeasurements
    findings: tuple[RuntimeFinding, ...] = ()
    verified_at: str

    @field_validator("evidence_id", "relationship_id", "snapshot_id")
    @classmethod
    def validate_digests(cls, value: str) -> str:
        return _digest(value)


class AuthorizationProjection(StrictModel):
    policy_version: str
    current: bool
    exact: bool
    usable: bool
    blocking_reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_usable(self) -> AuthorizationProjection:
        expected = self.current and self.exact and not self.blocking_reasons
        if self.usable != expected:
            raise ValueError("authorization usability must be evidence-derived")
        return self


class AuthorizedRelationship(StrictModel):
    definition: RelationshipDefinition
    evidence: EvidenceRecord
    authorization: AuthorizationProjection
    declared_cardinality: Cardinality

    @model_validator(mode="after")
    def validate_binding(self) -> AuthorizedRelationship:
        if self.definition.relationship_id != self.evidence.relationship_id:
            raise ValueError("authorized relationship must bind matching evidence")
        if not self.authorization.usable:
            raise ValueError("authorized graph cannot contain unusable evidence")
        return self


class EntityRef(StrictModel):
    ref: str
    entity: str

    @field_validator("ref", "entity")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        return _identifier(value)


class ProofEdge(StrictModel):
    from_ref: str
    to_ref: str
    relationship_id: Digest
    evidence_id: Digest
    child: Endpoint
    parent: Endpoint
    traversal: Literal["forward", "reverse"]
    cardinality: Cardinality
    provenance: Provenance
    predicates: tuple[str, ...]

    @field_validator("from_ref", "to_ref")
    @classmethod
    def validate_refs(cls, value: str) -> str:
        return _identifier(value)

    @field_validator("relationship_id", "evidence_id")
    @classmethod
    def validate_digests(cls, value: str) -> str:
        return _digest(value)

    @model_validator(mode="after")
    def validate_predicates(self) -> ProofEdge:
        if len(self.predicates) != len(self.child.columns):
            raise ValueError("proof edge requires one predicate per endpoint pair")
        return self


class JoinProof(StrictModel):
    schema_version: Literal[2] = 2
    plan_id: Digest
    claim_scope: Literal["physical_join_only"] = CLAIM_SCOPE
    source_id: str
    snapshot_id: Digest
    policy_version: str
    entity_refs: tuple[EntityRef, ...]
    start_ref: str
    expected_grain_ref: str
    max_depth: int = Field(ge=1, le=4)
    edges: tuple[ProofEdge, ...]
    alternatives: tuple[JoinProof, ...] = ()
    verified_at: str
    freshness_checked_at: str

    @field_validator("plan_id", "snapshot_id")
    @classmethod
    def validate_digests(cls, value: str) -> str:
        return _digest(value)

    @model_validator(mode="after")
    def validate_refs(self) -> JoinProof:
        refs = [item.ref for item in self.entity_refs]
        if not 2 <= len(refs) <= 8 or len(refs) != len(set(refs)):
            raise ValueError("proof requires 2 to 8 unique request-local refs")
        if self.start_ref not in refs or self.expected_grain_ref not in refs:
            raise ValueError("proof start and expected grain refs must be present")
        if not self.edges:
            raise ValueError("proof requires at least one edge")
        return self


class ProofLifecycleProjection(StrictModel):
    status: Literal["current", "stale", "unverifiable"]
    reason: str | None = None
    freshness_checked_at: str

    @model_validator(mode="after")
    def validate_reason(self) -> ProofLifecycleProjection:
        if (self.status == "current") != (self.reason is None):
            raise ValueError("only current proof lifecycle has no reason")
        return self


class NormalizedJoinEdge(StrictModel):
    left_ref: str
    right_ref: str
    endpoint_pairs: tuple[tuple[str, str], ...]
    join_kind: Literal["inner", "left", "unsupported"]


class NormalizedJoinGraph(StrictModel):
    source_id: str
    entity_refs: tuple[EntityRef, ...]
    edges: tuple[NormalizedJoinEdge, ...]


class SQLValidationOutcome(StrictModel):
    decision: Literal["pass", "block"]
    graph: NormalizedJoinGraph
    matched_relationship_ids: tuple[Digest, ...]
    matched_evidence_ids: tuple[Digest, ...]
    proof_matched: bool | None
    findings: tuple[RuntimeFinding, ...] = ()
    execution_count: Literal[0] = 0

    @model_validator(mode="after")
    def validate_decision(self) -> SQLValidationOutcome:
        blocking = any(finding.severity == "blocking" for finding in self.findings)
        if (self.decision == "block") != blocking:
            raise ValueError("SQL validation decision must match blocking findings")
        return self


def relationship_id_for(source_id: str, child: Endpoint, parent: Endpoint) -> str:
    return _hash(
        "joinlint-relationship-v2",
        {
            "source_id": source_id,
            "child": child.model_dump(mode="json"),
            "parent": parent.model_dump(mode="json"),
        },
    )


def evidence_id_for(
    relationship_id: str,
    snapshot_id: str,
    provenance: Provenance,
    verifier_version: str,
    measurements: EvidenceMeasurements,
    findings: tuple[RuntimeFinding, ...],
) -> str:
    ordered_findings = sorted(
        (finding.model_dump(mode="json") for finding in findings),
        key=lambda finding: (finding["code"].encode("utf-8"), finding["severity"]),
    )
    return _hash(
        "joinlint-evidence-v2",
        {
            "relationship_id": relationship_id,
            "snapshot_id": snapshot_id,
            "provenance": provenance,
            "verifier_version": verifier_version,
            "measurements": measurements.model_dump(mode="json"),
            "findings": ordered_findings,
        },
    )


def plan_id_for(
    source_id: str,
    snapshot_id: str,
    policy_version: str,
    entity_refs: tuple[EntityRef, ...],
    start_ref: str,
    expected_grain_ref: str,
    max_depth: int,
    edges: tuple[ProofEdge, ...],
) -> str:
    ordered_refs = sorted(
        (item.model_dump(mode="json") for item in entity_refs),
        key=lambda item: item["ref"].encode("utf-8"),
    )
    ordered_edges = sorted(
        (
            {
                "from_ref": edge.from_ref,
                "to_ref": edge.to_ref,
                "relationship_id": edge.relationship_id,
                "evidence_id": edge.evidence_id,
                "traversal": edge.traversal,
                "cardinality": edge.cardinality,
                "child": edge.child.model_dump(mode="json"),
                "parent": edge.parent.model_dump(mode="json"),
            }
            for edge in edges
        ),
        key=lambda item: (
            item["from_ref"].encode("utf-8"),
            item["to_ref"].encode("utf-8"),
            item["relationship_id"],
            item["evidence_id"],
        ),
    )
    return _hash(
        "joinlint-plan-v2",
        {
            "schema_version": 2,
            "claim_scope": CLAIM_SCOPE,
            "source_id": source_id,
            "snapshot_id": snapshot_id,
            "policy_version": policy_version,
            "entity_refs": ordered_refs,
            "start_ref": start_ref,
            "expected_grain_ref": expected_grain_ref,
            "max_depth": max_depth,
            "edges": ordered_edges,
        },
    )


def _hash(domain: str, value: object) -> str:
    digest = hashlib.sha256()
    digest.update(domain.encode("ascii"))
    digest.update(b"\0")
    digest.update(canonical_json(value))
    return digest.hexdigest()
