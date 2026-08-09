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
INPUT_LOCK_SHA256 = "d" * 64
REPOSITORY = "Chloride233/joinlint"
REPOSITORY_ID = 1_311_654_200
LEDGER_BRANCH = "joinlint-campaign-ledger"


def _request(
    *,
    run_id: int = 123,
    mode: str = "calibration",
    upper_cny: str = "4",
    evaluated_commit: str = "b" * 40,
    input_lock_sha256: str | None = INPUT_LOCK_SHA256,
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
        input_lock_sha256=input_lock_sha256,
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
        b'"reservations":[],"schema_version":2}\n'
    )
    assert CampaignLedger.from_bytes(raw) == ledger


@pytest.mark.parametrize(
    "raw",
    (
        b'{"budget_micro_cny":1,"campaign_id":"c","currency":"CNY",'
        b'"opening_reserved_upper_micro_cny":0,"reservations":[],'
        b'"schema_version":2}',
        b'{"budget_micro_cny":1,"campaign_id":"c","currency":"CNY",'
        b'"opening_reserved_upper_micro_cny":0,"reservations":[],'
        b'"schema_version":2}\n\n',
        b'\xef\xbb\xbf{}\n',
        b'{"schema_version":2, "campaign_id":"c"}\n',
        b'{"budget_micro_cny":1,"budget_micro_cny":1,"campaign_id":"c",'
        b'"currency":"CNY","opening_reserved_upper_micro_cny":0,'
        b'"reservations":[],"schema_version":2}\n',
        b'{"budget_micro_cny":1,"campaign_id":"c","currency":"CNY",'
        b'"opening_reserved_upper_micro_cny":0,"reservations":[],'
        b'"schema_version":1}\n',
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
    with pytest.raises(ReservationConflict):
        ledger.reserve(_request(input_lock_sha256="e" * 64))


def test_reservation_identity_round_trips_without_a_frozen_input_for_readiness() -> None:
    request = _request(
        mode="readiness",
        upper_cny="2.10",
        input_lock_sha256=None,
    )

    reservation = request.to_reservation()

    assert reservation.input_lock_sha256 is None
    assert reservation.request == request


@pytest.mark.parametrize("digest", ("", "d" * 63, "D" * 64))
def test_reservation_rejects_invalid_input_lock_digest(digest: str) -> None:
    with pytest.raises(CampaignLedgerError, match="input_lock_sha256"):
        _request(input_lock_sha256=digest)


@pytest.mark.parametrize(
    ("mode", "digest"),
    (
        ("readiness", INPUT_LOCK_SHA256),
        ("calibration", None),
        ("pilot", None),
    ),
)
def test_reservation_mode_requires_the_matching_input_lock_contract(
    mode: str,
    digest: str | None,
) -> None:
    with pytest.raises(CampaignLedgerError, match="input lock"):
        _request(mode=mode, input_lock_sha256=digest)


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
    store.conflicting_request = _request(
        run_id=200,
        mode="readiness",
        upper_cny="2.10",
        input_lock_sha256=None,
    )

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
    store.conflicting_request = _request(
        run_id=200,
        mode="readiness",
        upper_cny="2",
        input_lock_sha256=None,
    )

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

    legacy = receipt.to_bytes().replace(b'"schema_version":2', b'"schema_version":1')
    with pytest.raises(CampaignLedgerError, match="contract"):
        ReservationReceipt.from_bytes(legacy)

    tampered_lock = receipt.to_bytes().replace(
        INPUT_LOCK_SHA256.encode("ascii"),
        ("e" * 64).encode("ascii"),
    )
    with pytest.raises(CampaignLedgerError, match="identity"):
        ReservationReceipt.from_bytes(tampered_lock)


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
    parents: tuple[str, ...] = (),
) -> tuple[dict[str, object], ...]:
    return (
        {"id": REPOSITORY_ID},
        {
            "ref": "refs/heads/joinlint-campaign-ledger",
            "object": {"type": "commit", "sha": head_sha},
        },
        *_remote_commit_responses(
            ledger,
            commit_sha=head_sha,
            tree_sha=tree_sha,
            parents=parents,
        ),
    )


def _remote_commit_responses(
    ledger: CampaignLedger,
    *,
    commit_sha: str,
    tree_sha: str,
    parents: tuple[str, ...],
) -> tuple[dict[str, object], ...]:
    raw = ledger.to_bytes()
    blob_sha = _git_blob_sha(raw)
    return (
        {
            "sha": commit_sha,
            "tree": {"sha": tree_sha},
            "parents": [{"sha": parent} for parent in parents],
        },
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


def _lineage_read_responses(
    *history: tuple[CampaignLedger, str, str, tuple[str, ...]],
    repository_id: int = REPOSITORY_ID,
) -> tuple[dict[str, object], ...]:
    head_sha = history[0][1]
    responses: list[dict[str, object]] = [
        {"id": repository_id},
        {
            "ref": f"refs/heads/{LEDGER_BRANCH}",
            "object": {"type": "commit", "sha": head_sha},
        },
    ]
    for ledger, commit_sha, tree_sha, parents in history:
        responses.extend(
            _remote_commit_responses(
                ledger,
                commit_sha=commit_sha,
                tree_sha=tree_sha,
                parents=parents,
            )
        )
    return tuple(responses)


def _lineage_store(
    api: _StubGitHubApi,
    *,
    genesis_sha: str | None,
) -> GitHubGitLedgerStore:
    return GitHubGitLedgerStore(
        api=api,
        repository=REPOSITORY,
        branch=LEDGER_BRANCH,
        expected_repository_id=REPOSITORY_ID,
        expected_genesis_commit=genesis_sha,
    )


def _initialize_candidate_responses(
    ledger: CampaignLedger,
    *,
    tree_sha: str,
    commit_sha: str,
) -> tuple[dict[str, object], ...]:
    return (
        {"id": REPOSITORY_ID},
        {"sha": _git_blob_sha(ledger.to_bytes())},
        {"sha": tree_sha},
        {"sha": commit_sha},
    )


def test_github_store_verifies_two_appends_from_its_pinned_empty_genesis() -> None:
    root = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="50",
        opening_reserved_upper_cny="10.56",
    )
    first = root.reserve(_request()).ledger
    second = first.reserve(
        _request(
            run_id=124,
            mode="readiness",
            upper_cny="2.10",
            input_lock_sha256=None,
        )
    ).ledger
    root_sha, first_sha, second_sha = "1" * 40, "2" * 40, "3" * 40
    api = _StubGitHubApi(
        *_lineage_read_responses(
            (second, second_sha, "6" * 40, (first_sha,)),
            (first, first_sha, "5" * 40, (root_sha,)),
            (root, root_sha, "4" * 40, ()),
        )
    )

    snapshot = _lineage_store(api, genesis_sha=root_sha).read()

    assert snapshot.ledger == second
    commit_reads = [call for call in api.calls if "/git/commits/" in call[1]]
    assert len(commit_reads) == len(second.reservations) + 1


def test_github_store_accepts_a_valid_ancestor_prefix_so_rollback_needs_an_external_checkpoint() -> None:
    root = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="50",
        opening_reserved_upper_cny="0",
    )
    first = root.reserve(_request()).ledger
    second = first.reserve(
        _request(
            run_id=124,
            mode="readiness",
            upper_cny="2.10",
            input_lock_sha256=None,
        )
    ).ledger
    root_sha, first_sha, second_sha = "1" * 40, "2" * 40, "3" * 40
    api = _StubGitHubApi(
        *_lineage_read_responses(
            (second, second_sha, "6" * 40, (first_sha,)),
            (first, first_sha, "5" * 40, (root_sha,)),
            (root, root_sha, "4" * 40, ()),
        ),
        *_lineage_read_responses(
            (first, first_sha, "5" * 40, (root_sha,)),
            (root, root_sha, "4" * 40, ()),
        ),
    )
    store = _lineage_store(api, genesis_sha=root_sha)

    assert store.read().ledger == second
    # A valid ancestor has no intrinsic marker that a later head once existed.
    # Branch protection or an external checkpoint must prevent/detect this rollback.
    assert store.read().ledger == first


