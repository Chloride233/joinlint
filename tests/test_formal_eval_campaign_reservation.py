from __future__ import annotations

from dataclasses import replace

import pytest

from benchmarks.formal_eval.campaign_ledger import (
    CampaignLedger,
    LedgerSnapshot,
    ReservationConflict,
)
from benchmarks.formal_eval.campaign_reservation import (
    CampaignReservationError,
    reservation_request_for_current_run,
    reserve_current_run,
    verify_current_run_receipt,
)


CAMPAIGN_ID = "joinlint-formal-v1"
REPOSITORY = "Chloride233/joinlint"
REPOSITORY_ID = 1_311_654_200
WORKFLOW_SHA = "a" * 40
EVALUATED_COMMIT = "b" * 40

POLICIES = {
    "readiness": (
        ".github/workflows/formal-pilot-canary.yml",
        "readiness",
        2_100_000,
    ),
    "calibration": (
        ".github/workflows/formal-pilot-canary.yml",
        "canary",
        4_000_000,
    ),
    "pilot": (
        ".github/workflows/formal-pilot.yml",
        "pilot",
        20_000_000,
    ),
}


def _environment(mode: str, *, run_id: str = "123") -> dict[str, str]:
    workflow_path, job, _ = POLICIES.get(mode, POLICIES["calibration"])
    return {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_JOB": job,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REF_PROTECTED": "true",
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_REPOSITORY_ID": str(REPOSITORY_ID),
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_RUN_ID": run_id,
        "GITHUB_SHA": WORKFLOW_SHA,
        "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/{workflow_path}@refs/heads/main",
        "GITHUB_WORKFLOW_SHA": WORKFLOW_SHA,
    }


def _workflow_inputs(mode: str) -> dict[str, str]:
    if mode == "readiness":
        return {
            "budget_cny": "2.10",
            "calibration": "false",
            "confirm_paid": "true",
            "readiness_only": "true",
        }
    if mode == "calibration":
        return {
            "budget_cny": "4",
            "calibration": "true",
            "confirm_paid": "true",
            "pilot_commit": EVALUATED_COMMIT,
            "readiness_only": "false",
        }
    if mode == "pilot":
        return {
            "budget_cny": "20",
            "confirm_paid": "true",
            "pilot_commit": EVALUATED_COMMIT,
        }
    return {"budget_cny": "2.25", "confirm_paid": "true"}


@pytest.mark.parametrize("mode", tuple(POLICIES))
def test_current_run_request_uses_trusted_identity_and_fixed_upper(mode: str) -> None:
    workflow_path, job, upper = POLICIES[mode]

    request = reservation_request_for_current_run(
        _environment(mode),
        mode=mode,
        workflow_inputs=_workflow_inputs(mode),
        caller_attested_evaluated_commit=(
            None if mode == "readiness" else EVALUATED_COMMIT
        ),
    )

    assert request.repository_id == REPOSITORY_ID
    assert request.repository == REPOSITORY
    assert request.workflow_path == workflow_path
    assert request.job == job
    assert request.mode == mode
    assert request.run_id == 123
    assert request.run_attempt == 1
    assert request.workflow_sha == WORKFLOW_SHA
    assert request.evaluated_commit == (
        WORKFLOW_SHA if mode == "readiness" else EVALUATED_COMMIT
    )
    assert request.upper_micro_cny == upper


@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("GITHUB_ACTIONS", "false"),
        ("GITHUB_EVENT_NAME", "push"),
        ("GITHUB_JOB", "pilot"),
        ("GITHUB_REF", "refs/heads/feature"),
        ("GITHUB_REF_PROTECTED", "false"),
        ("GITHUB_REPOSITORY", "attacker/fork"),
        ("GITHUB_REPOSITORY_ID", "1"),
        ("GITHUB_RUN_ATTEMPT", "2"),
        ("GITHUB_RUN_ID", "0"),
        ("GITHUB_SHA", "c" * 40),
        ("GITHUB_WORKFLOW_REF", "attacker/workflow@refs/heads/main"),
        ("GITHUB_WORKFLOW_SHA", "c" * 40),
    ),
)
def test_current_run_request_rejects_untrusted_github_context(
    key: str,
    value: str,
) -> None:
    environment = _environment("calibration")
    environment[key] = value

    with pytest.raises(CampaignReservationError):
        reservation_request_for_current_run(
            environment,
            mode="calibration",
            workflow_inputs=_workflow_inputs("calibration"),
            caller_attested_evaluated_commit=EVALUATED_COMMIT,
        )


