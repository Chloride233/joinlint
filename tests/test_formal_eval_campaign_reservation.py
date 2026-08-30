from __future__ import annotations

import os
import subprocess
import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from benchmarks.formal_eval import campaign_reservation, pilot
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
from benchmarks.formal_eval.lineage import digest_value


CAMPAIGN_ID = "joinlint-formal-v1"
REPOSITORY = "Chloride233/joinlint"
REPOSITORY_ID = 1_311_654_200
WORKFLOW_SHA = "a" * 40
EVALUATED_COMMIT = "b" * 40
INPUT_LOCK_SHA256 = "d" * 64
_READ_CHECKOUT_HEAD = campaign_reservation._read_checkout_head
_VERIFY_FROZEN_PILOT_INPUT = campaign_reservation._verify_frozen_pilot_input

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
    "pilot_stage": (
        ".github/workflows/formal-pilot.yml",
        "pilot",
        7_400_000,
    ),
    "pilot_stage_safety": (
        ".github/workflows/formal-pilot.yml",
        "pilot",
        8_000_000,
    ),
    "pilot_stage_contract_safety": (
        ".github/workflows/formal-pilot.yml",
        "pilot",
        8_000_000,
    ),
    "pilot_stage_contract_ambiguity": (
        ".github/workflows/formal-pilot.yml",
        "pilot",
        8_000_000,
    ),
    "pilot_stage_contract_safety_resume": (
        ".github/workflows/formal-pilot.yml",
        "pilot",
        6_350_000,
    ),
    "pilot_stage_safety_confirmation": (
        ".github/workflows/formal-pilot.yml",
        "pilot",
        5_530_000,
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
        "GITHUB_WORKSPACE": f"/trusted/{mode}",
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
            "stage": "full",
        }
    if mode == "pilot_stage":
        return {
            "budget_cny": "7.4",
            "confirm_paid": "true",
            "pilot_commit": EVALUATED_COMMIT,
            "stage": "flash_full_dataset_v1",
        }
    if mode == "pilot_stage_safety":
        return {
            "budget_cny": "8",
            "confirm_paid": "true",
            "pilot_commit": EVALUATED_COMMIT,
            "stage": "semantic_join_safety_v1",
        }
    if mode == "pilot_stage_contract_safety":
        return {
            "budget_cny": "8",
            "confirm_paid": "true",
            "pilot_commit": EVALUATED_COMMIT,
            "stage": "semantic_join_contract_safety_v1",
        }
    if mode == "pilot_stage_contract_ambiguity":
        return {
            "budget_cny": "8",
            "confirm_paid": "true",
            "pilot_commit": EVALUATED_COMMIT,
            "stage": "semantic_join_contract_ambiguity_v1",
        }
    if mode == "pilot_stage_contract_safety_resume":
        return {
            "budget_cny": "6.35",
            "confirm_paid": "true",
            "pilot_commit": EVALUATED_COMMIT,
            "resume_run_id": "33242737801",
            "stage": "semantic_join_contract_safety_resume_v1",
        }
    if mode == "pilot_stage_safety_confirmation":
        return {
            "budget_cny": "5.53",
            "confirm_paid": "true",
            "pilot_commit": EVALUATED_COMMIT,
            "stage": "semantic_join_safety_confirmation_v1",
        }
    return {"budget_cny": "2.25", "confirm_paid": "true"}


