from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, field_validator, model_validator

from joinlint.runtime.domain import (
    NOT_VALIDATED_SCOPE,
    VALIDATED_SCOPE,
    EntityRef,
    JoinProof,
    NormalizedJoinGraph,
    ProofLifecycleProjection,
    RuntimeFinding,
    StrictModel,
)


MCPStatus = Literal["ok", "findings", "inconclusive", "error"]
NextAction = Literal[
    "fix_request",
    "specify_source",
    "fix_entity_refs",
    "replan",
    "revise_sql",
    "change_expected_grain",
    "reduce_request",
    "stop",
]
FreshnessReason = Literal[
    "SOURCE_CHANGED",
    "SOURCE_SNAPSHOT_CHANGED",
    "POLICY_CHANGED",
    "EVIDENCE_UNAVAILABLE",
]

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")


class RecoveryGuidance(StrictModel):
    retryable: bool
    next_action: NextAction
    affected_refs: tuple[str, ...] = ()
    blocking_relationship_ids: tuple[str, ...] = ()
    freshness_reason: FreshnessReason | None = None

    @model_validator(mode="after")
    def validate_stable_values(self) -> RecoveryGuidance:
        if self.affected_refs != tuple(sorted(set(self.affected_refs), key=str.encode)):
            raise ValueError("affected_refs must be unique and sorted")
        if any(not _IDENTIFIER.fullmatch(value) for value in self.affected_refs):
            raise ValueError("affected_refs contain an invalid identifier")
        expected_relationships = tuple(
            sorted(set(self.blocking_relationship_ids), key=str.encode)
        )
        if self.blocking_relationship_ids != expected_relationships:
            raise ValueError("blocking_relationship_ids must be unique and sorted")
        if any(
            len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in self.blocking_relationship_ids
        ):
            raise ValueError("blocking_relationship_ids must contain SHA-256 digests")
        if (self.next_action == "stop") == self.retryable:
            raise ValueError("stop must not be retryable and corrective actions must be retryable")
        return self


class MCPFinding(RuntimeFinding):
    guidance: RecoveryGuidance


class MCPError(StrictModel):
    code: str
    message: str
    guidance: RecoveryGuidance


class GetJoinPlanRequest(StrictModel):
    entity_refs: tuple[EntityRef, ...]
    start_ref: str
    expected_grain_ref: str | None = None
    max_depth: int = Field(default=4, ge=1, le=4)
    include_alternatives: bool = False

    @model_validator(mode="after")
    def validate_refs(self) -> GetJoinPlanRequest:
        refs = [item.ref for item in self.entity_refs]
        if not 2 <= len(refs) <= 8 or len(refs) != len(set(refs)):
            raise ValueError("entity_refs must contain 2 to 8 unique refs")
        if self.start_ref not in refs:
            raise ValueError("start_ref must identify one request-local ref")
        if self.expected_grain_ref is not None and self.expected_grain_ref not in refs:
            raise ValueError("expected_grain_ref must identify one request-local ref")
        return self


class ValidateSQLRequest(StrictModel):
    sql: str
    dialect: Literal["sqlite"] = "sqlite"
    source_id: str | None = None
    plan_id: str | None = None
    expected_grain_ref: str | None = None

    @field_validator("sql")
    @classmethod
    def require_sql(cls, value: str) -> str:
        if not value:
            raise ValueError("sql is required")
        return value


class JoinPlanData(StrictModel):
    proof: JoinProof
    lifecycle: ProofLifecycleProjection
    validated_scope: tuple[str, ...]
    not_validated_scope: tuple[str, ...]
    execution_count: Literal[0] = 0


class SQLValidationData(StrictModel):
    normalized_join_graph: NormalizedJoinGraph
    matched_relationship_ids: tuple[str, ...]
    matched_evidence_ids: tuple[str, ...]
    proof_matched: bool | None
    repair_proof: JoinProof | None = None
    validated_scope: tuple[str, ...]
    not_validated_scope: tuple[str, ...]
    execution_count: Literal[0]


class GetJoinPlanResponse(StrictModel):
    schema_version: Literal[3] = 3
    command: Literal["get_join_plan"] = "get_join_plan"
    status: MCPStatus
    data: JoinPlanData | None = None
    findings: tuple[MCPFinding, ...] = ()
    error: MCPError | None = None

    @model_validator(mode="after")
    def validate_status(self) -> GetJoinPlanResponse:
        _validate_envelope(self.status, self.data, self.findings, self.error)
        return self


class ValidateSQLResponse(StrictModel):
    schema_version: Literal[3] = 3
    command: Literal["validate_sql"] = "validate_sql"
    status: MCPStatus
    data: SQLValidationData | None = None
    findings: tuple[MCPFinding, ...] = ()
    error: MCPError | None = None

    @model_validator(mode="after")
    def validate_status(self) -> ValidateSQLResponse:
        _validate_envelope(self.status, self.data, self.findings, self.error)
        return self


def validated_scope(*, proof_bound: bool) -> tuple[str, ...]:
    values = (*VALIDATED_SCOPE, *(("proof_binding",) if proof_bound else ()))
    return tuple(values)


