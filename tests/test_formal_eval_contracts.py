from __future__ import annotations

import pytest
from pydantic import ValidationError

from benchmarks.formal_eval.contracts import (
    AgentResultRow,
    DeterministicEvidenceBundle,
    PreregistrationV2,
)
from benchmarks.formal_eval.fake import (
    fake_agent_rows,
    fake_deterministic_evidence,
    fake_preregistration,
)


def test_preregistration_requires_two_model_tiers() -> None:
    preregistration = fake_preregistration()
    payload = preregistration.model_dump(mode="python")
    payload["models"] = (preregistration.models[0], preregistration.models[0])

    with pytest.raises(ValidationError, match="two distinct model IDs"):
        PreregistrationV2.model_validate(payload)


def test_preregistration_requires_two_model_families() -> None:
    preregistration = fake_preregistration()
    payload = preregistration.model_dump(mode="python")
    second = preregistration.models[1].model_copy(
        update={"family": preregistration.models[0].family}
    )
    payload["models"] = (preregistration.models[0], second)

    with pytest.raises(ValidationError, match="two distinct model families"):
        PreregistrationV2.model_validate(payload)


def test_preregistration_requires_digest_pinned_remote_image() -> None:
    preregistration = fake_preregistration()
    payload = preregistration.model_dump(mode="python")
    payload["image_reference"] = "ghcr.io/example/joinlint-formal:latest"

    with pytest.raises(ValidationError, match="frozen image digest"):
        PreregistrationV2.model_validate(payload)


def test_safe_abstention_is_a_success_only_without_oracle_path() -> None:
    abstention = next(row for row in fake_agent_rows() if row.safe_abstention)
    payload = abstention.model_dump(mode="python")
    payload["oracle_has_safe_path"] = True

    with pytest.raises(ValidationError, match="safe abstention"):
        AgentResultRow.model_validate(payload)


def test_failed_agent_outcome_requires_stable_failure_code() -> None:
    failed = next(
        row
        for row in fake_agent_rows()
        if row.condition == "control" and not row.join_correct_task_completion
    )
    payload = failed.model_dump(mode="python")
    payload["failure_code"] = None

    with pytest.raises(ValidationError, match="successful completion or failure code"):
        AgentResultRow.model_validate(payload)


def test_control_row_rejects_mcp_behavior() -> None:
    control = next(row for row in fake_agent_rows() if row.condition == "control")
    payload = control.model_dump(mode="python")
    payload["plan_called"] = True

    with pytest.raises(ValidationError, match="control rows"):
        AgentResultRow.model_validate(payload)


def test_deterministic_evidence_rejects_duplicate_case_ids() -> None:
    evidence = fake_deterministic_evidence()
    payload = evidence.model_dump(mode="python")
    payload["relationship_rows"] = (
        evidence.relationship_rows[0],
        evidence.relationship_rows[0],
    )

    with pytest.raises(ValidationError, match="duplicate cases"):
        DeterministicEvidenceBundle.model_validate(payload)