@pytest.mark.parametrize(
    ("mode", "reserved_after_micro_cny"),
    (("calibration", 30_663_153), ("pilot_stage_contract_ambiguity", 34_663_153)),
)
def test_reservation_cli_reads_the_actions_event_and_writes_a_fresh_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    reserved_after_micro_cny: int,
) -> None:
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps({"inputs": _workflow_inputs(mode)}),
        encoding="utf-8",
    )
    receipt_path = tmp_path / "reservation.json"
    github_output = tmp_path / "github-output"
    environment = {
        **_environment(mode),
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_OUTPUT": str(github_output),
    }
    ledger = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="50",
        opening_reserved_upper_cny="26.663153",
    )
    store = _MemoryStore(ledger)
    captured: dict[str, object] = {}

    def fake_store(**kwargs: object) -> _MemoryStore:
        captured.update(kwargs)
        return store

    monkeypatch.setattr(campaign_reservation, "GitHubGitLedgerStore", fake_store)
    monkeypatch.setattr(campaign_reservation, "GhCliApi", lambda: object())

    assert campaign_reservation.main(
        [
            "reserve",
            "--mode",
            mode,
            "--campaign-id",
            CAMPAIGN_ID,
            "--ledger-branch",
            "joinlint-campaign-ledger",
            "--genesis-commit",
            "c" * 40,
            "--receipt",
            str(receipt_path),
        ],
        environment=environment,
    ) == 0

    assert set(captured) == {
        "api",
        "repository",
        "branch",
        "expected_repository_id",
        "expected_genesis_commit",
    }
    assert captured["repository"] == REPOSITORY
    assert captured["branch"] == "joinlint-campaign-ledger"
    assert captured["expected_repository_id"] == REPOSITORY_ID
    assert captured["expected_genesis_commit"] == "c" * 40
    receipt = campaign_reservation.ReservationReceipt.from_bytes(
        receipt_path.read_bytes()
    )
    assert receipt.authorized is True
    assert receipt.reserved_before_micro_cny == 26_663_153
    assert receipt.reserved_after_micro_cny == reserved_after_micro_cny
    assert receipt_path.stat().st_mode & 0o777 == 0o600
    assert github_output.read_text(encoding="utf-8").splitlines() == [
        "campaign_budget_cny=50",
        "campaign_reserved_before_cny=26.663153",
        f"campaign_ledger_commit_sha={receipt.ledger_commit_sha}",
        f"campaign_reservation_id={receipt.reservation_id}",
    ]


def test_reservation_cli_rejects_replay_and_does_not_overwrite_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps({"inputs": _workflow_inputs("calibration")}),
        encoding="utf-8",
    )
    receipt_path = tmp_path / "reservation.json"
    environment = {
        **_environment("calibration"),
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_OUTPUT": str(tmp_path / "github-output"),
    }
    store = _MemoryStore(
        CampaignLedger.create(
            campaign_id=CAMPAIGN_ID,
            budget_cny="50",
            opening_reserved_upper_cny="26.663153",
        )
    )
    monkeypatch.setattr(
        campaign_reservation,
        "GitHubGitLedgerStore",
        lambda **kwargs: store,
    )
    monkeypatch.setattr(campaign_reservation, "GhCliApi", lambda: object())
    arguments = [
        "reserve",
        "--mode",
        "calibration",
        "--campaign-id",
        CAMPAIGN_ID,
        "--ledger-branch",
        "joinlint-campaign-ledger",
        "--genesis-commit",
        "c" * 40,
        "--receipt",
        str(receipt_path),
    ]
    assert campaign_reservation.main(arguments, environment=environment) == 0
    original = receipt_path.read_bytes()

    with pytest.raises(CampaignReservationError, match="does not authorize"):
        campaign_reservation.main(arguments, environment=environment)

    assert receipt_path.read_bytes() == original


@pytest.mark.parametrize(
    "raw",
    (
        b"{}",
        b'{"inputs":[]}',
        b'{"inputs":{"calibration":true,"calibration":false}}',
        b'{"inputs":{"calibration":null}}',
    ),
)
def test_reservation_cli_rejects_invalid_event_before_opening_the_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw: bytes,
) -> None:
    event = tmp_path / "event.json"
    event.write_bytes(raw)
    environment = {
        **_environment("calibration"),
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_OUTPUT": str(tmp_path / "github-output"),
    }
    monkeypatch.setattr(
        campaign_reservation,
        "GitHubGitLedgerStore",
        lambda **kwargs: pytest.fail("store must not be opened"),
    )

    with pytest.raises(CampaignReservationError, match="event"):
        campaign_reservation.main(
            [
                "reserve",
                "--mode",
                "calibration",
                "--campaign-id",
                CAMPAIGN_ID,
                "--ledger-branch",
                "joinlint-campaign-ledger",
                "--genesis-commit",
                "c" * 40,
                "--receipt",
                str(tmp_path / "reservation.json"),
            ],
            environment=environment,
        )


@pytest.fixture(autouse=True)
def _stub_workspace_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        campaign_reservation,
        "_workspace_root",
        lambda value: Path(value),
    )
    monkeypatch.setattr(
        campaign_reservation,
        "_read_checkout_head",
        lambda root: (
            WORKFLOW_SHA if root.parent.name == "readiness" else EVALUATED_COMMIT
        ),
    )
    monkeypatch.setattr(
        campaign_reservation,
        "_verify_frozen_pilot_input",
        lambda root, evaluated_commit: INPUT_LOCK_SHA256,
    )


