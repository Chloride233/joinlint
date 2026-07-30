from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field

from benchmarks.formal_eval.contracts import FailureCode, Host, StrictModel


LIFECYCLE_STORE_KEY = "joinlint.formal_eval.lifecycle.v1"


class LifecyclePhase(StrEnum):
    INFRASTRUCTURE_PENDING = "INFRASTRUCTURE_PENDING"
    READINESS_PASSED = "READINESS_PASSED"
    EVALUATION_STARTED = "EVALUATION_STARTED"
    EVALUATION_COMPLETED = "EVALUATION_COMPLETED"
    SCORING_ELIGIBLE = "SCORING_ELIGIBLE"
    FAILED = "FAILED"


class InfrastructureStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


class EvaluationStatus(StrEnum):
    NOT_STARTED = "not_started"
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class LifecycleFailureReason(StrEnum):
    IMAGE_PREPARATION_FAILED = "IMAGE_PREPARATION_FAILED"
    SANDBOX_PROVISION_FAILED = "SANDBOX_PROVISION_FAILED"
    READINESS_FAILED = "READINESS_FAILED"
    HOST_CONTEXT_DRIFT = "HOST_CONTEXT_DRIFT"
    EVALUATION_NOT_STARTED = "EVALUATION_NOT_STARTED"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    MODEL_LIMIT = "MODEL_LIMIT"
    EVALUATION_FAILED = "EVALUATION_FAILED"
    SCORING_FAILURE = "SCORING_FAILURE"


class LifecycleRecord(StrictModel):
    schema_version: Literal[1] = 1
    phase: LifecyclePhase = LifecyclePhase.INFRASTRUCTURE_PENDING
    infrastructure_status: InfrastructureStatus = InfrastructureStatus.PENDING
    evaluation_status: EvaluationStatus = EvaluationStatus.NOT_STARTED
    host: Host
    agent_version: str
    readiness_started_at: datetime | None = None
    infrastructure_prepared_at: datetime | None = None
    infrastructure_preparation_duration_seconds: float | None = Field(default=None, ge=0)
    host_binary_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    readiness_completed_at: datetime | None = None
    readiness_duration_seconds: float | None = Field(default=None, ge=0)
    evaluation_started_at: datetime | None = None
    evaluation_completed_at: datetime | None = None
    evaluation_duration_seconds: float | None = Field(default=None, ge=0)
    failure_reason: LifecycleFailureReason | None = None
    failure_detail: str | None = Field(default=None, max_length=512)

    @property
    def scoring_eligible(self) -> bool:
        return (
            self.phase == LifecyclePhase.SCORING_ELIGIBLE
            and self.infrastructure_status == InfrastructureStatus.READY
            and self.evaluation_status == EvaluationStatus.COMPLETED
            and self.failure_reason is None
        )


class ScoringEligibility(StrictModel):
    eligible: bool
    failure_code: FailureCode | None = None
    lifecycle_reason: LifecycleFailureReason | None = None


def new_lifecycle(host: Host, agent_version: str, *, now: datetime | None = None) -> LifecycleRecord:
    return LifecycleRecord(
        host=host,
        agent_version=agent_version,
        readiness_started_at=now or _utc_now(),
    )


def readiness_passed(
    record: LifecycleRecord,
    *,
    duration_seconds: float,
    now: datetime | None = None,
) -> LifecycleRecord:
    _require_phase(record, LifecyclePhase.INFRASTRUCTURE_PENDING)
    return record.model_copy(
        update={
            "phase": LifecyclePhase.READINESS_PASSED,
            "infrastructure_status": InfrastructureStatus.READY,
            "readiness_completed_at": now or _utc_now(),
            "readiness_duration_seconds": duration_seconds,
        }
    )


def infrastructure_prepared(
    record: LifecycleRecord,
    *,
    duration_seconds: float,
    host_binary_sha256: str,
    now: datetime | None = None,
) -> LifecycleRecord:
    _require_phase(record, LifecyclePhase.INFRASTRUCTURE_PENDING)
    prepared_at = now or _utc_now()
    return record.model_copy(
        update={
            "infrastructure_prepared_at": prepared_at,
            "infrastructure_preparation_duration_seconds": duration_seconds,
            "host_binary_sha256": host_binary_sha256,
            "readiness_started_at": prepared_at,
        }
    )


def readiness_failed(
    record: LifecycleRecord,
    *,
    reason: LifecycleFailureReason = LifecycleFailureReason.READINESS_FAILED,
    detail: str | None = None,
    duration_seconds: float,
    now: datetime | None = None,
) -> LifecycleRecord:
    _require_phase(record, LifecyclePhase.INFRASTRUCTURE_PENDING)
    if reason not in {
        LifecycleFailureReason.IMAGE_PREPARATION_FAILED,
        LifecycleFailureReason.SANDBOX_PROVISION_FAILED,
        LifecycleFailureReason.READINESS_FAILED,
        LifecycleFailureReason.HOST_CONTEXT_DRIFT,
        LifecycleFailureReason.EVALUATION_NOT_STARTED,
    }:
        raise ValueError("readiness failure requires an infrastructure reason")
    return record.model_copy(
        update={
            "phase": LifecyclePhase.FAILED,
            "infrastructure_status": InfrastructureStatus.FAILED,
            "readiness_completed_at": now or _utc_now(),
            "readiness_duration_seconds": duration_seconds,
            "failure_reason": reason,
            "failure_detail": _bounded_detail(detail),
        }
    )


