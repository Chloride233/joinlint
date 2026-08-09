from __future__ import annotations

import base64
import hashlib
import subprocess

import pytest

from benchmarks.formal_eval.campaign_ledger import (
    CampaignBudgetExceeded,
    CampaignLedger,
    CampaignLedgerError,
    CasConflict,
    GitHubApiError,
    GitHubGitLedgerStore,
    GitHubLedgerError,
    GhCliApi,
    LedgerSnapshot,
    ReservationReceipt,
    ReservationConflict,
    ReservationRequest,
    parse_cny_micro,
    main as campaign_ledger_main,
    reserve_with_store,
)


CAMPAIGN_ID = "joinlint-formal-v1"


def _request(
    *,
    run_id: int = 123,
    mode: str = "calibration",
    upper_cny: str = "4",
    evaluated_commit: str = "b" * 40,
) -> ReservationRequest:
    return ReservationRequest.create(
        repository_id=1_311_654_200,
        repository="Chloride233/joinlint",
        workflow_path=".github/workflows/formal-pilot-canary.yml",
        job="canary",
        mode=mode,
        run_id=run_id,
        run_attempt=1,
        workflow_sha="a" * 40,
        evaluated_commit=evaluated_commit,
        upper_cny=upper_cny,
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("0", 0),
        ("0.000001", 1),
        ("2.10", 2_100_000),
        ("19.99584", 19_995_840),
        ("50", 50_000_000),
    ),
)
def test_cny_parser_uses_exact_micro_cny(value: str, expected: int) -> None:
    assert parse_cny_micro(value) == expected


@pytest.mark.parametrize(
    "value",
    (
        "",
        " ",
        "+1",
        "-1",
        "01",
        ".1",
        "1.",
        "1e2",
        "nan",
        "inf",
        "1.0000001",
    ),
)
def test_cny_parser_rejects_ambiguous_or_non_finite_values(value: str) -> None:
    with pytest.raises(CampaignLedgerError, match="CNY"):
        parse_cny_micro(value)


def test_campaign_ledger_is_strict_canonical_and_round_trips() -> None:
    ledger = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="50",
        opening_reserved_upper_cny="10.56",
    )
    raw = ledger.to_bytes()

    assert raw == (
        b'{"budget_micro_cny":50000000,"campaign_id":"joinlint-formal-v1",'
        b'"currency":"CNY","opening_reserved_upper_micro_cny":10560000,'
        b'"reservations":[],"schema_version":1}\n'
    )
    assert CampaignLedger.from_bytes(raw) == ledger


@pytest.mark.parametrize(
    "raw",
    (
        b'{"budget_micro_cny":1,"campaign_id":"c","currency":"CNY",'
        b'"opening_reserved_upper_micro_cny":0,"reservations":[],'
        b'"schema_version":1}',
        b'{"budget_micro_cny":1,"campaign_id":"c","currency":"CNY",'
        b'"opening_reserved_upper_micro_cny":0,"reservations":[],'
        b'"schema_version":1}\n\n',
        b'\xef\xbb\xbf{}\n',
        b'{"schema_version":1, "campaign_id":"c"}\n',
        b'{"budget_micro_cny":1,"budget_micro_cny":1,"campaign_id":"c",'
        b'"currency":"CNY","opening_reserved_upper_micro_cny":0,'
        b'"reservations":[],"schema_version":1}\n',
        b'{}\n',
    ),
)
def test_campaign_ledger_rejects_noncanonical_or_ambiguous_json(raw: bytes) -> None:
    with pytest.raises(CampaignLedgerError):
        CampaignLedger.from_bytes(raw)


def test_new_reservation_uses_a_deterministic_identity_and_exact_budget() -> None:
    ledger = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="4",
        opening_reserved_upper_cny="0",
    )
    result = ledger.reserve(_request())

    assert result.created is True
    assert len(result.reservation.reservation_id) == 64
    assert result.ledger.reserved_upper_micro_cny == 4_000_000
    assert result.ledger.remaining_micro_cny == 0
    assert CampaignLedger.from_bytes(result.ledger.to_bytes()) == result.ledger

    with pytest.raises(CampaignBudgetExceeded):
        result.ledger.reserve(_request(run_id=124, upper_cny="0.000001"))