@pytest.mark.parametrize("mode", tuple(POLICIES))
def test_current_run_request_uses_trusted_identity_and_fixed_upper(mode: str) -> None:
    workflow_path, job, upper = POLICIES[mode]

    request = reservation_request_for_current_run(
        _environment(mode),
        mode=mode,
        workflow_inputs=_workflow_inputs(mode),
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
    assert request.input_lock_sha256 == (
        None if mode == "readiness" else INPUT_LOCK_SHA256
    )
    assert request.upper_micro_cny == upper


@pytest.mark.parametrize("mode", campaign_reservation._RESERVATION_CLI_MODES)
def test_reservation_cli_accepts_every_enabled_policy_mode(mode: str) -> None:
    with pytest.raises(CampaignReservationError, match="GITHUB_EVENT_PATH"):
        campaign_reservation.main(
            [
                "reserve",
                "--mode",
                mode,
                "--campaign-id",
                CAMPAIGN_ID,
                "--ledger-branch",
                "joinlint-campaign-ledger",
                "--genesis-commit",
                "c" * 40,
                "--receipt",
                "/tmp/unused-receipt.json",
            ],
            environment={},
        )


def test_formal_pilot_workflow_uses_only_enabled_reservation_modes() -> None:
    workflow = yaml.load(
        Path(".github/workflows/formal-pilot.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    reservation = next(
        step
        for step in workflow["jobs"]["pilot"]["steps"]
        if step.get("name") == "Reserve Pilot stage budget"
    )
    workflow_modes = set(
        re.findall(r"'(pilot_stage[^']*)'", reservation["env"]["PILOT_RESERVATION_MODE"])
    )

    assert workflow_modes == set(campaign_reservation._RESERVATION_CLI_MODES) - {
        "calibration"
    }


def test_current_run_request_accepts_the_protected_evaluation_branch() -> None:
    environment = _environment("calibration")
    protected_ref = "refs/heads/codex/evaluation-lifecycle-boundaries"
    environment["GITHUB_REF"] = protected_ref
    environment["GITHUB_WORKFLOW_REF"] = (
        f"{REPOSITORY}/.github/workflows/formal-pilot-canary.yml@{protected_ref}"
    )

    request = reservation_request_for_current_run(
        environment,
        mode="calibration",
        workflow_inputs=_workflow_inputs("calibration"),
    )

    assert request.workflow_sha == WORKFLOW_SHA


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
        )


def test_current_run_request_rejects_missing_context_and_checkout_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment("calibration")
    environment.pop("GITHUB_WORKFLOW_SHA")
    with pytest.raises(CampaignReservationError, match="GITHUB_WORKFLOW_SHA"):
        reservation_request_for_current_run(
            environment,
            mode="calibration",
            workflow_inputs=_workflow_inputs("calibration"),
        )

    monkeypatch.setattr(
        campaign_reservation,
        "_read_checkout_head",
        lambda root: "c" * 40,
    )
    with pytest.raises(CampaignReservationError, match="checkout HEAD"):
        reservation_request_for_current_run(
            _environment("calibration"),
            mode="calibration",
            workflow_inputs=_workflow_inputs("calibration"),
        )
    with pytest.raises(CampaignReservationError, match="workflow commit"):
        reservation_request_for_current_run(
            _environment("readiness"),
            mode="readiness",
            workflow_inputs=_workflow_inputs("readiness"),
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
    )
    with pytest.raises(CampaignReservationError, match="not authorize"):
        reserve_current_run(
            store,
            expected_campaign_id=CAMPAIGN_ID,
            environment=environment,
            mode="calibration",
            workflow_inputs=_workflow_inputs("calibration"),
        )

    assert first.authorized is True
    assert first.upper_micro_cny == 4_000_000
    assert store.cas_calls == 1


def test_receipt_verification_requires_live_exact_head_and_current_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    )

    assert verify_current_run_receipt(
        store,
        receipt_bytes=receipt.to_bytes(),
        expected_campaign_id=CAMPAIGN_ID,
        environment=environment,
        mode="calibration",
        workflow_inputs=_workflow_inputs("calibration"),
    ) == receipt

    monkeypatch.setattr(
        campaign_reservation,
        "_verify_frozen_pilot_input",
        lambda root, evaluated_commit: "e" * 64,
    )
    with pytest.raises(CampaignReservationError, match="reservation"):
        verify_current_run_receipt(
            store,
            receipt_bytes=receipt.to_bytes(),
            expected_campaign_id=CAMPAIGN_ID,
            environment=environment,
            mode="calibration",
            workflow_inputs=_workflow_inputs("calibration"),
        )
    monkeypatch.setattr(
        campaign_reservation,
        "_verify_frozen_pilot_input",
        lambda root, evaluated_commit: INPUT_LOCK_SHA256,
    )
    assert verify_current_run_receipt(
        store,
        receipt_bytes=receipt.to_bytes(),
        expected_campaign_id=CAMPAIGN_ID,
        environment=environment,
        mode="calibration",
        workflow_inputs=_workflow_inputs("calibration"),
    ) == receipt

    reserve_current_run(
        store,
        expected_campaign_id=CAMPAIGN_ID,
        environment=_environment("calibration", run_id="124"),
        mode="calibration",
        workflow_inputs=_workflow_inputs("calibration"),
    )
    with pytest.raises(CampaignReservationError, match="ledger head"):
        verify_current_run_receipt(
            store,
            receipt_bytes=receipt.to_bytes(),
            expected_campaign_id=CAMPAIGN_ID,
            environment=environment,
            mode="calibration",
            workflow_inputs=_workflow_inputs("calibration"),
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
        )
    with pytest.raises(CampaignReservationError, match="reservation"):
        verify_current_run_receipt(
            store,
            receipt_bytes=receipt.to_bytes(),
            expected_campaign_id=CAMPAIGN_ID,
            environment=_environment("calibration", run_id="124"),
            mode="calibration",
            workflow_inputs=_workflow_inputs("calibration"),
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
    )

    with pytest.raises(ReservationConflict):
        reserve_current_run(
            store,
            expected_campaign_id=CAMPAIGN_ID,
            environment=_environment("pilot"),
            mode="pilot",
            workflow_inputs=_workflow_inputs("pilot"),
        )