def not_validated_scope(*, proof_bound: bool) -> tuple[str, ...]:
    values = (*NOT_VALIDATED_SCOPE, *(("proof_binding",) if not proof_bound else ()))
    return tuple(values)


def error_response(
    command: Literal["get_join_plan", "validate_sql"],
    code: str,
    *,
    inconclusive: bool,
    affected_refs: tuple[str, ...] = (),
    blocking_relationship_ids: tuple[str, ...] = (),
    freshness_reason: FreshnessReason | None = None,
) -> GetJoinPlanResponse | ValidateSQLResponse:
    payload = {
        "status": "inconclusive" if inconclusive else "error",
        "error": MCPError(
            code=code,
            message=code,
            guidance=recovery_guidance(
                code,
                affected_refs=affected_refs,
                blocking_relationship_ids=blocking_relationship_ids,
                freshness_reason=freshness_reason,
            ),
        ),
    }
    if command == "get_join_plan":
        return GetJoinPlanResponse(**payload)
    return ValidateSQLResponse(**payload)


def mcp_finding(
    finding: RuntimeFinding,
    *,
    affected_refs: tuple[str, ...] = (),
    blocking_relationship_ids: tuple[str, ...] = (),
) -> MCPFinding:
    return MCPFinding(
        code=finding.code,
        severity=finding.severity,
        message=finding.code,
        guidance=recovery_guidance(
            finding.code,
            affected_refs=affected_refs,
            blocking_relationship_ids=blocking_relationship_ids,
        ),
    )


def recovery_guidance(
    code: str,
    *,
    affected_refs: tuple[str, ...] = (),
    blocking_relationship_ids: tuple[str, ...] = (),
    freshness_reason: FreshnessReason | None = None,
) -> RecoveryGuidance:
    next_action = _NEXT_ACTION_BY_CODE.get(code, "stop")
    return RecoveryGuidance(
        retryable=next_action != "stop",
        next_action=next_action,
        affected_refs=tuple(sorted(set(affected_refs), key=str.encode)),
        blocking_relationship_ids=tuple(
            sorted(set(blocking_relationship_ids), key=str.encode)
        ),
        freshness_reason=freshness_reason or _FRESHNESS_BY_CODE.get(code),
    )


def _validate_envelope(
    status: MCPStatus,
    data: object | None,
    findings: tuple[MCPFinding, ...],
    error: MCPError | None,
) -> None:
    if status in {"inconclusive", "error"}:
        if data is not None or findings or error is None:
            raise ValueError("inconclusive and error responses require only a stable error")
        return
    if error is not None:
        raise ValueError("successful responses cannot contain an error")
    blocking = any(finding.severity == "blocking" for finding in findings)
    if status == "findings" and not blocking:
        raise ValueError("findings status requires a blocking finding")
    if status == "ok" and (data is None or blocking):
        raise ValueError("ok status requires data and no blocking finding")


_NEXT_ACTION_BY_CODE: dict[str, NextAction] = {
    "INVALID_ARGUMENT": "fix_request",
    "UNSUPPORTED_DIALECT": "fix_request",
    "CROSS_SOURCE_UNSUPPORTED": "fix_request",
    "UNSUPPORTED_SOURCE": "fix_request",
    "SOURCE_DISCOVERY_FAILED": "fix_request",
    "SYMLINK_NOT_ALLOWED": "fix_request",
    "SOURCE_NOT_FOUND": "specify_source",
    "SOURCE_AMBIGUOUS": "specify_source",
    "ENTITY_NOT_FOUND": "fix_entity_refs",
    "UNKNOWN_ENTITY": "revise_sql",
    "PROOF_NOT_AVAILABLE": "replan",
    "PROOF_STALE": "replan",
    "PROOF_UNVERIFIABLE": "replan",
    "SOURCE_CHANGED_DURING_SCAN": "replan",
    "INCONCLUSIVE_SCAN": "replan",
    "EVIDENCE_UNVERIFIABLE": "replan",
    "GRAIN_INCOMPATIBLE": "change_expected_grain",
    "REQUEST_TOO_LARGE": "reduce_request",
    "RESOURCE_LIMIT_EXCEEDED": "reduce_request",
    "OUTPUT_LIMIT_EXCEEDED": "reduce_request",
    "SQL_PARSE_ERROR": "revise_sql",
    "MULTIPLE_STATEMENTS": "revise_sql",
    "UNSUPPORTED_SQL_STATEMENT": "revise_sql",
    "UNSUPPORTED_JOIN_KIND": "revise_sql",
    "UNSUPPORTED_JOIN_SHAPE": "revise_sql",
    "NON_EQUALITY_JOIN": "revise_sql",
    "UNRESOLVED_DERIVED_COLUMN": "revise_sql",
    "MISSING_JOIN_PREDICATE": "revise_sql",
    "UNSUPPORTED_JOIN_EDGE": "revise_sql",
    "PROOF_GRAPH_MISMATCH": "revise_sql",
}

_FRESHNESS_BY_CODE: dict[str, FreshnessReason] = {
    "PROOF_UNVERIFIABLE": "EVIDENCE_UNAVAILABLE",
    "SOURCE_CHANGED_DURING_SCAN": "SOURCE_CHANGED",
}