def test_current_run_request_rejects_missing_context_and_unverified_commit() -> None:
    environment = _environment("calibration")
    environment.pop("GITHUB_WORKFLOW_SHA")
    with pytest.raises(CampaignReservationError, match="GITHUB_WORKFLOW_SHA"):
        reservation_request_for_current_run(
            environment,
            mode="calibration",
            workflow_inputs=_workflow_inputs("calibration"),
            caller_attested_evaluated_commit=EVALUATED_COMMIT,
        )

    with pytest.raises(CampaignReservationError, match="evaluated commit"):
        reservation_request_for_current_run(
            _environment("calibration"),
            mode="calibration",
            workflow_inputs=_workflow_inputs("calibration"),
            caller_attested_evaluated_commit=None,
        )
    with pytest.raises(CampaignReservationError, match="readiness commit"):
        reservation_request_for_current_run(
            _environment("readiness"),
            mode="readiness",
            workflow_inputs=_workflow_inputs("readiness"),
            caller_attested_evaluated_commit=EVALUATED_COMMIT,
        )


@pytest.mark.parametrize(
    ("mode", "key", "value"),
    (
        ("readiness", "confirm_paid", "false"),
        ("readiness", "budget_cny", "2.1"),
        ("readiness", "readiness_only", "false"),
        ("readiness", "calibration", "true"),
        ("calibration", "budget_cny", "2.25"),
        ("calibration", "calibration", "false"),
        ("calibration", "pilot_commit", "c" * 40),
        ("calibration", "readiness_only", "true"),
        ("pilot", "budget_cny", "19.99584"),
        ("pilot", "pilot_commit", "c" * 40),
    ),
)
def test_mode_must_match_exact_dispatch_inputs(
    mode: str,
    key: str,
    value: str,
) -> None:
    inputs = _workflow_inputs(mode)
    inputs[key] = value

    with pytest.raises(CampaignReservationError, match="dispatch inputs"):
        reservation_request_for_current_run(
            _environment(mode),
            mode=mode,
            workflow_inputs=inputs,
            caller_attested_evaluated_commit=(
                None if mode == "readiness" else EVALUATED_COMMIT
            ),
        )


class _MemoryStore:
    def __init__(self, ledger: CampaignLedger) -> None:
        self.snapshot = LedgerSnapshot(commit_sha="1" * 40, ledger=ledger)
        self.read_calls = 0
        self.cas_calls = 0

    def read(self) -> LedgerSnapshot:
        self.read_calls += 1
        return self.snapshot

    def compare_and_swap(
        self,
        expected: LedgerSnapshot,
        updated: CampaignLedger,
    ) -> str:
        assert expected == self.snapshot
        self.cas_calls += 1
        commit_sha = f"{self.cas_calls + 1:040x}"
        self.snapshot = LedgerSnapshot(commit_sha=commit_sha, ledger=updated)
        return commit_sha


@pytest.mark.parametrize("mode", ("canary", "formal", "prepare", "unknown"))
def test_unsupported_paid_modes_fail_before_the_ledger_is_read(mode: str) -> None:
    store = _MemoryStore(
        CampaignLedger.create(
            campaign_id=CAMPAIGN_ID,
            budget_cny="50",
            opening_reserved_upper_cny="0",
        )
    )

    with pytest.raises(CampaignReservationError, match="not authorized"):
        reserve_current_run(
            store,
            expected_campaign_id=CAMPAIGN_ID,
            environment=_environment(mode),
            mode=mode,
            workflow_inputs=_workflow_inputs(mode),
            caller_attested_evaluated_commit=EVALUATED_COMMIT,
        )
    assert store.read_calls == 0
    assert store.cas_calls == 0


def test_current_run_reserves_once_and_replay_never_authorizes() -> None:
    store = _MemoryStore(
        CampaignLedger.create(
            campaign_id=CAMPAIGN_ID,
            budget_cny="50",
            opening_reserved_upper_cny="0",
        )
    )
    environment = _environment("calibration")

    first = reserve_current_run(
        store,
        expected_campaign_id=CAMPAIGN_ID,
        environment=environment,
        mode="calibration",
        workflow_inputs=_workflow_inputs("calibration"),
        caller_attested_evaluated_commit=EVALUATED_COMMIT,
    )
    with pytest.raises(CampaignReservationError, match="not authorize"):
        reserve_current_run(
            store,
            expected_campaign_id=CAMPAIGN_ID,
            environment=environment,
            mode="calibration",
            workflow_inputs=_workflow_inputs("calibration"),
            caller_attested_evaluated_commit=EVALUATED_COMMIT,
        )

    assert first.authorized is True
    assert first.upper_micro_cny == 4_000_000
    assert store.cas_calls == 1