def test_request_derives_paid_commit_and_input_lock_from_fixed_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Path] = {}

    monkeypatch.setattr(
        campaign_reservation,
        "_workspace_root",
        lambda value: Path(value),
        raising=False,
    )

    def read_head(root: Path) -> str:
        observed["checkout"] = root
        return EVALUATED_COMMIT

    def verify_frozen(root: Path, evaluated_commit: str) -> str:
        observed["frozen"] = root
        assert evaluated_commit == EVALUATED_COMMIT
        return INPUT_LOCK_SHA256

    monkeypatch.setattr(
        campaign_reservation,
        "_read_checkout_head",
        read_head,
        raising=False,
    )
    monkeypatch.setattr(
        campaign_reservation,
        "_verify_frozen_pilot_input",
        verify_frozen,
        raising=False,
    )

    request = reservation_request_for_current_run(
        _environment("calibration"),
        mode="calibration",
        workflow_inputs=_workflow_inputs("calibration"),
    )

    assert request.evaluated_commit == EVALUATED_COMMIT
    assert request.input_lock_sha256 == INPUT_LOCK_SHA256
    assert observed == {
        "checkout": Path("/trusted/calibration/evaluated-checkout"),
        "frozen": Path("/trusted/calibration/frozen-pilot-input"),
    }