def test_github_store_rejects_the_wrong_repository_identity_before_reading_ref() -> None:
    store = _lineage_store(
        _StubGitHubApi({"id": REPOSITORY_ID + 1}),
        genesis_sha="1" * 40,
    )

    with pytest.raises(GitHubLedgerError, match="repository"):
        store.read()


def test_github_store_rejects_a_canonical_reset_outside_the_pinned_genesis() -> None:
    root = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="50",
        opening_reserved_upper_cny="10.56",
    )
    head = root.reserve(_request()).ledger
    rogue_root_sha, head_sha = "8" * 40, "9" * 40
    api = _StubGitHubApi(
        *_lineage_read_responses(
            (head, head_sha, "7" * 40, (rogue_root_sha,)),
            (root, rogue_root_sha, "6" * 40, ()),
        )
    )
    store = _lineage_store(api, genesis_sha="1" * 40)

    with pytest.raises(GitHubLedgerError, match="genesis"):
        store.read()
    assert len([call for call in api.calls if "/git/commits/" in call[1]]) == 2


def test_github_store_rejects_a_merge_commit() -> None:
    root = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="50",
        opening_reserved_upper_cny="0",
    )
    head = root.reserve(_request()).ledger
    store = _lineage_store(
        _StubGitHubApi(
            *_lineage_read_responses(
                (head, "3" * 40, "4" * 40, ("1" * 40, "2" * 40)),
            )
        ),
        genesis_sha="1" * 40,
    )

    with pytest.raises(GitHubLedgerError, match="parent"):
        store.read()


