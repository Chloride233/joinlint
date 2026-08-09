from __future__ import annotations

import re
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from benchmarks.formal_eval.campaign_ledger import (
    LedgerStore,
    Reservation,
    ReservationReceipt,
    ReservationRequest,
    reserve_with_store,
)
from benchmarks.formal_eval.contracts import InputLockV2
from benchmarks.formal_eval.lineage import digest_value
from benchmarks.formal_eval.manifest import load_document
from benchmarks.formal_eval.pilot import verify_pilot_inputs


_REPOSITORY = "Chloride233/joinlint"
_REPOSITORY_ID = 1_311_654_200
_PROTECTED_REF = "refs/heads/main"
_SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_SHA_BYTES_PATTERN = re.compile(rb"[0-9a-f]{40}\n\Z")
_POSITIVE_INTEGER_PATTERN = re.compile(r"[1-9][0-9]*\Z")
_GIT = "/usr/bin/git"
_CHECKOUT_DIRECTORY = "evaluated-checkout"
_FROZEN_INPUT_DIRECTORY = "frozen-pilot-input"
_GIT_ENVIRONMENT = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_NO_LAZY_FETCH": "1",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
    "LC_ALL": "C",
}


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
            "GITHUB_WORKSPACE",
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

    workspace = _workspace_root(values["GITHUB_WORKSPACE"])
    evaluated_commit = _read_checkout_head(workspace / _CHECKOUT_DIRECTORY)
    if mode == "readiness":
        if evaluated_commit != values["GITHUB_SHA"]:
            raise CampaignReservationError(
                "readiness checkout HEAD does not match the workflow commit"
            )
        input_lock_sha256 = None
    else:
        if workflow_inputs.get("pilot_commit") != evaluated_commit:
            raise CampaignReservationError(
                "workflow dispatch inputs do not match the checkout HEAD"
            )
        input_lock_sha256 = _verify_frozen_pilot_input(
            workspace / _FROZEN_INPUT_DIRECTORY,
            evaluated_commit,
        )

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
        input_lock_sha256=input_lock_sha256,
        upper_micro_cny=policy.upper_micro_cny,
    )


def reserve_current_run(
    store: LedgerStore,
    *,
    expected_campaign_id: str,
    environment: Mapping[str, str],
    mode: str,
    workflow_inputs: Mapping[str, str],
) -> ReservationReceipt:
    request = reservation_request_for_current_run(
        environment,
        mode=mode,
        workflow_inputs=workflow_inputs,
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
    return _verify_current_run_receipt(
        store,
        receipt_bytes=receipt.to_bytes(),
        expected_campaign_id=expected_campaign_id,
        expected=request.to_reservation(),
    )


def verify_current_run_receipt(
    store: LedgerStore,
    *,
    receipt_bytes: bytes,
    expected_campaign_id: str,
    environment: Mapping[str, str],
    mode: str,
    workflow_inputs: Mapping[str, str],
) -> ReservationReceipt:
    expected = reservation_request_for_current_run(
        environment,
        mode=mode,
        workflow_inputs=workflow_inputs,
    ).to_reservation()
    return _verify_current_run_receipt(
        store,
        receipt_bytes=receipt_bytes,
        expected_campaign_id=expected_campaign_id,
        expected=expected,
    )


def _verify_current_run_receipt(
    store: LedgerStore,
    *,
    receipt_bytes: bytes,
    expected_campaign_id: str,
    expected: Reservation,
) -> ReservationReceipt:
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


def _workspace_root(value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        raise CampaignReservationError("GitHub workspace path is not absolute")
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise CampaignReservationError("GitHub workspace is unavailable") from error
    if not stat.S_ISDIR(metadata.st_mode) or candidate.is_symlink() or resolved != candidate:
        raise CampaignReservationError("GitHub workspace is not one real directory")
    return candidate


def _read_checkout_head(checkout_root: Path) -> str:
    _require_real_directory(checkout_root, label="evaluated checkout")
    git_dir = checkout_root / ".git"
    _require_real_directory(git_dir, label="evaluated checkout Git directory")
    _require_regular_file(git_dir / "HEAD", label="evaluated checkout Git HEAD")
    if (git_dir / "commondir").exists() or (git_dir / "commondir").is_symlink():
        raise CampaignReservationError("linked Git checkouts are not accepted")
    try:
        completed = _run_git(
            checkout_root,
            git_dir,
            "rev-parse",
            "--verify",
            "--end-of-options",
            "HEAD^{commit}",
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CampaignReservationError("evaluated checkout HEAD is unavailable") from error
    if (
        completed.returncode != 0
        or _SHA_BYTES_PATTERN.fullmatch(completed.stdout) is None
    ):
        raise CampaignReservationError("evaluated checkout HEAD is invalid")
    try:
        status = _run_git(
            checkout_root,
            git_dir,
            f"--work-tree={checkout_root}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CampaignReservationError("evaluated checkout status is unavailable") from error
    if status.returncode != 0 or status.stdout:
        raise CampaignReservationError("evaluated checkout is not clean")
    return completed.stdout[:-1].decode("ascii")


def _run_git(
    checkout_root: Path,
    git_dir: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        (
            _GIT,
            "--no-pager",
            "--no-replace-objects",
            "--no-lazy-fetch",
            "--no-optional-locks",
            f"--git-dir={git_dir}",
            *arguments,
        ),
        cwd=checkout_root,
        env=_GIT_ENVIRONMENT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=5,
    )


def _verify_frozen_pilot_input(root: Path, evaluated_commit: str) -> str:
    _require_real_directory(root, label="frozen pilot input")
    try:
        registration, _, _ = verify_pilot_inputs(root)
        lock = load_document(root / "input-lock.json", InputLockV2)
    except (OSError, ValueError) as error:
        raise CampaignReservationError("frozen pilot input is invalid") from error
    if registration.joinlint_commit != evaluated_commit:
        raise CampaignReservationError(
            "frozen pilot input does not match the checkout HEAD"
        )
    return digest_value(lock.model_dump(mode="json"))


def _require_real_directory(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise CampaignReservationError(f"{label} is unavailable") from error
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise CampaignReservationError(f"{label} is not one real directory")


def _require_regular_file(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise CampaignReservationError(f"{label} is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise CampaignReservationError(f"{label} is not one regular file")


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