def test_checkout_head_reader_ignores_ambient_git_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout, expected = _create_git_checkout(tmp_path)
    poison = tmp_path / "attacker.git"
    poison.mkdir()
    for name, value in {
        "GIT_DIR": str(poison),
        "GIT_WORK_TREE": str(tmp_path / "attacker-worktree"),
        "GIT_OBJECT_DIRECTORY": str(poison / "objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(poison / "alternates"),
        "GIT_CONFIG_GLOBAL": str(tmp_path / "attacker-config"),
        "GIT_EXEC_PATH": str(tmp_path / "attacker-bin"),
        "LD_PRELOAD": str(tmp_path / "attacker-library"),
        "PATH": str(tmp_path / "attacker-path"),
    }.items():
        monkeypatch.setenv(name, value)

    assert _READ_CHECKOUT_HEAD(checkout) == expected
    assert os.environ["GIT_DIR"] == str(poison)


@pytest.mark.parametrize(
    "state",
    (
        "tracked",
        "staged",
        "untracked",
        "ignored",
        "assume-unchanged",
        "skip-worktree",
    ),
)
def test_checkout_head_reader_rejects_a_dirty_tree(
    tmp_path: Path,
    state: str,
) -> None:
    checkout, _ = _create_git_checkout(tmp_path)
    if state in {"assume-unchanged", "skip-worktree"}:
        subprocess.run(
            (
                "/usr/bin/git",
                "-C",
                str(checkout),
                "update-index",
                f"--{state}",
                "fixture.txt",
            ),
            check=True,
        )
        (checkout / "fixture.txt").write_text("modified\n", encoding="utf-8")
    elif state in {"tracked", "staged"}:
        (checkout / "fixture.txt").write_text("modified\n", encoding="utf-8")
        if state == "staged":
            subprocess.run(
                ("/usr/bin/git", "-C", str(checkout), "add", "fixture.txt"),
                check=True,
            )
    elif state == "untracked":
        (checkout / "untracked.py").write_text("raise RuntimeError\n", encoding="utf-8")
    else:
        (checkout / "ignored.py").write_text("raise RuntimeError\n", encoding="utf-8")

    with pytest.raises(CampaignReservationError, match="not clean"):
        _READ_CHECKOUT_HEAD(checkout)


def test_checkout_head_reader_rejects_a_redirected_git_directory(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "evaluated-checkout"
    redirected = tmp_path / "redirected.git"
    checkout.mkdir()
    redirected.mkdir()
    (checkout / ".git").symlink_to(redirected, target_is_directory=True)

    with pytest.raises(CampaignReservationError, match="real directory"):
        _READ_CHECKOUT_HEAD(checkout)


def test_frozen_input_must_bind_the_checkout_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "frozen-pilot-input"
    root.mkdir()
    monkeypatch.setattr(
        campaign_reservation,
        "verify_pilot_input_bundle",
        lambda current: (
            SimpleNamespace(joinlint_commit="c" * 40),
            None,
            None,
            pilot.InputLockV2(files={"registration.json": "d" * 64}),
        ),
    )

    with pytest.raises(CampaignReservationError, match="checkout HEAD"):
        _VERIFY_FROZEN_PILOT_INPUT(root, EVALUATED_COMMIT)


def test_frozen_input_digest_comes_from_the_same_verified_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "frozen-pilot-input"
    root.mkdir()
    lock = pilot.InputLockV2(files={"registration.json": "d" * 64})
    monkeypatch.setattr(
        campaign_reservation,
        "verify_pilot_input_bundle",
        lambda current: (
            SimpleNamespace(joinlint_commit=EVALUATED_COMMIT),
            None,
            None,
            lock,
        ),
        raising=False,
    )
    monkeypatch.setattr(
        campaign_reservation,
        "verify_pilot_inputs",
        lambda current: (_ for _ in ()).throw(AssertionError("stale verifier")),
        raising=False,
    )
    monkeypatch.setattr(
        campaign_reservation,
        "load_document",
        lambda path, model: (_ for _ in ()).throw(AssertionError("second lock read")),
        raising=False,
    )

    assert _VERIFY_FROZEN_PILOT_INPUT(root, EVALUATED_COMMIT) == digest_value(
        lock.model_dump(mode="json")
    )


def _create_git_checkout(tmp_path: Path) -> tuple[Path, str]:
    checkout = tmp_path / "evaluated-checkout"
    checkout.mkdir()
    subprocess.run(("/usr/bin/git", "init", "-q", str(checkout)), check=True)
    subprocess.run(
        ("/usr/bin/git", "-C", str(checkout), "config", "user.name", "JoinLint"),
        check=True,
    )
    subprocess.run(
        (
            "/usr/bin/git",
            "-C",
            str(checkout),
            "config",
            "user.email",
            "joinlint@example.invalid",
        ),
        check=True,
    )
    (checkout / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (checkout / "fixture.txt").write_text("fixture\n", encoding="utf-8")
    subprocess.run(("/usr/bin/git", "-C", str(checkout), "add", "."), check=True)
    subprocess.run(
        ("/usr/bin/git", "-C", str(checkout), "commit", "-qm", "fixture"),
        check=True,
    )
    expected = subprocess.run(
        ("/usr/bin/git", "-C", str(checkout), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return checkout, expected