@pytest.mark.parametrize("mutation", ("skip", "replace", "budget", "opening"))
def test_github_store_rejects_a_non_append_history_transition(mutation: str) -> None:
    root = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="50",
        opening_reserved_upper_cny="0",
    )
    first = root.reserve(_request()).ledger
    second = first.reserve(
        _request(
            run_id=124,
            mode="readiness",
            upper_cny="2.10",
            input_lock_sha256=None,
        )
    ).ledger
    if mutation == "skip":
        parent, head = root, second
    elif mutation == "replace":
        parent = first
        replaced = root.reserve(_request(run_id=125)).ledger
        head = replaced.reserve(
            _request(
                run_id=124,
                mode="readiness",
                upper_cny="2.10",
                input_lock_sha256=None,
            )
        ).ledger
    elif mutation == "budget":
        parent = first
        head = CampaignLedger(
            campaign_id=second.campaign_id,
            budget_micro_cny=second.budget_micro_cny + 1,
            opening_reserved_upper_micro_cny=second.opening_reserved_upper_micro_cny,
            reservations=second.reservations,
        )
    else:
        parent = first
        head = CampaignLedger(
            campaign_id=second.campaign_id,
            budget_micro_cny=second.budget_micro_cny,
            opening_reserved_upper_micro_cny=1,
            reservations=second.reservations,
        )
    root_sha, parent_sha, head_sha = "1" * 40, "2" * 40, "3" * 40
    store = _lineage_store(
        _StubGitHubApi(
            *_lineage_read_responses(
                (head, head_sha, "6" * 40, (parent_sha,)),
                (parent, parent_sha, "5" * 40, (root_sha,)),
            )
        ),
        genesis_sha=root_sha,
    )

    with pytest.raises(GitHubLedgerError, match="append"):
        store.read()


def test_github_store_rejects_nonempty_or_parented_pinned_genesis() -> None:
    root = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="50",
        opening_reserved_upper_cny="0",
    )
    nonempty = root.reserve(_request()).ledger
    genesis_sha = "1" * 40
    nonempty_store = _lineage_store(
        _StubGitHubApi(
            *_lineage_read_responses(
                (nonempty, genesis_sha, "2" * 40, ()),
            )
        ),
        genesis_sha=genesis_sha,
    )
    with pytest.raises(GitHubLedgerError, match="genesis"):
        nonempty_store.read()

    parented_store = _lineage_store(
        _StubGitHubApi(
            *_lineage_read_responses(
                (root, genesis_sha, "2" * 40, ("0" * 40,)),
            )
        ),
        genesis_sha=genesis_sha,
    )
    with pytest.raises(GitHubLedgerError, match="genesis"):
        parented_store.read()