def test_reservation_replay_is_not_a_second_authorization() -> None:
    ledger = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="10",
        opening_reserved_upper_cny="0",
    )
    first = ledger.reserve(_request())
    replay = first.ledger.reserve(_request())

    assert replay.created is False
    assert replay.ledger is first.ledger
    assert replay.reservation == first.reservation


def test_same_reservation_key_with_different_payload_fails_closed() -> None:
    ledger = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="10",
        opening_reserved_upper_cny="0",
    ).reserve(_request()).ledger

    with pytest.raises(ReservationConflict):
        ledger.reserve(_request(upper_cny="5"))
    with pytest.raises(ReservationConflict):
        ledger.reserve(_request(evaluated_commit="c" * 40))


def test_same_github_run_cannot_change_mode_to_obtain_another_reservation() -> None:
    ledger = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="10",
        opening_reserved_upper_cny="0",
    ).reserve(_request(mode="calibration")).ledger

    with pytest.raises(ReservationConflict):
        ledger.reserve(_request(mode="canary", upper_cny="2.25"))


def test_zero_upper_reservation_is_rejected() -> None:
    with pytest.raises(CampaignLedgerError, match="positive"):
        _request(upper_cny="0")


class _FakeStore:
    def __init__(self, ledger: CampaignLedger) -> None:
        self.snapshot = LedgerSnapshot(commit_sha="1" * 40, ledger=ledger)
        self.cas_calls = 0
        self.conflicting_request: ReservationRequest | None = None
        self.always_conflict = False

    def read(self) -> LedgerSnapshot:
        return self.snapshot

    def compare_and_swap(
        self,
        expected: LedgerSnapshot,
        updated: CampaignLedger,
    ) -> str:
        self.cas_calls += 1
        if expected.commit_sha != self.snapshot.commit_sha:
            raise AssertionError("stale snapshot reached fake CAS")
        if self.conflicting_request is not None:
            competitor = self.snapshot.ledger.reserve(self.conflicting_request).ledger
            self.conflicting_request = None
            self.snapshot = LedgerSnapshot(commit_sha="2" * 40, ledger=competitor)
            raise CasConflict("simulated sibling commit")
        if self.always_conflict:
            raise CasConflict("simulated repeated conflict")
        commit_sha = f"{self.cas_calls + 2:040x}"
        self.snapshot = LedgerSnapshot(commit_sha=commit_sha, ledger=updated)
        return commit_sha


def test_cas_reservation_reloads_after_a_sibling_commit() -> None:
    store = _FakeStore(
        CampaignLedger.create(
            campaign_id=CAMPAIGN_ID,
            budget_cny="10",
            opening_reserved_upper_cny="0",
        )
    )
    store.conflicting_request = _request(run_id=200, mode="readiness", upper_cny="2.10")

    receipt = reserve_with_store(
        store,
        expected_campaign_id=CAMPAIGN_ID,
        request=_request(),
    )

    assert receipt.authorized is True
    assert receipt.reserved_before_micro_cny == 2_100_000
    assert receipt.reserved_after_micro_cny == 6_100_000
    assert store.cas_calls == 2


def test_cas_conflict_rechecks_the_budget_before_retrying() -> None:
    store = _FakeStore(
        CampaignLedger.create(
            campaign_id=CAMPAIGN_ID,
            budget_cny="5",
            opening_reserved_upper_cny="0",
        )
    )
    store.conflicting_request = _request(run_id=200, mode="readiness", upper_cny="2")

    with pytest.raises(CampaignBudgetExceeded):
        reserve_with_store(
            store,
            expected_campaign_id=CAMPAIGN_ID,
            request=_request(),
        )
    assert store.snapshot.ledger.reserved_upper_micro_cny == 2_000_000


def test_existing_reservation_never_reauthorizes_paid_execution() -> None:
    initial = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="10",
        opening_reserved_upper_cny="0",
    ).reserve(_request()).ledger
    store = _FakeStore(initial)

    receipt = reserve_with_store(
        store,
        expected_campaign_id=CAMPAIGN_ID,
        request=_request(),
    )

    assert receipt.authorized is False
    assert store.cas_calls == 0


