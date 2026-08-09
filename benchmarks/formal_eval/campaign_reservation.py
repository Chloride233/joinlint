from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from benchmarks.formal_eval.campaign_ledger import (
    LedgerStore,
    ReservationReceipt,
    ReservationRequest,
    reserve_with_store,
)


_REPOSITORY = "Chloride233/joinlint"
_REPOSITORY_ID = 1_311_654_200
_PROTECTED_REF = "refs/heads/main"
_SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_POSITIVE_INTEGER_PATTERN = re.compile(r"[1-9][0-9]*\Z")


class CampaignReservationError(ValueError):
    pass


@dataclass(frozen=True)
class _ReservationPolicy:
    workflow_path: str
    job: str
    upper_micro_cny: int


_POLICIES = {
    "readiness": _ReservationPolicy(
        workflow_path=".github/workflows/formal-pilot-canary.yml",
        job="readiness",
        upper_micro_cny=2_100_000,
    ),
    "calibration": _ReservationPolicy(
        workflow_path=".github/workflows/formal-pilot-canary.yml",
        job="canary",
        upper_micro_cny=4_000_000,
    ),
    "pilot": _ReservationPolicy(
        workflow_path=".github/workflows/formal-pilot.yml",
        job="pilot",
        upper_micro_cny=20_000_000,
    ),
}

_EXPECTED_WORKFLOW_INPUTS = {
    "readiness": {
        "budget_cny": "2.10",
        "calibration": "false",
        "confirm_paid": "true",
        "readiness_only": "true",
    },
    "calibration": {
        "budget_cny": "4",
        "calibration": "true",
        "confirm_paid": "true",
        "readiness_only": "false",
    },
    "pilot": {
        "budget_cny": "20",
        "confirm_paid": "true",
    },
}


def reservation_request_for_current_run(
    environment: Mapping[str, str],
    *,
    mode: str,
    workflow_inputs: Mapping[str, str],
    caller_attested_evaluated_commit: str | None,
) -> ReservationRequest:
    policy = _POLICIES.get(mode)
    if policy is None:
        raise CampaignReservationError("paid mode is not authorized for reservation")
    _require_workflow_inputs(workflow_inputs, mode=mode)

    values = {
        name: _required(environment, name)
        for name in (
            "GITHUB_ACTIONS",
            "GITHUB_EVENT_NAME",
            "GITHUB_JOB",
            "GITHUB_REF",
            "GITHUB_REF_PROTECTED",
            "GITHUB_REPOSITORY",
            "GITHUB_REPOSITORY_ID",
            "GITHUB_RUN_ATTEMPT",
            "GITHUB_RUN_ID",
            "GITHUB_SHA",
            "GITHUB_WORKFLOW_REF",
            "GITHUB_WORKFLOW_SHA",
        )
    }
    expected_workflow_ref = (
        f"{_REPOSITORY}/{policy.workflow_path}@{_PROTECTED_REF}"
    )
    if (
        values["GITHUB_ACTIONS"] != "true"
        or values["GITHUB_EVENT_NAME"] != "workflow_dispatch"
        or values["GITHUB_JOB"] != policy.job
        or values["GITHUB_REF"] != _PROTECTED_REF
        or values["GITHUB_REF_PROTECTED"] != "true"
        or values["GITHUB_REPOSITORY"] != _REPOSITORY
        or values["GITHUB_REPOSITORY_ID"] != str(_REPOSITORY_ID)
        or values["GITHUB_RUN_ATTEMPT"] != "1"
        or values["GITHUB_WORKFLOW_REF"] != expected_workflow_ref
        or values["GITHUB_WORKFLOW_SHA"] != values["GITHUB_SHA"]
        or _SHA_PATTERN.fullmatch(values["GITHUB_SHA"]) is None
    ):
        raise CampaignReservationError("GitHub Actions run identity is not authorized")
    run_id = values["GITHUB_RUN_ID"]
    if _POSITIVE_INTEGER_PATTERN.fullmatch(run_id) is None:
        raise CampaignReservationError("GitHub Actions run ID is invalid")

    if mode == "readiness":
        if caller_attested_evaluated_commit not in (None, values["GITHUB_SHA"]):
            raise CampaignReservationError("readiness commit cannot be overridden")
        evaluated_commit = values["GITHUB_SHA"]
    else:
        if (
            caller_attested_evaluated_commit is None
            or _SHA_PATTERN.fullmatch(caller_attested_evaluated_commit) is None
        ):
            raise CampaignReservationError("caller-attested evaluated commit is invalid")
        if workflow_inputs.get("pilot_commit") != caller_attested_evaluated_commit:
            raise CampaignReservationError(
                "workflow dispatch inputs do not match the evaluated commit"
            )
        evaluated_commit = caller_attested_evaluated_commit

    return ReservationRequest(
        repository_id=_REPOSITORY_ID,
        repository=_REPOSITORY,
        workflow_path=policy.workflow_path,
        job=policy.job,
        mode=mode,
        run_id=int(run_id),
        run_attempt=1,
        workflow_sha=values["GITHUB_WORKFLOW_SHA"],
        evaluated_commit=evaluated_commit,
        upper_micro_cny=policy.upper_micro_cny,
    )