def test_github_store_rejects_cas_without_a_pinned_genesis_before_any_write() -> None:
    root = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="50",
        opening_reserved_upper_cny="0",
    )
    updated = root.reserve(_request()).ledger
    api = _StubGitHubApi()
    store = _lineage_store(api, genesis_sha=None)

    with pytest.raises(GitHubLedgerError, match="genesis"):
        store.compare_and_swap(
            LedgerSnapshot(
                commit_sha="1" * 40,
                tree_sha="2" * 40,
                ledger=root,
            ),
            updated,
        )
    assert api.calls == []


def test_github_store_rejects_a_tree_less_cas_before_any_api_call() -> None:
    root = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="50",
        opening_reserved_upper_cny="0",
    )
    api = _StubGitHubApi()
    store = _lineage_store(api, genesis_sha="1" * 40)

    with pytest.raises(GitHubLedgerError, match="missing its tree"):
        store.compare_and_swap(
            LedgerSnapshot(commit_sha="1" * 40, ledger=root),
            root.reserve(_request()).ledger,
        )
    assert api.calls == []


def test_github_store_rejects_a_non_append_cas_before_any_api_call() -> None:
    root = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="50",
        opening_reserved_upper_cny="0",
    )
    first = root.reserve(_request()).ledger
    skipped = first.reserve(
        _request(
            run_id=124,
            mode="readiness",
            upper_cny="2.10",
            input_lock_sha256=None,
        )
    ).ledger
    api = _StubGitHubApi()
    store = _lineage_store(api, genesis_sha="1" * 40)

    with pytest.raises(GitHubLedgerError, match="append"):
        store.compare_and_swap(
            LedgerSnapshot(
                commit_sha="1" * 40,
                tree_sha="2" * 40,
                ledger=root,
            ),
            skipped,
        )
    assert api.calls == []


def test_github_store_rechecks_the_verified_snapshot_before_any_write() -> None:
    root = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="50",
        opening_reserved_upper_cny="0",
    )
    competitor = root.reserve(
        _request(
            run_id=124,
            mode="readiness",
            upper_cny="2.10",
            input_lock_sha256=None,
        )
    ).ledger
    root_sha = "1" * 40
    api = _StubGitHubApi(
        *_remote_read_responses(root, head_sha=root_sha, tree_sha="2" * 40),
        *_lineage_read_responses(
            (competitor, "3" * 40, "4" * 40, (root_sha,)),
            (root, root_sha, "2" * 40, ()),
        ),
    )
    store = _lineage_store(api, genesis_sha=root_sha)
    expected = store.read()

    with pytest.raises(CasConflict, match="advanced"):
        store.compare_and_swap(expected, root.reserve(_request()).ledger)
    assert not any(call[0] == "POST" for call in api.calls)


def test_github_store_requires_a_pinned_genesis_to_read_an_existing_branch() -> None:
    api = _StubGitHubApi()
    store = _lineage_store(api, genesis_sha=None)

    with pytest.raises(GitHubLedgerError, match="genesis"):
        store.read()
    assert api.calls == []