def test_reservation_receipt_round_trips_with_its_complete_reservation() -> None:
    store = _FakeStore(
        CampaignLedger.create(
            campaign_id=CAMPAIGN_ID,
            budget_cny="10",
            opening_reserved_upper_cny="0",
        )
    )
    receipt = reserve_with_store(
        store,
        expected_campaign_id=CAMPAIGN_ID,
        request=_request(),
    )

    assert ReservationReceipt.from_bytes(receipt.to_bytes()) == receipt
    assert receipt.to_dict()["reservation"] == _request().to_reservation().to_dict()


def test_cas_retry_exhaustion_fails_closed_without_authorization() -> None:
    store = _FakeStore(
        CampaignLedger.create(
            campaign_id=CAMPAIGN_ID,
            budget_cny="10",
            opening_reserved_upper_cny="0",
        )
    )
    store.always_conflict = True

    with pytest.raises(CasConflict, match="retry limit"):
        reserve_with_store(
            store,
            expected_campaign_id=CAMPAIGN_ID,
            request=_request(),
            max_attempts=3,
        )
    assert store.cas_calls == 3


def test_ledger_rejects_tampered_reservation_identity() -> None:
    ledger = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="10",
        opening_reserved_upper_cny="0",
    ).reserve(_request()).ledger
    with pytest.raises(CampaignLedgerError, match="identity"):
        type(ledger.reservations[0])(
            **{
                **ledger.reservations[0].to_dict(),
                "reservation_id": "0" * 64,
            }
        )


class _StubGitHubApi:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []

    def request(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.calls.append((method, endpoint, payload))
        if not self.responses:
            raise AssertionError("unexpected GitHub API call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, dict)
        return response


def _git_blob_sha(raw: bytes) -> str:
    header = f"blob {len(raw)}\0".encode()
    return hashlib.sha1(header + raw).hexdigest()  # noqa: S324 - Git object identity


def _remote_read_responses(
    ledger: CampaignLedger,
    *,
    head_sha: str,
    tree_sha: str,
) -> tuple[dict[str, object], ...]:
    raw = ledger.to_bytes()
    blob_sha = _git_blob_sha(raw)
    return (
        {
            "ref": "refs/heads/joinlint-campaign-ledger",
            "object": {"type": "commit", "sha": head_sha},
        },
        {"sha": head_sha, "tree": {"sha": tree_sha}},
        {
            "sha": tree_sha,
            "truncated": False,
            "tree": [
                {
                    "path": "campaign-ledger.json",
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob_sha,
                }
            ],
        },
        {
            "sha": blob_sha,
            "encoding": "base64",
            "content": base64.b64encode(raw).decode("ascii"),
        },
    )


def test_github_store_reads_and_verifies_the_exact_git_object_chain() -> None:
    ledger = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="50",
        opening_reserved_upper_cny="10.56",
    )
    api = _StubGitHubApi(
        *_remote_read_responses(ledger, head_sha="1" * 40, tree_sha="2" * 40)
    )
    store = GitHubGitLedgerStore(
        api=api,
        repository="Chloride233/joinlint",
        branch="joinlint-campaign-ledger",
    )

    snapshot = store.read()

    assert snapshot == LedgerSnapshot(
        commit_sha="1" * 40,
        tree_sha="2" * 40,
        ledger=ledger,
    )
    assert [call[:2] for call in api.calls] == [
        (
            "GET",
            "repos/Chloride233/joinlint/git/ref/heads/joinlint-campaign-ledger",
        ),
        ("GET", f"repos/Chloride233/joinlint/git/commits/{'1' * 40}"),
        ("GET", f"repos/Chloride233/joinlint/git/trees/{'2' * 40}"),
        ("GET", f"repos/Chloride233/joinlint/git/blobs/{_git_blob_sha(ledger.to_bytes())}"),
    ]


def test_github_store_creates_one_parent_commit_and_non_force_ref_update() -> None:
    initial = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="10",
        opening_reserved_upper_cny="0",
    )
    updated = initial.reserve(_request()).ledger
    new_blob_sha = _git_blob_sha(updated.to_bytes())
    api = _StubGitHubApi(
        {"sha": new_blob_sha},
        {"sha": "3" * 40},
        {"sha": "4" * 40},
        {
            "ref": "refs/heads/joinlint-campaign-ledger",
            "object": {"type": "commit", "sha": "4" * 40},
        },
        *_remote_read_responses(updated, head_sha="4" * 40, tree_sha="3" * 40),
    )
    store = GitHubGitLedgerStore(
        api=api,
        repository="Chloride233/joinlint",
        branch="joinlint-campaign-ledger",
    )
    snapshot = LedgerSnapshot(
        commit_sha="1" * 40,
        tree_sha="2" * 40,
        ledger=initial,
    )

    assert store.compare_and_swap(snapshot, updated) == "4" * 40
    create_tree = api.calls[1]
    assert create_tree[2] == {
        "base_tree": "2" * 40,
        "tree": [
            {
                "mode": "100644",
                "path": "campaign-ledger.json",
                "sha": new_blob_sha,
                "type": "blob",
            }
        ],
    }
    create_commit = api.calls[2]
    assert create_commit[2]["parents"] == ["1" * 40]
    assert create_commit[2]["message"].startswith(
        f"reserve {updated.reservations[-1].reservation_id} "
    )
    assert create_commit[2]["author"] == create_commit[2]["committer"]
    assert api.calls[3][2] == {"force": False, "sha": "4" * 40}