def start_evaluation(record: LifecycleRecord, *, now: datetime | None = None) -> LifecycleRecord:
    _require_phase(record, LifecyclePhase.READINESS_PASSED)
    if record.infrastructure_status != InfrastructureStatus.READY:
        raise ValueError("evaluation cannot start before infrastructure is ready")
    return record.model_copy(
        update={
            "phase": LifecyclePhase.EVALUATION_STARTED,
            "evaluation_status": EvaluationStatus.STARTED,
            "evaluation_started_at": now or _utc_now(),
        }
    )


def complete_evaluation(
    record: LifecycleRecord,
    *,
    duration_seconds: float,
    now: datetime | None = None,
) -> LifecycleRecord:
    _require_phase(record, LifecyclePhase.EVALUATION_STARTED)
    return record.model_copy(
        update={
            "phase": LifecyclePhase.EVALUATION_COMPLETED,
            "evaluation_status": EvaluationStatus.COMPLETED,
            "evaluation_completed_at": now or _utc_now(),
            "evaluation_duration_seconds": duration_seconds,
        }
    )


def fail_evaluation(
    record: LifecycleRecord,
    *,
    reason: LifecycleFailureReason,
    detail: str | None = None,
    duration_seconds: float,
    now: datetime | None = None,
) -> LifecycleRecord:
    _require_phase(record, LifecyclePhase.EVALUATION_STARTED)
    if reason not in {
        LifecycleFailureReason.MODEL_TIMEOUT,
        LifecycleFailureReason.MODEL_LIMIT,
        LifecycleFailureReason.EVALUATION_FAILED,
    }:
        raise ValueError("evaluation failure requires an evaluation reason")
    status = (
        EvaluationStatus.TIMED_OUT
        if reason == LifecycleFailureReason.MODEL_TIMEOUT
        else EvaluationStatus.FAILED
    )
    return record.model_copy(
        update={
            "phase": LifecyclePhase.FAILED,
            "evaluation_status": status,
            "evaluation_completed_at": now or _utc_now(),
            "evaluation_duration_seconds": duration_seconds,
            "failure_reason": reason,
            "failure_detail": _bounded_detail(detail),
        }
    )


def allow_scoring(record: LifecycleRecord) -> LifecycleRecord:
    _require_phase(record, LifecyclePhase.EVALUATION_COMPLETED)
    if record.evaluation_status != EvaluationStatus.COMPLETED:
        raise ValueError("only a completed evaluation can become scoring eligible")
    return record.model_copy(update={"phase": LifecyclePhase.SCORING_ELIGIBLE})


def scoring_eligibility(value: object) -> ScoringEligibility:
    try:
        record = parse_lifecycle(value)
    except (TypeError, ValueError):
        return ScoringEligibility(
            eligible=False,
            failure_code="INFRASTRUCTURE_FAILURE",
            lifecycle_reason=LifecycleFailureReason.EVALUATION_NOT_STARTED,
        )
    if record.scoring_eligible:
        return ScoringEligibility(eligible=True)
    reason = record.failure_reason or LifecycleFailureReason.EVALUATION_NOT_STARTED
    failure_code: FailureCode = (
        "MODEL_TIMEOUT"
        if reason == LifecycleFailureReason.MODEL_TIMEOUT
        else "MODEL_LIMIT"
        if reason == LifecycleFailureReason.MODEL_LIMIT
        else "INFRASTRUCTURE_FAILURE"
    )
    return ScoringEligibility(
        eligible=False,
        failure_code=failure_code,
        lifecycle_reason=reason,
    )


def lifecycle_from_store(store: Any) -> LifecycleRecord | None:
    value = store.get(LIFECYCLE_STORE_KEY)
    if value is None:
        return None
    try:
        return parse_lifecycle(value)
    except ValueError:
        return None


def write_lifecycle(store: Any, record: LifecycleRecord) -> None:
    payload = record.model_dump(mode="json")
    setter = getattr(store, "set", None)
    if callable(setter):
        setter(LIFECYCLE_STORE_KEY, payload)
    else:
        store[LIFECYCLE_STORE_KEY] = payload


def parse_lifecycle(value: object) -> LifecycleRecord:
    if isinstance(value, LifecycleRecord):
        return value
    return LifecycleRecord.model_validate(value, strict=False)


def elapsed_seconds_since(started_at: datetime | None, *, now: datetime | None = None) -> float:
    if started_at is None:
        return 0.0
    return max(0.0, ((now or _utc_now()) - started_at).total_seconds())


def _require_phase(record: LifecycleRecord, expected: LifecyclePhase) -> None:
    if record.phase != expected:
        raise ValueError(f"illegal lifecycle transition from {record.phase}; expected {expected}")


def _bounded_detail(detail: str | None) -> str | None:
    if detail is None:
        return None
    normalized = " ".join(detail.split())
    return normalized[:512]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