def reserve_current_run(
    store: LedgerStore,
    *,
    expected_campaign_id: str,
    environment: Mapping[str, str],
    mode: str,
    workflow_inputs: Mapping[str, str],
    caller_attested_evaluated_commit: str | None,
) -> ReservationReceipt:
    request = reservation_request_for_current_run(
        environment,
        mode=mode,
        workflow_inputs=workflow_inputs,
        caller_attested_evaluated_commit=caller_attested_evaluated_commit,
    )
    receipt = reserve_with_store(
        store,
        expected_campaign_id=expected_campaign_id,
        request=request,
    )
    if (
        receipt.campaign_id != expected_campaign_id
        or receipt.reservation != request.to_reservation()
    ):
        raise CampaignReservationError("reservation receipt does not match the current run")
    return verify_current_run_receipt(
        store,
        receipt_bytes=receipt.to_bytes(),
        expected_campaign_id=expected_campaign_id,
        environment=environment,
        mode=mode,
        workflow_inputs=workflow_inputs,
        caller_attested_evaluated_commit=caller_attested_evaluated_commit,
    )


def verify_current_run_receipt(
    store: LedgerStore,
    *,
    receipt_bytes: bytes,
    expected_campaign_id: str,
    environment: Mapping[str, str],
    mode: str,
    workflow_inputs: Mapping[str, str],
    caller_attested_evaluated_commit: str | None,
) -> ReservationReceipt:
    expected = reservation_request_for_current_run(
        environment,
        mode=mode,
        workflow_inputs=workflow_inputs,
        caller_attested_evaluated_commit=caller_attested_evaluated_commit,
    ).to_reservation()
    receipt = ReservationReceipt.from_bytes(receipt_bytes)
    if not receipt.authorized:
        raise CampaignReservationError("receipt does not authorize paid execution")
    if receipt.campaign_id != expected_campaign_id or receipt.reservation != expected:
        raise CampaignReservationError("receipt reservation does not match the current run")

    snapshot = store.read()
    if snapshot.commit_sha != receipt.ledger_commit_sha:
        raise CampaignReservationError("receipt does not match the live ledger head")
    if (
        snapshot.ledger.campaign_id != expected_campaign_id
        or snapshot.ledger.budget_micro_cny != receipt.budget_micro_cny
    ):
        raise CampaignReservationError("receipt campaign does not match the live ledger")
    matches = [
        index
        for index, reservation in enumerate(snapshot.ledger.reservations)
        if reservation == receipt.reservation
    ]
    if matches != [len(snapshot.ledger.reservations) - 1]:
        raise CampaignReservationError("receipt reservation is not present at the live head")
    index = matches[0]
    reserved_before = snapshot.ledger.opening_reserved_upper_micro_cny + sum(
        reservation.upper_micro_cny
        for reservation in snapshot.ledger.reservations[:index]
    )
    if (
        receipt.upper_micro_cny != expected.upper_micro_cny
        or receipt.reserved_before_micro_cny != reserved_before
        or receipt.reserved_after_micro_cny
        != reserved_before + expected.upper_micro_cny
        or snapshot.ledger.reserved_upper_micro_cny
        != receipt.reserved_after_micro_cny
    ):
        raise CampaignReservationError("receipt totals do not match the live ledger")
    return receipt


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if type(value) is not str or not value:
        raise CampaignReservationError(f"missing GitHub Actions variable: {name}")
    return value


def _require_workflow_inputs(
    workflow_inputs: Mapping[str, str],
    *,
    mode: str,
) -> None:
    expected = _EXPECTED_WORKFLOW_INPUTS[mode]
    if any(workflow_inputs.get(name) != value for name, value in expected.items()):
        raise CampaignReservationError(
            "workflow dispatch inputs do not match the paid mode"
        )