def test_github_store_reads_and_verifies_the_exact_git_object_chain() -> None:
    ledger = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="50",
        opening_reserved_upper_cny="10.56",
    )
    api = _StubGitHubApi(
        *_remote_read_responses(ledger, head_sha="1" * 40, tree_sha="2" * 40)
    )
    store = _lineage_store(api, genesis_sha="1" * 40)

    snapshot = store.read()

    assert snapshot == LedgerSnapshot(
        commit_sha="1" * 40,
        tree_sha="2" * 40,
        ledger=ledger,
    )
    assert [call[:2] for call in api.calls] == [
        ("GET", "repos/Chloride233/joinlint"),
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
    initial_read = _remote_read_responses(
        initial,
        head_sha="1" * 40,
        tree_sha="2" * 40,
    )
    api = _StubGitHubApi(
        *initial_read,
        *initial_read,
        {"sha": new_blob_sha},
        {"sha": "3" * 40},
        {"sha": "4" * 40},
        {
            "ref": "refs/heads/joinlint-campaign-ledger",
            "object": {"type": "commit", "sha": "4" * 40},
        },
        *_lineage_read_responses(
            (updated, "4" * 40, "3" * 40, ("1" * 40,)),
            (initial, "1" * 40, "2" * 40, ()),
        ),
    )
    store = _lineage_store(api, genesis_sha="1" * 40)
    snapshot = store.read()

    assert store.compare_and_swap(snapshot, updated) == "4" * 40
    create_tree = next(
        call
        for call in api.calls
        if call[0] == "POST" and call[1].endswith("/git/trees")
    )
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
    create_commit = next(
        call
        for call in api.calls
        if call[0] == "POST" and call[1].endswith("/git/commits")
    )
    assert create_commit[2]["parents"] == ["1" * 40]
    assert create_commit[2]["message"].startswith(
        f"reserve {updated.reservations[-1].reservation_id} "
    )
    assert create_commit[2]["author"] == create_commit[2]["committer"]
    update_ref = next(call for call in api.calls if call[0] == "PATCH")
    assert update_ref[2] == {"force": False, "sha": "4" * 40}


def test_github_store_recovers_only_an_exact_commit_after_unknown_patch_result() -> None:
    initial = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="10",
        opening_reserved_upper_cny="0",
    )
    updated = initial.reserve(_request()).ledger
    new_blob_sha = _git_blob_sha(updated.to_bytes())
    initial_read = _remote_read_responses(
        initial,
        head_sha="1" * 40,
        tree_sha="2" * 40,
    )
    api = _StubGitHubApi(
        *initial_read,
        *initial_read,
        {"sha": new_blob_sha},
        {"sha": "3" * 40},
        {"sha": "4" * 40},
        GitHubApiError(status=None),
        *_lineage_read_responses(
            (updated, "4" * 40, "3" * 40, ("1" * 40,)),
            (initial, "1" * 40, "2" * 40, ()),
        ),
    )
    store = _lineage_store(api, genesis_sha="1" * 40)
    snapshot = store.read()

    assert store.compare_and_swap(
        snapshot,
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
    initial_read = _remote_read_responses(
        initial,
        head_sha="1" * 40,
        tree_sha="2" * 40,
    )

    winner_api = _StubGitHubApi(
        *initial_read,
        *initial_read,
        {"sha": new_blob_sha},
        {"sha": "3" * 40},
        {"sha": "4" * 40},
        {
            "ref": "refs/heads/joinlint-campaign-ledger",
            "object": {"type": "commit", "sha": "4" * 40},
        },
        *_lineage_read_responses(
            (updated, "4" * 40, "3" * 40, ("1" * 40,)),
            (initial, "1" * 40, "2" * 40, ()),
        ),
    )
    winner = GitHubGitLedgerStore(
        api=winner_api,
        repository=REPOSITORY,
        branch=LEDGER_BRANCH,
        expected_repository_id=REPOSITORY_ID,
        expected_genesis_commit="1" * 40,
        nonce_factory=lambda: "a" * 32,
    )
    snapshot = winner.read()
    assert winner.compare_and_swap(snapshot, updated) == "4" * 40

    loser_api = _StubGitHubApi(
        *initial_read,
        *initial_read,
        {"sha": new_blob_sha},
        {"sha": "3" * 40},
        {"sha": "5" * 40},
        GitHubApiError(status=loser_status),
        *_lineage_read_responses(
            (updated, "4" * 40, "3" * 40, ("1" * 40,)),
            (initial, "1" * 40, "2" * 40, ()),
        ),
    )
    loser = GitHubGitLedgerStore(
        api=loser_api,
        repository=REPOSITORY,
        branch=LEDGER_BRANCH,
        expected_repository_id=REPOSITORY_ID,
        expected_genesis_commit="1" * 40,
        nonce_factory=lambda: "b" * 32,
    )
    loser_snapshot = loser.read()
    with pytest.raises(CasConflict):
        loser.compare_and_swap(loser_snapshot, updated)

    winner_commit = next(
        call
        for call in winner_api.calls
        if call[0] == "POST" and call[1].endswith("/git/commits")
    )
    loser_commit = next(
        call
        for call in loser_api.calls
        if call[0] == "POST" and call[1].endswith("/git/commits")
    )
    assert winner_commit[2]["message"] != loser_commit[2]["message"]


def test_github_store_distinguishes_ref_rejection_from_a_sibling_commit() -> None:
    initial = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="10",
        opening_reserved_upper_cny="0",
    )
    updated = initial.reserve(_request()).ledger
    new_blob_sha = _git_blob_sha(updated.to_bytes())
    initial_read = _remote_read_responses(
        initial,
        head_sha="1" * 40,
        tree_sha="2" * 40,
    )

    unchanged_api = _StubGitHubApi(
        *initial_read,
        *initial_read,
        {"sha": new_blob_sha},
        {"sha": "3" * 40},
        {"sha": "4" * 40},
        GitHubApiError(status=422),
        *initial_read,
    )
    unchanged_store = _lineage_store(unchanged_api, genesis_sha="1" * 40)
    snapshot = unchanged_store.read()
    with pytest.raises(GitHubLedgerError, match="rejected"):
        unchanged_store.compare_and_swap(snapshot, updated)

    competitor = initial.reserve(
        _request(
            run_id=200,
            mode="readiness",
            upper_cny="2.10",
            input_lock_sha256=None,
        )
    ).ledger
    changed_api = _StubGitHubApi(
        *initial_read,
        *initial_read,
        {"sha": new_blob_sha},
        {"sha": "3" * 40},
        {"sha": "4" * 40},
        GitHubApiError(status=409),
        *_lineage_read_responses(
            (competitor, "5" * 40, "6" * 40, ("1" * 40,)),
            (initial, "1" * 40, "2" * 40, ()),
        ),
    )
    changed_store = _lineage_store(changed_api, genesis_sha="1" * 40)
    changed_snapshot = changed_store.read()
    with pytest.raises(CasConflict, match="advanced"):
        changed_store.compare_and_swap(changed_snapshot, updated)