def test_github_store_recovers_only_an_exact_commit_after_unknown_patch_result() -> None:
    initial = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="10",
        opening_reserved_upper_cny="0",
    )
    updated = initial.reserve(_request()).ledger
    new_blob_sha = _git_blob_sha(updated.to_bytes())
    api = _StubGitHubApi(
        {"sha": new_blob_sha},
        {"sha": "3" * 40},
        {"sha": "4" * 40},
        GitHubApiError(status=None),
        *_remote_read_responses(updated, head_sha="4" * 40, tree_sha="3" * 40),
    )
    store = GitHubGitLedgerStore(
        api=api,
        repository="Chloride233/joinlint",
        branch="joinlint-campaign-ledger",
    )

    assert store.compare_and_swap(
        LedgerSnapshot(
            commit_sha="1" * 40,
            tree_sha="2" * 40,
            ledger=initial,
        ),
        updated,
    ) == ("4" * 40)


@pytest.mark.parametrize("loser_status", (422, None))
def test_duplicate_candidates_use_distinct_commits_and_only_winner_can_own_update(
    loser_status: int | None,
) -> None:
    initial = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="10",
        opening_reserved_upper_cny="0",
    )
    updated = initial.reserve(_request()).ledger
    new_blob_sha = _git_blob_sha(updated.to_bytes())
    snapshot = LedgerSnapshot(
        commit_sha="1" * 40,
        tree_sha="2" * 40,
        ledger=initial,
    )

    winner_api = _StubGitHubApi(
        {"sha": new_blob_sha},
        {"sha": "3" * 40},
        {"sha": "4" * 40},
        {
            "ref": "refs/heads/joinlint-campaign-ledger",
            "object": {"type": "commit", "sha": "4" * 40},
        },
        *_remote_read_responses(updated, head_sha="4" * 40, tree_sha="3" * 40),
    )
    winner = GitHubGitLedgerStore(
        api=winner_api,
        repository="Chloride233/joinlint",
        branch="joinlint-campaign-ledger",
        nonce_factory=lambda: "a" * 32,
    )
    assert winner.compare_and_swap(snapshot, updated) == "4" * 40

    loser_api = _StubGitHubApi(
        {"sha": new_blob_sha},
        {"sha": "3" * 40},
        {"sha": "5" * 40},
        GitHubApiError(status=loser_status),
        *_remote_read_responses(updated, head_sha="4" * 40, tree_sha="3" * 40),
    )
    loser = GitHubGitLedgerStore(
        api=loser_api,
        repository="Chloride233/joinlint",
        branch="joinlint-campaign-ledger",
        nonce_factory=lambda: "b" * 32,
    )
    with pytest.raises(CasConflict):
        loser.compare_and_swap(snapshot, updated)

    assert winner_api.calls[2][2]["message"] != loser_api.calls[2][2]["message"]


