from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from benchmarks.formal_eval.lifecycle import (
    LifecycleFailureReason,
    allow_scoring,
    complete_evaluation,
    fail_evaluation,
    infrastructure_prepared,
    record_infrastructure_retry,
    new_lifecycle,
    readiness_failed,
    readiness_passed,
    scoring_eligibility,
    start_evaluation,
)


NOW = datetime(2026, 7, 27, tzinfo=timezone.utc)


def test_evaluation_cannot_start_before_readiness() -> None:
    record = new_lifecycle("codex", "0.144.1", now=NOW)

    with pytest.raises(ValueError, match="illegal lifecycle transition"):
        start_evaluation(record, now=NOW)


def test_infrastructure_preparation_does_not_start_evaluation_or_pass_readiness() -> None:
    prepared_at = NOW + timedelta(seconds=45)
    record = infrastructure_prepared(
        new_lifecycle("codex", "0.144.1", now=NOW),
        duration_seconds=45,
        host_binary_sha256="a" * 64,
        now=prepared_at,
    )

    assert record.phase == "INFRASTRUCTURE_PENDING"
    assert record.infrastructure_status == "pending"
    assert record.evaluation_status == "not_started"
    assert record.infrastructure_prepared_at == prepared_at
    assert record.readiness_started_at == prepared_at
    assert scoring_eligibility(record).eligible is False


def test_infrastructure_retry_evidence_is_preserved() -> None:
    record = infrastructure_prepared(
        new_lifecycle("codex", "0.144.1", now=NOW),
        duration_seconds=61,
        host_binary_sha256="a" * 64,
        infrastructure_attempts=2,
        infrastructure_retry_reason="readiness_timeout",
        now=NOW,
    )

    assert record.infrastructure_attempts == 2
    assert record.infrastructure_retry_reason == "readiness_timeout"


def test_agent_startup_retry_preserves_the_prepared_infrastructure() -> None:
    record = infrastructure_prepared(
        new_lifecycle("codex", "0.144.1", now=NOW),
        duration_seconds=1,
        host_binary_sha256="a" * 64,
        now=NOW,
    )

    retried = record_infrastructure_retry(
        record,
        reason=(
            "host_tool_surface_mismatch:"
            "missing=execute_sql,get_join_plan,submit_sql,validate_sql;unexpected=-"
        ),
    )

    assert retried.infrastructure_attempts == 2
    assert retried.infrastructure_retry_reason is not None
    assert retried.host_binary_sha256 == "a" * 64
    assert retried.phase == "INFRASTRUCTURE_PENDING"


def test_readiness_failure_is_scoring_ineligible() -> None:
    record = readiness_failed(
        new_lifecycle("codex", "0.144.1", now=NOW),
        duration_seconds=1,
        detail="readiness_timeout",
        now=NOW,
    )

    eligibility = scoring_eligibility(record)

    assert eligibility.eligible is False
    assert eligibility.failure_code == "INFRASTRUCTURE_FAILURE"
    assert eligibility.lifecycle_reason == LifecycleFailureReason.READINESS_FAILED


def test_started_evaluation_is_scoring_ineligible() -> None:
    record = readiness_passed(
        new_lifecycle("codex", "0.144.1", now=NOW),
        duration_seconds=1,
        now=NOW,
    )
    record = start_evaluation(record, now=NOW)

    eligibility = scoring_eligibility(record)

    assert eligibility.eligible is False
    assert eligibility.lifecycle_reason == LifecycleFailureReason.EVALUATION_NOT_STARTED


def test_completed_evaluation_is_scoring_eligible() -> None:
    record = readiness_passed(
        new_lifecycle("codex", "0.144.1", now=NOW),
        duration_seconds=1,
        now=NOW,
    )
    record = start_evaluation(record, now=NOW)
    record = complete_evaluation(record, duration_seconds=2, now=NOW)
    record = allow_scoring(record)

    assert scoring_eligibility(record).eligible is True


def test_model_timeout_remains_distinct_from_infrastructure_failure() -> None:
    record = readiness_passed(
        new_lifecycle("codex", "0.144.1", now=NOW),
        duration_seconds=1,
        now=NOW,
    )
    record = start_evaluation(record, now=NOW)
    record = fail_evaluation(
        record,
        reason=LifecycleFailureReason.MODEL_TIMEOUT,
        duration_seconds=90,
        now=NOW,
    )

    eligibility = scoring_eligibility(record)

    assert eligibility.failure_code == "MODEL_TIMEOUT"
    assert eligibility.lifecycle_reason == LifecycleFailureReason.MODEL_TIMEOUT


def test_validation_ledger_failure_marks_evaluation_infrastructure_failed() -> None:
    record = readiness_passed(
        new_lifecycle("codex", "0.144.1", now=NOW),
        duration_seconds=1,
        now=NOW,
    )
    record = start_evaluation(record, now=NOW)

    failed = fail_evaluation(
        record,
        reason=LifecycleFailureReason.VALIDATION_LEDGER_WRITE_FAILED,
        duration_seconds=2,
        now=NOW,
    )

    assert failed.infrastructure_status == "failed"
    assert failed.evaluation_status == "failed"
    assert scoring_eligibility(failed).failure_code == "INFRASTRUCTURE_FAILURE"