def test_github_store_rejects_extra_tree_entries_and_wrong_blob_identity() -> None:
    ledger = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="10",
        opening_reserved_upper_cny="0",
    )
    responses = list(
        _remote_read_responses(ledger, head_sha="1" * 40, tree_sha="2" * 40)
    )
    responses[3]["tree"].append(
        {"path": "extra", "mode": "100644", "type": "blob", "sha": "3" * 40}
    )
    store = _lineage_store(_StubGitHubApi(*responses), genesis_sha="1" * 40)
    with pytest.raises(GitHubLedgerError, match="tree"):
        store.read()

    responses = list(
        _remote_read_responses(ledger, head_sha="1" * 40, tree_sha="2" * 40)
    )
    responses[4]["sha"] = "0" * 40
    store = _lineage_store(_StubGitHubApi(*responses), genesis_sha="1" * 40)
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
        {"id": REPOSITORY_ID},
        {"sha": blob_sha},
        {"sha": "2" * 40},
        {"sha": "3" * 40},
        {
            "ref": "refs/heads/joinlint-campaign-ledger",
            "object": {"type": "commit", "sha": "3" * 40},
        },
        *_remote_read_responses(ledger, head_sha="3" * 40, tree_sha="2" * 40),
    )
    store = _lineage_store(api, genesis_sha=None)

    assert store.initialize(ledger) == "3" * 40
    create_tree = next(
        call
        for call in api.calls
        if call[0] == "POST" and call[1].endswith("/git/trees")
    )
    assert create_tree[2] == {
        "tree": [
            {
                "mode": "100644",
                "path": "campaign-ledger.json",
                "sha": blob_sha,
                "type": "blob",
            }
        ]
    }
    create_commit = next(
        call
        for call in api.calls
        if call[0] == "POST" and call[1].endswith("/git/commits")
    )
    assert create_commit[2]["parents"] == []
    create_ref = next(
        call
        for call in api.calls
        if call[0] == "POST" and call[1].endswith("/git/refs")
    )
    assert create_ref[2] == {
        "ref": "refs/heads/joinlint-campaign-ledger",
        "sha": "3" * 40,
    }


@pytest.mark.parametrize("status", (None, 409, 422))
def test_github_store_recovers_an_exact_initialized_root_after_an_uncertain_ref_result(
    status: int | None,
) -> None:
    ledger = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="0.000001",
        opening_reserved_upper_cny="0",
    )
    candidate_sha, tree_sha = "3" * 40, "2" * 40
    api = _StubGitHubApi(
        *_initialize_candidate_responses(
            ledger,
            tree_sha=tree_sha,
            commit_sha=candidate_sha,
        ),
        GitHubApiError(status=status),
        *_remote_read_responses(
            ledger,
            head_sha=candidate_sha,
            tree_sha=tree_sha,
        ),
    )

    assert _lineage_store(api, genesis_sha=None).initialize(ledger) == candidate_sha