def test_github_store_distinguishes_ref_rejection_from_a_sibling_commit() -> None:
    initial = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="10",
        opening_reserved_upper_cny="0",
    )
    updated = initial.reserve(_request()).ledger
    new_blob_sha = _git_blob_sha(updated.to_bytes())

    unchanged_api = _StubGitHubApi(
        {"sha": new_blob_sha},
        {"sha": "3" * 40},
        {"sha": "4" * 40},
        GitHubApiError(status=422),
        *_remote_read_responses(initial, head_sha="1" * 40, tree_sha="2" * 40),
    )
    unchanged_store = GitHubGitLedgerStore(
        api=unchanged_api,
        repository="Chloride233/joinlint",
        branch="joinlint-campaign-ledger",
    )
    snapshot = LedgerSnapshot(
        commit_sha="1" * 40,
        tree_sha="2" * 40,
        ledger=initial,
    )
    with pytest.raises(GitHubLedgerError, match="rejected"):
        unchanged_store.compare_and_swap(snapshot, updated)

    competitor = initial.reserve(
        _request(run_id=200, mode="readiness", upper_cny="2.10")
    ).ledger
    changed_api = _StubGitHubApi(
        {"sha": new_blob_sha},
        {"sha": "3" * 40},
        {"sha": "4" * 40},
        GitHubApiError(status=409),
        *_remote_read_responses(competitor, head_sha="5" * 40, tree_sha="6" * 40),
    )
    changed_store = GitHubGitLedgerStore(
        api=changed_api,
        repository="Chloride233/joinlint",
        branch="joinlint-campaign-ledger",
    )
    with pytest.raises(CasConflict, match="advanced"):
        changed_store.compare_and_swap(snapshot, updated)


def test_github_store_rejects_extra_tree_entries_and_wrong_blob_identity() -> None:
    ledger = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="10",
        opening_reserved_upper_cny="0",
    )
    responses = list(
        _remote_read_responses(ledger, head_sha="1" * 40, tree_sha="2" * 40)
    )
    responses[2]["tree"].append(
        {"path": "extra", "mode": "100644", "type": "blob", "sha": "3" * 40}
    )
    store = GitHubGitLedgerStore(
        api=_StubGitHubApi(*responses),
        repository="Chloride233/joinlint",
        branch="joinlint-campaign-ledger",
    )
    with pytest.raises(GitHubLedgerError, match="tree"):
        store.read()

    responses = list(
        _remote_read_responses(ledger, head_sha="1" * 40, tree_sha="2" * 40)
    )
    responses[3]["sha"] = "0" * 40
    store = GitHubGitLedgerStore(
        api=_StubGitHubApi(*responses),
        repository="Chloride233/joinlint",
        branch="joinlint-campaign-ledger",
    )
    with pytest.raises(GitHubLedgerError, match="blob"):
        store.read()


def test_gh_cli_api_sends_canonical_json_without_copying_a_token() -> None:
    calls: list[tuple[list[str], bytes | None]] = []

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, kwargs.get("input")))
        return subprocess.CompletedProcess(command, 0, b'{"sha":"abc"}', b"")

    api = GhCliApi(runner=runner)

    assert api.request("POST", "repos/o/r/git/blobs", {"z": 2, "a": 1}) == {
        "sha": "abc"
    }
    command, payload = calls[0]
    assert command[:4] == ["gh", "api", "--method", "POST"]
    assert command[-3:] == ["repos/o/r/git/blobs", "--input", "-"]
    assert payload == b'{"a":1,"z":2}'
    assert not any("TOKEN" in value or "Bearer" in value for value in command)


def test_gh_cli_api_preserves_http_status_without_leaking_error_content() -> None:
    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            command,
            1,
            b"",
            b"gh: private remote detail (HTTP 409)",
        )

    api = GhCliApi(runner=runner)

    with pytest.raises(GitHubApiError) as captured:
        api.request("PATCH", "repos/o/r/git/refs/heads/ledger", {"force": False})
    assert captured.value.status == 409
    assert "private remote detail" not in str(captured.value)