def test_receipt_verification_requires_live_exact_head_and_current_identity() -> None:
    store = _MemoryStore(
        CampaignLedger.create(
            campaign_id=CAMPAIGN_ID,
            budget_cny="50",
            opening_reserved_upper_cny="0",
        )
    )
    environment = _environment("calibration")
    receipt = reserve_current_run(
        store,
        expected_campaign_id=CAMPAIGN_ID,
        environment=environment,
        mode="calibration",
        workflow_inputs=_workflow_inputs("calibration"),
        caller_attested_evaluated_commit=EVALUATED_COMMIT,
    )

    assert verify_current_run_receipt(
        store,
        receipt_bytes=receipt.to_bytes(),
        expected_campaign_id=CAMPAIGN_ID,
        environment=environment,
        mode="calibration",
        workflow_inputs=_workflow_inputs("calibration"),
        caller_attested_evaluated_commit=EVALUATED_COMMIT,
    ) == receipt
    assert verify_current_run_receipt(
        store,
        receipt_bytes=receipt.to_bytes(),
        expected_campaign_id=CAMPAIGN_ID,
        environment=environment,
        mode="calibration",
        workflow_inputs=_workflow_inputs("calibration"),
        caller_attested_evaluated_commit=EVALUATED_COMMIT,
    ) == receipt

    reserve_current_run(
        store,
        expected_campaign_id=CAMPAIGN_ID,
        environment=_environment("calibration", run_id="124"),
        mode="calibration",
        workflow_inputs=_workflow_inputs("calibration"),
        caller_attested_evaluated_commit=EVALUATED_COMMIT,
    )
    with pytest.raises(CampaignReservationError, match="ledger head"):
        verify_current_run_receipt(
            store,
            receipt_bytes=receipt.to_bytes(),
            expected_campaign_id=CAMPAIGN_ID,
            environment=environment,
            mode="calibration",
            workflow_inputs=_workflow_inputs("calibration"),
            caller_attested_evaluated_commit=EVALUATED_COMMIT,
        )


def test_receipt_verification_rejects_replay_wrong_run_and_missing_inclusion() -> None:
    initial = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="50",
        opening_reserved_upper_cny="0",
    )
    store = _MemoryStore(initial)
    environment = _environment("calibration")
    receipt = reserve_current_run(
        store,
        expected_campaign_id=CAMPAIGN_ID,
        environment=environment,
        mode="calibration",
        workflow_inputs=_workflow_inputs("calibration"),
        caller_attested_evaluated_commit=EVALUATED_COMMIT,
    )
    replay = replace(receipt, authorized=False)

    with pytest.raises(CampaignReservationError, match="not authorize"):
        verify_current_run_receipt(
            store,
            receipt_bytes=replay.to_bytes(),
            expected_campaign_id=CAMPAIGN_ID,
            environment=environment,
            mode="calibration",
            workflow_inputs=_workflow_inputs("calibration"),
            caller_attested_evaluated_commit=EVALUATED_COMMIT,
        )
    with pytest.raises(CampaignReservationError, match="reservation"):
        verify_current_run_receipt(
            store,
            receipt_bytes=receipt.to_bytes(),
            expected_campaign_id=CAMPAIGN_ID,
            environment=_environment("calibration", run_id="124"),
            mode="calibration",
            workflow_inputs=_workflow_inputs("calibration"),
            caller_attested_evaluated_commit=EVALUATED_COMMIT,
        )

    missing = _MemoryStore(initial)
    missing.snapshot = replace(missing.snapshot, commit_sha=receipt.ledger_commit_sha)
    with pytest.raises(CampaignReservationError, match="not present"):
        verify_current_run_receipt(
            missing,
            receipt_bytes=receipt.to_bytes(),
            expected_campaign_id=CAMPAIGN_ID,
            environment=environment,
            mode="calibration",
            workflow_inputs=_workflow_inputs("calibration"),
            caller_attested_evaluated_commit=EVALUATED_COMMIT,
        )


def test_same_run_cannot_switch_from_calibration_to_pilot() -> None:
    store = _MemoryStore(
        CampaignLedger.create(
            campaign_id=CAMPAIGN_ID,
            budget_cny="50",
            opening_reserved_upper_cny="0",
        )
    )
    reserve_current_run(
        store,
        expected_campaign_id=CAMPAIGN_ID,
        environment=_environment("calibration"),
        mode="calibration",
        workflow_inputs=_workflow_inputs("calibration"),
        caller_attested_evaluated_commit=EVALUATED_COMMIT,
    )

    with pytest.raises(ReservationConflict):
        reserve_current_run(
            store,
            expected_campaign_id=CAMPAIGN_ID,
            environment=_environment("pilot"),
            mode="pilot",
            workflow_inputs=_workflow_inputs("pilot"),
            caller_attested_evaluated_commit=EVALUATED_COMMIT,
        )