def test_github_store_recovers_an_exact_initialized_root_after_a_malformed_ref_response() -> None:
    ledger = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="0.000001",
        opening_reserved_upper_cny="0",
    )
    candidate_sha, tree_sha = "3" * 40, "2" * 40
    api = _StubGitHubApi(
        *_initialize_candidate_responses(
            ledger,
            tree_sha=tree_sha,
            commit_sha=candidate_sha,
        ),
        {
            "ref": "refs/heads/wrong-branch",
            "object": {"type": "commit", "sha": candidate_sha},
        },
        *_remote_read_responses(
            ledger,
            head_sha=candidate_sha,
            tree_sha=tree_sha,
        ),
    )

    assert _lineage_store(api, genesis_sha=None).initialize(ledger) == candidate_sha


@pytest.mark.parametrize("readback", ("competitor", "wrong-ledger"))
def test_github_store_fails_closed_when_uncertain_initialization_readback_is_not_exact(
    readback: str,
) -> None:
    ledger = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="0.000001",
        opening_reserved_upper_cny="0",
    )
    candidate_sha, tree_sha = "3" * 40, "2" * 40
    if readback == "competitor":
        current_ledger = ledger
        current_sha = "4" * 40
    else:
        current_ledger = CampaignLedger.create(
            campaign_id=CAMPAIGN_ID,
            budget_cny="0.000002",
            opening_reserved_upper_cny="0",
        )
        current_sha = candidate_sha
    api = _StubGitHubApi(
        *_initialize_candidate_responses(
            ledger,
            tree_sha=tree_sha,
            commit_sha=candidate_sha,
        ),
        GitHubApiError(status=None),
        *_remote_read_responses(
            current_ledger,
            head_sha=current_sha,
            tree_sha="5" * 40,
        ),
    )
    store = _lineage_store(api, genesis_sha=None)

    with pytest.raises(GitHubLedgerError, match="could not be verified"):
        store.initialize(ledger)
    calls_before_unpinned_read = len(api.calls)
    with pytest.raises(GitHubLedgerError, match="not pinned"):
        store.read()
    assert len(api.calls) == calls_before_unpinned_read


def test_github_store_can_retry_initialization_after_an_unverifiable_ref_result() -> None:
    ledger = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="0.000001",
        opening_reserved_upper_cny="0",
    )
    candidate_sha, tree_sha = "3" * 40, "2" * 40
    candidate_responses = _initialize_candidate_responses(
        ledger,
        tree_sha=tree_sha,
        commit_sha=candidate_sha,
    )
    api = _StubGitHubApi(
        *candidate_responses,
        GitHubApiError(status=None),
        GitHubApiError(status=None),
        *candidate_responses,
        GitHubApiError(status=422),
        *_remote_read_responses(
            ledger,
            head_sha=candidate_sha,
            tree_sha=tree_sha,
        ),
    )
    store = _lineage_store(api, genesis_sha=None)

    with pytest.raises(GitHubLedgerError, match="could not be verified"):
        store.initialize(ledger)
    assert store.initialize(ledger) == candidate_sha


def test_github_store_rejects_a_nonempty_genesis_before_any_api_call() -> None:
    root = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="10",
        opening_reserved_upper_cny="0",
    )
    api = _StubGitHubApi()
    store = _lineage_store(api, genesis_sha=None)

    with pytest.raises(GitHubLedgerError, match="no reservations"):
        store.initialize(root.reserve(_request()).ledger)
    assert api.calls == []


def test_github_store_rejects_initialization_after_genesis_is_pinned() -> None:
    ledger = CampaignLedger.create(
        campaign_id=CAMPAIGN_ID,
        budget_cny="10",
        opening_reserved_upper_cny="0",
    )
    api = _StubGitHubApi()
    store = _lineage_store(api, genesis_sha="1" * 40)

    with pytest.raises(GitHubLedgerError, match="already pinned"):
        store.initialize(ledger)
    assert api.calls == []


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


def test_campaign_ledger_cli_has_no_caller_defined_reservation_command() -> None:
    with pytest.raises(SystemExit) as error:
        campaign_ledger_main(["reserve-github"])
    assert error.value.code == 2