def test_github_store_initializes_a_single_file_root_commit() -> None:
    ledger = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="0.000001",
        opening_reserved_upper_cny="0",
    )
    blob_sha = _git_blob_sha(ledger.to_bytes())
    api = _StubGitHubApi(
        {"sha": blob_sha},
        {"sha": "2" * 40},
        {"sha": "3" * 40},
        {
            "ref": "refs/heads/joinlint-campaign-ledger",
            "object": {"type": "commit", "sha": "3" * 40},
        },
        *_remote_read_responses(ledger, head_sha="3" * 40, tree_sha="2" * 40),
    )
    store = GitHubGitLedgerStore(
        api=api,
        repository="Chloride233/joinlint",
        branch="joinlint-campaign-ledger",
    )

    assert store.initialize(ledger) == "3" * 40
    assert api.calls[1][2] == {
        "tree": [
            {
                "mode": "100644",
                "path": "campaign-ledger.json",
                "sha": blob_sha,
                "type": "blob",
            }
        ]
    }
    assert api.calls[2][2]["parents"] == []
    assert api.calls[3][2] == {
        "ref": "refs/heads/joinlint-campaign-ledger",
        "sha": "3" * 40,
    }


def test_campaign_ledger_cli_creates_and_verifies_local_genesis(tmp_path) -> None:
    ledger_path = tmp_path / "campaign-ledger.json"

    assert campaign_ledger_main(
        [
            "create-genesis",
            "--campaign-id",
            CAMPAIGN_ID,
            "--budget-cny",
            "50",
            "--opening-reserved-upper-cny",
            "10.56",
            "--output",
            str(ledger_path),
        ]
    ) == 0
    assert campaign_ledger_main(["verify", "--ledger", str(ledger_path)]) == 0
    assert CampaignLedger.from_bytes(ledger_path.read_bytes()).reserved_upper_micro_cny == (
        10_560_000
    )

    with pytest.raises(FileExistsError):
        campaign_ledger_main(
            [
                "create-genesis",
                "--campaign-id",
                CAMPAIGN_ID,
                "--budget-cny",
                "50",
                "--opening-reserved-upper-cny",
                "10.56",
                "--output",
                str(ledger_path),
            ]
        )


def test_campaign_ledger_cli_reserves_once_and_replay_exits_nonzero(tmp_path) -> None:
    initial = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="10",
        opening_reserved_upper_cny="0",
    )
    updated = initial.reserve(_request()).ledger
    new_blob_sha = _git_blob_sha(updated.to_bytes())
    api = _StubGitHubApi(
        *_remote_read_responses(initial, head_sha="1" * 40, tree_sha="2" * 40),
        {"sha": new_blob_sha},
        {"sha": "3" * 40},
        {"sha": "4" * 40},
        {
            "ref": "refs/heads/joinlint-campaign-ledger",
            "object": {"type": "commit", "sha": "4" * 40},
        },
        *_remote_read_responses(updated, head_sha="4" * 40, tree_sha="3" * 40),
    )
    receipt = tmp_path / "receipt.json"
    arguments = [
        "reserve-github",
        "--repository-id",
        "1311654200",
        "--repository",
        "Chloride233/joinlint",
        "--branch",
        "joinlint-campaign-ledger",
        "--campaign-id",
        CAMPAIGN_ID,
        "--workflow-path",
        ".github/workflows/formal-pilot-canary.yml",
        "--job",
        "canary",
        "--mode",
        "calibration",
        "--run-id",
        "123",
        "--run-attempt",
        "1",
        "--workflow-sha",
        "a" * 40,
        "--evaluated-commit",
        "b" * 40,
        "--upper-cny",
        "4",
        "--receipt",
        str(receipt),
    ]

    assert campaign_ledger_main(arguments, api=api) == 0
    assert b'"authorized":true' in receipt.read_bytes()
    assert b'"ledger_commit_sha":"4444444444444444444444444444444444444444"' in (
        receipt.read_bytes()
    )

    replay_api = _StubGitHubApi(
        *_remote_read_responses(updated, head_sha="4" * 40, tree_sha="3" * 40)
    )
    replay_receipt = tmp_path / "replay.json"
    replay_arguments = [
        *arguments[:-1],
        str(replay_receipt),
    ]
    assert campaign_ledger_main(replay_arguments, api=replay_api) == 3
    assert b'"authorized":false' in replay_receipt.read_bytes()
    assert len(replay_api.calls) == 4
