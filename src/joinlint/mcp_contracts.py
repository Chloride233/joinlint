from __future__ import annotations

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


class MCPError(StrictModel):
    code: str
    message: str


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
    schema_version: Literal[2] = 2
    command: Literal["get_join_plan"] = "get_join_plan"
    status: MCPStatus
    data: JoinPlanData | None = None
    findings: tuple[RuntimeFinding, ...] = ()
    error: MCPError | None = None

    @model_validator(mode="after")
    def validate_status(self) -> GetJoinPlanResponse:
        _validate_envelope(self.status, self.data, self.findings, self.error)
        return self


class ValidateSQLResponse(StrictModel):
    schema_version: Literal[2] = 2
    command: Literal["validate_sql"] = "validate_sql"
    status: MCPStatus
    data: SQLValidationData | None = None
    findings: tuple[RuntimeFinding, ...] = ()
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


def error_response(command: Literal["get_join_plan", "validate_sql"], code: str, *, inconclusive: bool) -> GetJoinPlanResponse | ValidateSQLResponse:
    payload = {
        "status": "inconclusive" if inconclusive else "error",
        "error": MCPError(code=code, message=code),
    }
    if command == "get_join_plan":
        return GetJoinPlanResponse(**payload)
    return ValidateSQLResponse(**payload)


def _validate_envelope(
    status: MCPStatus,
    data: object | None,
    findings: tuple[RuntimeFinding, ...],
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
