from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import secrets
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Protocol


_CNY_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?\Z")
_CAMPAIGN_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_REPOSITORY_PATTERN = re.compile(
    r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z"
)
_WORKFLOW_PATTERN = re.compile(
    r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml\Z"
)
_LABEL_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_SHA1_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_NONCE_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
_BRANCH_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_LEDGER_PATH = "campaign-ledger.json"
_GIT_IDENTITY = {
    "date": "2000-01-01T00:00:00Z",
    "email": "41898282+github-actions[bot]@users.noreply.github.com",
    "name": "joinlint-campaign-ledger",
}


class CampaignLedgerError(ValueError):
    pass


class CampaignBudgetExceeded(CampaignLedgerError):
    pass


class ReservationConflict(CampaignLedgerError):
    pass


class SettlementConflict(CampaignLedgerError):
    pass


class CasConflict(RuntimeError):
    pass


class GitHubApiError(RuntimeError):
    def __init__(self, *, status: int | None) -> None:
        self.status = status
        super().__init__(
            "GitHub API request failed"
            if status is None
            else f"GitHub API request failed with status {status}"
        )


class GitHubLedgerError(RuntimeError):
    pass


def parse_cny_micro(value: str) -> int:
    if not isinstance(value, str) or _CNY_PATTERN.fullmatch(value) is None:
        raise CampaignLedgerError("CNY amount must be a bounded decimal string")
    whole, separator, fraction = value.partition(".")
    micro_fraction = (fraction if separator else "").ljust(6, "0")
    return int(whole) * 1_000_000 + int(micro_fraction or "0")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _require_exact_int(value: object, *, field: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise CampaignLedgerError(f"{field} must be an integer >= {minimum}")
    return value


def _require_pattern(value: object, *, field: str, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise CampaignLedgerError(f"{field} is invalid")
    return value


@dataclass(frozen=True)
class ReservationRequest:
    repository_id: int
    repository: str
    workflow_path: str
    job: str
    mode: str
    run_id: int
    run_attempt: int
    workflow_sha: str
    evaluated_commit: str
    input_lock_sha256: str | None
    upper_micro_cny: int

    def __post_init__(self) -> None:
        _require_exact_int(self.repository_id, field="repository_id", minimum=1)
        _require_pattern(
            self.repository,
            field="repository",
            pattern=_REPOSITORY_PATTERN,
        )
        _require_pattern(
            self.workflow_path,
            field="workflow_path",
            pattern=_WORKFLOW_PATTERN,
        )
        _require_pattern(self.job, field="job", pattern=_LABEL_PATTERN)
        _require_pattern(self.mode, field="mode", pattern=_LABEL_PATTERN)
        _require_exact_int(self.run_id, field="run_id", minimum=1)
        if type(self.run_attempt) is not int or self.run_attempt != 1:
            raise CampaignLedgerError("run_attempt must equal 1")
        _require_pattern(self.workflow_sha, field="workflow_sha", pattern=_SHA1_PATTERN)
        _require_pattern(
            self.evaluated_commit,
            field="evaluated_commit",
            pattern=_SHA1_PATTERN,
        )
        if self.input_lock_sha256 is not None:
            _require_pattern(
                self.input_lock_sha256,
                field="input_lock_sha256",
                pattern=_SHA256_PATTERN,
            )
        if (self.mode == "readiness" and self.input_lock_sha256 is not None) or (
            self.mode in {"calibration", "pilot", "pilot_stage"}
            and self.input_lock_sha256 is None
        ):
            raise CampaignLedgerError("input lock does not match reservation mode")
        _require_exact_int(self.upper_micro_cny, field="upper_micro_cny", minimum=1)

    @classmethod
    def create(
        cls,
        *,
        repository_id: int,
        repository: str,
        workflow_path: str,
        job: str,
        mode: str,
        run_id: int,
        run_attempt: int,
        workflow_sha: str,
        evaluated_commit: str,
        input_lock_sha256: str | None,
        upper_cny: str,
    ) -> ReservationRequest:
        upper_micro_cny = parse_cny_micro(upper_cny)
        if upper_micro_cny <= 0:
            raise CampaignLedgerError("reservation upper bound must be positive")
        return cls(
            repository_id=repository_id,
            repository=repository,
            workflow_path=workflow_path,
            job=job,
            mode=mode,
            run_id=run_id,
            run_attempt=run_attempt,
            workflow_sha=workflow_sha,
            evaluated_commit=evaluated_commit,
            input_lock_sha256=input_lock_sha256,
            upper_micro_cny=upper_micro_cny,
        )

    @property
    def key(self) -> tuple[int, int]:
        return (self.repository_id, self.run_id)

    def to_dict(self) -> dict[str, object]:
        return {
            "evaluated_commit": self.evaluated_commit,
            "input_lock_sha256": self.input_lock_sha256,
            "job": self.job,
            "mode": self.mode,
            "repository": self.repository,
            "repository_id": self.repository_id,
            "run_attempt": self.run_attempt,
            "run_id": self.run_id,
            "upper_micro_cny": self.upper_micro_cny,
            "workflow_path": self.workflow_path,
            "workflow_sha": self.workflow_sha,
        }

    @property
    def reservation_id(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_dict())).hexdigest()

    def to_reservation(self) -> Reservation:
        return Reservation(
            reservation_id=self.reservation_id,
            **self.to_dict(),
        )


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    repository_id: int
    repository: str
    workflow_path: str
    job: str
    mode: str
    run_id: int
    run_attempt: int
    workflow_sha: str
    evaluated_commit: str
    input_lock_sha256: str | None
    upper_micro_cny: int

    def __post_init__(self) -> None:
        request = self.request
        _require_pattern(
            self.reservation_id,
            field="reservation_id",
            pattern=_SHA256_PATTERN,
        )
        if self.reservation_id != request.reservation_id:
            raise CampaignLedgerError("reservation identity is inconsistent")

    @property
    def request(self) -> ReservationRequest:
        return ReservationRequest(
            repository_id=self.repository_id,
            repository=self.repository,
            workflow_path=self.workflow_path,
            job=self.job,
            mode=self.mode,
            run_id=self.run_id,
            run_attempt=self.run_attempt,
            workflow_sha=self.workflow_sha,
            evaluated_commit=self.evaluated_commit,
            input_lock_sha256=self.input_lock_sha256,
            upper_micro_cny=self.upper_micro_cny,
        )

    @property
    def key(self) -> tuple[int, int]:
        return self.request.key

    def to_dict(self) -> dict[str, object]:
        return {"reservation_id": self.reservation_id, **self.request.to_dict()}

    @classmethod
    def from_dict(cls, value: object) -> Reservation:
        if type(value) is not dict or set(value) != {
            "evaluated_commit",
            "input_lock_sha256",
            "job",
            "mode",
            "repository",
            "repository_id",
            "reservation_id",
            "run_attempt",
            "run_id",
            "upper_micro_cny",
            "workflow_path",
            "workflow_sha",
        }:
            raise CampaignLedgerError("reservation fields are invalid")
        return cls(**value)


@dataclass(frozen=True)
class ReserveResult:
    ledger: CampaignLedger
    reservation: Reservation
    created: bool


@dataclass(frozen=True)
class SettlementRequest:
    reservation_id: str
    accounted_upper_micro_cny: int
    evidence_sha256: str

    def __post_init__(self) -> None:
        _require_pattern(
            self.reservation_id,
            field="settlement reservation_id",
            pattern=_SHA256_PATTERN,
        )
        _require_exact_int(
            self.accounted_upper_micro_cny,
            field="accounted_upper_micro_cny",
        )
        _require_pattern(
            self.evidence_sha256,
            field="settlement evidence_sha256",
            pattern=_SHA256_PATTERN,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "accounted_upper_micro_cny": self.accounted_upper_micro_cny,
            "evidence_sha256": self.evidence_sha256,
            "reservation_id": self.reservation_id,
        }

    @property
    def settlement_id(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_dict())).hexdigest()

    def to_settlement(self) -> Settlement:
        return Settlement(settlement_id=self.settlement_id, **self.to_dict())


@dataclass(frozen=True)
class Settlement:
    settlement_id: str
    reservation_id: str
    accounted_upper_micro_cny: int
    evidence_sha256: str

    def __post_init__(self) -> None:
        request = self.request
        _require_pattern(
            self.settlement_id,
            field="settlement_id",
            pattern=_SHA256_PATTERN,
        )
        if self.settlement_id != request.settlement_id:
            raise CampaignLedgerError("settlement identity is inconsistent")

    @property
    def request(self) -> SettlementRequest:
        return SettlementRequest(
            reservation_id=self.reservation_id,
            accounted_upper_micro_cny=self.accounted_upper_micro_cny,
            evidence_sha256=self.evidence_sha256,
        )

    def to_dict(self) -> dict[str, object]:
        return {"settlement_id": self.settlement_id, **self.request.to_dict()}

    @classmethod
    def from_dict(cls, value: object) -> Settlement:
        if type(value) is not dict or set(value) != {
            "accounted_upper_micro_cny",
            "evidence_sha256",
            "reservation_id",
            "settlement_id",
        }:
            raise CampaignLedgerError("settlement fields are invalid")
        return cls(**value)


@dataclass(frozen=True)
class SettlementResult:
    ledger: CampaignLedger
    settlement: Settlement
    created: bool


@dataclass(frozen=True)
class CampaignLedger:
    campaign_id: str
    budget_micro_cny: int
    opening_reserved_upper_micro_cny: int
    reservations: tuple[Reservation, ...] = ()
    settlements: tuple[Settlement, ...] = ()
    schema_version: int = 2
    currency: str = "CNY"

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version not in {2, 3}:
            raise CampaignLedgerError("campaign ledger schema_version must equal 2 or 3")
        if self.currency != "CNY":
            raise CampaignLedgerError("campaign ledger currency must be CNY")
        _require_pattern(
            self.campaign_id,
            field="campaign_id",
            pattern=_CAMPAIGN_ID_PATTERN,
        )
        _require_exact_int(self.budget_micro_cny, field="budget_micro_cny")
        _require_exact_int(
            self.opening_reserved_upper_micro_cny,
            field="opening_reserved_upper_micro_cny",
        )
        if type(self.reservations) is not tuple or not all(
            isinstance(reservation, Reservation) for reservation in self.reservations
        ):
            raise CampaignLedgerError("reservations must be a tuple of valid records")
        if type(self.settlements) is not tuple or not all(
            isinstance(settlement, Settlement) for settlement in self.settlements
        ):
            raise CampaignLedgerError("settlements must be a tuple of valid records")
        if self.schema_version == 2 and self.settlements:
            raise CampaignLedgerError("schema version 2 cannot contain settlements")
        keys = [reservation.key for reservation in self.reservations]
        identities = [reservation.reservation_id for reservation in self.reservations]
        if len(keys) != len(set(keys)) or len(identities) != len(set(identities)):
            raise CampaignLedgerError("campaign ledger contains duplicate reservations")
        settlement_reservations = [
            settlement.reservation_id for settlement in self.settlements
        ]
        settlement_ids = [settlement.settlement_id for settlement in self.settlements]
        if (
            len(settlement_reservations) != len(set(settlement_reservations))
            or len(settlement_ids) != len(set(settlement_ids))
        ):
            raise CampaignLedgerError("campaign ledger contains duplicate settlements")
        reservations_by_id = {
            reservation.reservation_id: reservation for reservation in self.reservations
        }
        for settlement in self.settlements:
            reservation = reservations_by_id.get(settlement.reservation_id)
            if reservation is None:
                raise CampaignLedgerError("settlement references an unknown reservation")
            if settlement.accounted_upper_micro_cny > reservation.upper_micro_cny:
                raise CampaignLedgerError("settlement exceeds its reservation upper bound")
        if self.reserved_upper_micro_cny > self.budget_micro_cny:
            raise CampaignBudgetExceeded("campaign reserved upper bound exceeds its budget")

    @classmethod
    def create(
        cls,
        *,
        campaign_id: str,
        budget_cny: str,
        opening_reserved_upper_cny: str,
    ) -> CampaignLedger:
        return cls(
            campaign_id=campaign_id,
            budget_micro_cny=parse_cny_micro(budget_cny),
            opening_reserved_upper_micro_cny=parse_cny_micro(
                opening_reserved_upper_cny
            ),
        )

    @property
    def reserved_upper_micro_cny(self) -> int:
        settled = {
            settlement.reservation_id: settlement.accounted_upper_micro_cny
            for settlement in self.settlements
        }
        return self.opening_reserved_upper_micro_cny + sum(
            settled.get(reservation.reservation_id, reservation.upper_micro_cny)
            for reservation in self.reservations
        )

    @property
    def remaining_micro_cny(self) -> int:
        return self.budget_micro_cny - self.reserved_upper_micro_cny

    def reserved_before(self, reservation: Reservation) -> int:
        index = self.reservations.index(reservation)
        settled = {
            settlement.reservation_id: settlement.accounted_upper_micro_cny
            for settlement in self.settlements
        }
        return self.opening_reserved_upper_micro_cny + sum(
            settled.get(item.reservation_id, item.upper_micro_cny)
            for item in self.reservations[:index]
        )

    def reserve(self, request: ReservationRequest) -> ReserveResult:
        candidate = request.to_reservation()
        for reservation in self.reservations:
            if reservation.key != request.key:
                continue
            if reservation == candidate:
                return ReserveResult(
                    ledger=self,
                    reservation=reservation,
                    created=False,
                )
            raise ReservationConflict(
                "reservation key already exists with a different payload"
            )
        if request.upper_micro_cny > self.remaining_micro_cny:
            raise CampaignBudgetExceeded(
                "reservation would exceed the remaining campaign budget"
            )
        updated = replace(self, reservations=(*self.reservations, candidate))
        return ReserveResult(ledger=updated, reservation=candidate, created=True)

    def settle(self, request: SettlementRequest) -> SettlementResult:
        candidate = request.to_settlement()
        reservation = next(
            (
                item
                for item in self.reservations
                if item.reservation_id == request.reservation_id
            ),
            None,
        )
        if reservation is None:
            raise SettlementConflict("settlement references an unknown reservation")
        if request.accounted_upper_micro_cny > reservation.upper_micro_cny:
            raise SettlementConflict("settlement exceeds its reservation upper bound")
        for settlement in self.settlements:
            if settlement.reservation_id != request.reservation_id:
                continue
            if settlement == candidate:
                return SettlementResult(
                    ledger=self,
                    settlement=settlement,
                    created=False,
                )
            raise SettlementConflict(
                "reservation is already settled with different evidence"
            )
        updated = replace(
            self,
            schema_version=3,
            settlements=(*self.settlements, candidate),
        )
        return SettlementResult(ledger=updated, settlement=candidate, created=True)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "budget_micro_cny": self.budget_micro_cny,
            "campaign_id": self.campaign_id,
            "currency": self.currency,
            "opening_reserved_upper_micro_cny": (
                self.opening_reserved_upper_micro_cny
            ),
            "reservations": [
                reservation.to_dict() for reservation in self.reservations
            ],
            "schema_version": self.schema_version,
        }
        if self.schema_version == 3:
            payload["settlements"] = [
                settlement.to_dict() for settlement in self.settlements
            ]
        return payload

    def to_bytes(self) -> bytes:
        return _canonical_json(self.to_dict()) + b"\n"

    @classmethod
    def from_bytes(cls, raw: bytes) -> CampaignLedger:
        if not isinstance(raw, bytes) or not raw.endswith(b"\n") or raw.endswith(
            b"\n\n"
        ):
            raise CampaignLedgerError("campaign ledger must end in one LF")
        try:
            decoded = json.loads(
                raw[:-1].decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_nonfinite,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CampaignLedgerError("campaign ledger JSON is invalid") from error
        if type(decoded) is not dict:
            raise CampaignLedgerError("campaign ledger fields are invalid")
        schema_version = decoded.get("schema_version")
        expected_fields = {
            "budget_micro_cny",
            "campaign_id",
            "currency",
            "opening_reserved_upper_micro_cny",
            "reservations",
            "schema_version",
        }
        if schema_version == 3:
            expected_fields.add("settlements")
        if set(decoded) != expected_fields:
            raise CampaignLedgerError("campaign ledger fields are invalid")
        reservations = decoded["reservations"]
        if type(reservations) is not list:
            raise CampaignLedgerError("campaign ledger reservations must be a list")
        settlements = decoded.get("settlements", [])
        if type(settlements) is not list:
            raise CampaignLedgerError("campaign ledger settlements must be a list")
        ledger = cls(
            schema_version=decoded["schema_version"],
            campaign_id=decoded["campaign_id"],
            currency=decoded["currency"],
            budget_micro_cny=decoded["budget_micro_cny"],
            opening_reserved_upper_micro_cny=decoded[
                "opening_reserved_upper_micro_cny"
            ],
            reservations=tuple(
                Reservation.from_dict(reservation) for reservation in reservations
            ),
            settlements=tuple(
                Settlement.from_dict(settlement) for settlement in settlements
            ),
        )
        if raw != ledger.to_bytes():
            raise CampaignLedgerError("campaign ledger JSON must be canonical")
        return ledger


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise CampaignLedgerError("campaign ledger JSON contains duplicate keys")
        value[key] = item
    return value


def _reject_nonfinite(value: str) -> None:
    raise CampaignLedgerError(f"campaign ledger contains non-finite value: {value}")


@dataclass(frozen=True)
class LedgerSnapshot:
    commit_sha: str
    ledger: CampaignLedger
    tree_sha: str | None = None

    def __post_init__(self) -> None:
        _require_pattern(self.commit_sha, field="commit_sha", pattern=_SHA1_PATTERN)
        if self.tree_sha is not None:
            _require_pattern(self.tree_sha, field="tree_sha", pattern=_SHA1_PATTERN)


@dataclass(frozen=True)
class ReservationReceipt:
    authorized: bool
    campaign_id: str
    reservation: Reservation
    ledger_commit_sha: str
    budget_micro_cny: int
    upper_micro_cny: int
    reserved_before_micro_cny: int
    reserved_after_micro_cny: int

    def __post_init__(self) -> None:
        if type(self.authorized) is not bool:
            raise CampaignLedgerError("receipt authorization must be a boolean")
        _require_pattern(
            self.campaign_id,
            field="receipt campaign_id",
            pattern=_CAMPAIGN_ID_PATTERN,
        )
        _require_pattern(
            self.ledger_commit_sha,
            field="receipt ledger_commit_sha",
            pattern=_SHA1_PATTERN,
        )
        _require_exact_int(
            self.budget_micro_cny,
            field="receipt budget_micro_cny",
        )
        _require_exact_int(
            self.upper_micro_cny,
            field="receipt upper_micro_cny",
            minimum=1,
        )
        _require_exact_int(
            self.reserved_before_micro_cny,
            field="receipt reserved_before_micro_cny",
        )
        _require_exact_int(
            self.reserved_after_micro_cny,
            field="receipt reserved_after_micro_cny",
        )
        if self.upper_micro_cny != self.reservation.upper_micro_cny:
            raise CampaignLedgerError("receipt upper bound is inconsistent")
        if (
            self.reserved_after_micro_cny
            != self.reserved_before_micro_cny + self.upper_micro_cny
            or self.reserved_after_micro_cny > self.budget_micro_cny
        ):
            raise CampaignLedgerError("receipt reserved totals are inconsistent")

    @property
    def reservation_id(self) -> str:
        return self.reservation.reservation_id

    def to_dict(self) -> dict[str, object]:
        return {
            "authorized": self.authorized,
            "budget_micro_cny": self.budget_micro_cny,
            "campaign_id": self.campaign_id,
            "currency": "CNY",
            "ledger_commit_sha": self.ledger_commit_sha,
            "reservation": self.reservation.to_dict(),
            "reserved_after_micro_cny": self.reserved_after_micro_cny,
            "reserved_before_micro_cny": self.reserved_before_micro_cny,
            "schema_version": 2,
            "upper_micro_cny": self.upper_micro_cny,
        }

    def to_bytes(self) -> bytes:
        return _canonical_json(self.to_dict()) + b"\n"

    @classmethod
    def from_bytes(cls, raw: bytes) -> ReservationReceipt:
        if not isinstance(raw, bytes) or not raw.endswith(b"\n") or raw.endswith(
            b"\n\n"
        ):
            raise CampaignLedgerError("reservation receipt must end in one LF")
        try:
            decoded = json.loads(
                raw[:-1].decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_nonfinite,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CampaignLedgerError("reservation receipt JSON is invalid") from error
        if type(decoded) is not dict or set(decoded) != {
            "authorized",
            "budget_micro_cny",
            "campaign_id",
            "currency",
            "ledger_commit_sha",
            "reservation",
            "reserved_after_micro_cny",
            "reserved_before_micro_cny",
            "schema_version",
            "upper_micro_cny",
        }:
            raise CampaignLedgerError("reservation receipt fields are invalid")
        if decoded["schema_version"] != 2 or decoded["currency"] != "CNY":
            raise CampaignLedgerError("reservation receipt contract is invalid")
        receipt = cls(
            authorized=decoded["authorized"],
            campaign_id=decoded["campaign_id"],
            reservation=Reservation.from_dict(decoded["reservation"]),
            ledger_commit_sha=decoded["ledger_commit_sha"],
            budget_micro_cny=decoded["budget_micro_cny"],
            upper_micro_cny=decoded["upper_micro_cny"],
            reserved_before_micro_cny=decoded["reserved_before_micro_cny"],
            reserved_after_micro_cny=decoded["reserved_after_micro_cny"],
        )
        if raw != receipt.to_bytes():
            raise CampaignLedgerError("reservation receipt JSON must be canonical")
        return receipt


@dataclass(frozen=True)
class SettlementReceipt:
    created: bool
    campaign_id: str
    settlement: Settlement
    ledger_commit_sha: str
    reserved_before_micro_cny: int
    reserved_after_micro_cny: int


class LedgerStore(Protocol):
    def read(self) -> LedgerSnapshot: ...

    def compare_and_swap(
        self,
        expected: LedgerSnapshot,
        updated: CampaignLedger,
    ) -> str: ...


class GitHubApi(Protocol):
    def request(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]: ...


class GhCliApi:
    def __init__(
        self,
        *,
        runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    ) -> None:
        self._runner = runner

    def request(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        command = [
            "gh",
            "api",
            "--method",
            method,
            "--header",
            "Accept: application/vnd.github+json",
            "--header",
            "X-GitHub-Api-Version: 2022-11-28",
            endpoint,
        ]
        request_body = None
        if payload is not None:
            command.extend(("--input", "-"))
            request_body = _canonical_json(payload)
        result = self._runner(
            command,
            input=request_body,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")
            match = re.search(r"HTTP ([0-9]{3})", stderr)
            raise GitHubApiError(status=int(match.group(1)) if match else None)
        try:
            decoded = json.loads(
                result.stdout.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_nonfinite,
            )
        except (CampaignLedgerError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GitHubApiError(status=None) from error
        if type(decoded) is not dict:
            raise GitHubApiError(status=None)
        return decoded


class GitHubGitLedgerStore:
    def __init__(
        self,
        *,
        api: GitHubApi,
        repository: str,
        branch: str,
        expected_repository_id: int,
        expected_genesis_commit: str | None,
        nonce_factory: Callable[[], str] = lambda: secrets.token_hex(16),
    ) -> None:
        if _REPOSITORY_PATTERN.fullmatch(repository) is None:
            raise GitHubLedgerError("GitHub repository identity is invalid")
        if _BRANCH_PATTERN.fullmatch(branch) is None:
            raise GitHubLedgerError("GitHub ledger branch is invalid")
        if type(expected_repository_id) is not int or expected_repository_id < 1:
            raise GitHubLedgerError("expected GitHub repository ID is invalid")
        if (
            expected_genesis_commit is not None
            and (
                type(expected_genesis_commit) is not str
                or _SHA1_PATTERN.fullmatch(expected_genesis_commit) is None
            )
        ):
            raise GitHubLedgerError("expected GitHub genesis commit is invalid")
        self._api = api
        self._repository = repository
        self._branch = branch
        self._expected_repository_id = expected_repository_id
        self._expected_genesis_commit = expected_genesis_commit
        self._nonce_factory = nonce_factory

    def initialize(self, ledger: CampaignLedger) -> str:
        if self._expected_genesis_commit is not None:
            raise GitHubLedgerError("GitHub ledger genesis is already pinned")
        if ledger.reservations or ledger.settlements:
            raise GitHubLedgerError(
                "GitHub ledger genesis must have no reservations or settlements"
            )
        self._verify_repository_identity()
        blob_sha = self._create_blob(ledger.to_bytes())
        tree_sha = self._create_tree(blob_sha=blob_sha, base_tree=None)
        commit_sha = self._create_commit(
            tree_sha=tree_sha,
            parents=[],
            message=f"initialize campaign {ledger.campaign_id}",
        )
        try:
            ref = self._api.request(
                "POST",
                f"repos/{self._repository}/git/refs",
                {
                    "ref": f"refs/heads/{self._branch}",
                    "sha": commit_sha,
                },
            )
            ref_object = _github_mapping(
                ref.get("object"),
                field="created ref object",
            )
            if (
                ref.get("ref") != f"refs/heads/{self._branch}"
                or ref_object.get("type") != "commit"
                or ref_object.get("sha") != commit_sha
            ):
                raise GitHubLedgerError("created GitHub ledger ref is invalid")
        except GitHubApiError as error:
            if error.status not in {None, 409, 422} and not (
                error.status is not None and 500 <= error.status <= 599
            ):
                raise
        except GitHubLedgerError:
            pass
        self._expected_genesis_commit = commit_sha
        try:
            current = self.read()
            if current.commit_sha != commit_sha or current.ledger != ledger:
                raise GitHubLedgerError("created GitHub ledger failed readback")
        except (GitHubApiError, GitHubLedgerError) as error:
            self._expected_genesis_commit = None
            raise GitHubLedgerError(
                "created GitHub ledger could not be verified"
            ) from error
        return commit_sha

    def read(self) -> LedgerSnapshot:
        if self._expected_genesis_commit is None:
            raise GitHubLedgerError("GitHub ledger genesis commit is not pinned")
        self._verify_repository_identity()
        ref_name = f"refs/heads/{self._branch}"
        ref = self._api.request(
            "GET",
            f"repos/{self._repository}/git/ref/heads/{self._branch}",
        )
        ref_object = _github_mapping(ref.get("object"), field="ref object")
        if ref.get("ref") != ref_name or ref_object.get("type") != "commit":
            raise GitHubLedgerError("GitHub ledger ref is invalid")
        commit_sha = _github_sha(ref_object.get("sha"), field="ref commit")
        head, parents = self._read_commit(commit_sha)
        current = head
        event_count = len(head.ledger.reservations) + len(head.ledger.settlements)
        for depth in range(event_count + 1):
            if current.commit_sha == self._expected_genesis_commit:
                if current.ledger.reservations or current.ledger.settlements or parents:
                    raise GitHubLedgerError("GitHub ledger genesis is invalid")
                return head
            if depth == event_count:
                break
            if len(parents) != 1:
                raise GitHubLedgerError(
                    "GitHub ledger lineage must have exactly one parent"
                )
            parent, parent_parents = self._read_commit(parents[0])
            _require_append_only_transition(parent.ledger, current.ledger)
            current = parent
            parents = parent_parents
        raise GitHubLedgerError("GitHub ledger lineage did not reach pinned genesis")

    def _read_commit(
        self,
        commit_sha: str,
    ) -> tuple[LedgerSnapshot, tuple[str, ...]]:
        commit = self._api.request(
            "GET",
            f"repos/{self._repository}/git/commits/{commit_sha}",
        )
        commit_tree = _github_mapping(commit.get("tree"), field="commit tree")
        commit_parents = commit.get("parents")
        if commit.get("sha") != commit_sha or type(commit_parents) is not list:
            raise GitHubLedgerError("GitHub ledger commit identity is invalid")
        tree_sha = _github_sha(commit_tree.get("sha"), field="commit tree")
        parents = tuple(
            _github_sha(
                _github_mapping(parent, field="commit parent").get("sha"),
                field="commit parent",
            )
            for parent in commit_parents
        )

        tree = self._api.request(
            "GET",
            f"repos/{self._repository}/git/trees/{tree_sha}",
        )
        entries = tree.get("tree")
        if (
            tree.get("sha") != tree_sha
            or tree.get("truncated") is not False
            or type(entries) is not list
            or len(entries) != 1
        ):
            raise GitHubLedgerError("GitHub ledger tree is invalid")
        entry = _github_mapping(entries[0], field="ledger tree entry")
        if (
            entry.get("path") != _LEDGER_PATH
            or entry.get("mode") != "100644"
            or entry.get("type") != "blob"
        ):
            raise GitHubLedgerError("GitHub ledger tree entry is invalid")
        blob_sha = _github_sha(entry.get("sha"), field="ledger blob")

        blob = self._api.request(
            "GET",
            f"repos/{self._repository}/git/blobs/{blob_sha}",
        )
        if blob.get("encoding") != "base64" or type(blob.get("content")) is not str:
            raise GitHubLedgerError("GitHub ledger blob encoding is invalid")
        try:
            encoded = "".join(blob["content"].split()).encode("ascii")
            raw = base64.b64decode(encoded, validate=True)
        except (UnicodeEncodeError, ValueError) as error:
            raise GitHubLedgerError("GitHub ledger blob content is invalid") from error
        actual_blob_sha = _git_blob_sha(raw)
        if blob.get("sha") != blob_sha or actual_blob_sha != blob_sha:
            raise GitHubLedgerError("GitHub ledger blob identity is invalid")
        try:
            ledger = CampaignLedger.from_bytes(raw)
        except CampaignLedgerError as error:
            raise GitHubLedgerError("GitHub ledger content is invalid") from error
        return (
            LedgerSnapshot(
                commit_sha=commit_sha,
                tree_sha=tree_sha,
                ledger=ledger,
            ),
            parents,
        )

    def compare_and_swap(
        self,
        expected: LedgerSnapshot,
        updated: CampaignLedger,
    ) -> str:
        if expected.tree_sha is None:
            raise GitHubLedgerError("GitHub ledger snapshot is missing its tree")
        _require_append_only_transition(expected.ledger, updated)
        nonce = _require_pattern(
            self._nonce_factory(),
            field="CAS nonce",
            pattern=_NONCE_PATTERN,
        )
        current = self.read()
        if current != expected:
            raise CasConflict("GitHub ledger ref advanced before reservation")
        reservation_appended = len(updated.reservations) > len(
            expected.ledger.reservations
        )
        event_id = (
            updated.reservations[-1].reservation_id
            if reservation_appended
            else updated.settlements[-1].settlement_id
        )
        raw = updated.to_bytes()
        blob_sha = self._create_blob(raw)
        tree_sha = self._create_tree(
            blob_sha=blob_sha,
            base_tree=expected.tree_sha,
        )
        commit_sha = self._create_commit(
            tree_sha=tree_sha,
            parents=[expected.commit_sha],
            message=(
                f"reserve {event_id} {nonce}"
                if reservation_appended
                else f"settle {event_id} {nonce}"
            ),
        )
        try:
            ref = self._api.request(
                "PATCH",
                f"repos/{self._repository}/git/refs/heads/{self._branch}",
                {"force": False, "sha": commit_sha},
            )
        except GitHubApiError as error:
            return self._classify_uncertain_update(
                expected=expected,
                updated=updated,
                commit_sha=commit_sha,
                error=error,
            )
        ref_object = _github_mapping(ref.get("object"), field="updated ref object")
        if (
            ref.get("ref") != f"refs/heads/{self._branch}"
            or ref_object.get("type") != "commit"
            or ref_object.get("sha") != commit_sha
        ):
            raise GitHubLedgerError("GitHub ledger ref update response is invalid")
        current = self.read()
        if not _ledger_is_prefix(updated, current.ledger):
            raise GitHubLedgerError("GitHub ledger ref readback lost the reservation")
        return commit_sha

    def _verify_repository_identity(self) -> None:
        repository = self._api.request("GET", f"repos/{self._repository}")
        if (
            type(repository.get("id")) is not int
            or repository.get("id") != self._expected_repository_id
        ):
            raise GitHubLedgerError("GitHub repository identity does not match")

    def _create_blob(self, raw: bytes) -> str:
        blob = self._api.request(
            "POST",
            f"repos/{self._repository}/git/blobs",
            {
                "content": base64.b64encode(raw).decode("ascii"),
                "encoding": "base64",
            },
        )
        blob_sha = _github_sha(blob.get("sha"), field="created blob")
        if blob_sha != _git_blob_sha(raw):
            raise GitHubLedgerError("created GitHub blob identity is invalid")
        return blob_sha

    def _create_tree(self, *, blob_sha: str, base_tree: str | None) -> str:
        payload: dict[str, object] = {
            "tree": [
                {
                    "mode": "100644",
                    "path": _LEDGER_PATH,
                    "sha": blob_sha,
                    "type": "blob",
                }
            ]
        }
        if base_tree is not None:
            payload["base_tree"] = base_tree
        tree = self._api.request(
            "POST",
            f"repos/{self._repository}/git/trees",
            payload,
        )
        return _github_sha(tree.get("sha"), field="created tree")

    def _create_commit(
        self,
        *,
        tree_sha: str,
        parents: list[str],
        message: str,
    ) -> str:
        commit = self._api.request(
            "POST",
            f"repos/{self._repository}/git/commits",
            {
                "author": _GIT_IDENTITY,
                "committer": _GIT_IDENTITY,
                "message": message,
                "parents": parents,
                "tree": tree_sha,
            },
        )
        return _github_sha(commit.get("sha"), field="created commit")

    def _classify_uncertain_update(
        self,
        *,
        expected: LedgerSnapshot,
        updated: CampaignLedger,
        commit_sha: str,
        error: GitHubApiError,
    ) -> str:
        try:
            current = self.read()
        except (GitHubApiError, GitHubLedgerError) as read_error:
            raise GitHubLedgerError("GitHub ledger ref update could not be verified") from read_error
        if (
            (
                error.status is None
                or (error.status is not None and 500 <= error.status <= 599)
            )
            and current.commit_sha == commit_sha
            and current.ledger == updated
        ):
            return commit_sha
        if current.commit_sha != expected.commit_sha:
            raise CasConflict("GitHub ledger ref advanced during reservation") from error
        raise GitHubLedgerError("GitHub ledger ref update was rejected") from error


def _github_mapping(value: object, *, field: str) -> dict[str, object]:
    if type(value) is not dict:
        raise GitHubLedgerError(f"GitHub {field} is invalid")
    return value


def _github_sha(value: object, *, field: str) -> str:
    if type(value) is not str or _SHA1_PATTERN.fullmatch(value) is None:
        raise GitHubLedgerError(f"GitHub {field} SHA is invalid")
    return value


def _git_blob_sha(raw: bytes) -> str:
    header = f"blob {len(raw)}\0".encode("ascii")
    return hashlib.sha1(header + raw, usedforsecurity=False).hexdigest()


def _require_append_only_transition(
    previous: CampaignLedger,
    updated: CampaignLedger,
) -> None:
    if (
        previous.campaign_id != updated.campaign_id
        or previous.currency != updated.currency
        or previous.budget_micro_cny != updated.budget_micro_cny
        or previous.opening_reserved_upper_micro_cny
        != updated.opening_reserved_upper_micro_cny
        or updated.reservations[: len(previous.reservations)]
        != previous.reservations
        or updated.settlements[: len(previous.settlements)] != previous.settlements
    ):
        raise GitHubLedgerError(
            "GitHub ledger transition must preserve append-only campaign history"
        )
    reservation_delta = len(updated.reservations) - len(previous.reservations)
    settlement_delta = len(updated.settlements) - len(previous.settlements)
    if reservation_delta + settlement_delta != 1 or min(
        reservation_delta, settlement_delta
    ) < 0:
        raise GitHubLedgerError("GitHub ledger transition must append one event")
    if updated.schema_version != previous.schema_version and not (
        previous.schema_version == 2
        and updated.schema_version == 3
        and settlement_delta == 1
    ):
        raise GitHubLedgerError("GitHub ledger schema transition is invalid")


def _ledger_is_prefix(expected: CampaignLedger, current: CampaignLedger) -> bool:
    return (
        expected.campaign_id == current.campaign_id
        and expected.currency == current.currency
        and expected.budget_micro_cny == current.budget_micro_cny
        and expected.opening_reserved_upper_micro_cny
        == current.opening_reserved_upper_micro_cny
        and current.reservations[: len(expected.reservations)]
        == expected.reservations
        and current.settlements[: len(expected.settlements)] == expected.settlements
        and (
            expected.schema_version == current.schema_version
            or (expected.schema_version == 2 and current.schema_version == 3)
        )
    )


def reserve_with_store(
    store: LedgerStore,
    *,
    expected_campaign_id: str,
    request: ReservationRequest,
    max_attempts: int = 5,
) -> ReservationReceipt:
    if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
        raise CampaignLedgerError("max_attempts must be between 1 and 10")
    for attempt in range(max_attempts):
        snapshot = store.read()
        if snapshot.ledger.campaign_id != expected_campaign_id:
            raise CampaignLedgerError("campaign identity does not match")
        before = snapshot.ledger.reserved_upper_micro_cny
        result = snapshot.ledger.reserve(request)
        if not result.created:
            return ReservationReceipt(
                authorized=False,
                campaign_id=snapshot.ledger.campaign_id,
                reservation=result.reservation,
                ledger_commit_sha=snapshot.commit_sha,
                budget_micro_cny=snapshot.ledger.budget_micro_cny,
                upper_micro_cny=result.reservation.upper_micro_cny,
                reserved_before_micro_cny=_reserved_before(
                    snapshot.ledger,
                    result.reservation,
                ),
                reserved_after_micro_cny=(
                    _reserved_before(snapshot.ledger, result.reservation)
                    + result.reservation.upper_micro_cny
                ),
            )
        try:
            commit_sha = store.compare_and_swap(snapshot, result.ledger)
        except CasConflict as error:
            if attempt + 1 == max_attempts:
                raise CasConflict("campaign ledger CAS retry limit was exhausted") from error
            continue
        return ReservationReceipt(
            authorized=True,
            campaign_id=snapshot.ledger.campaign_id,
            reservation=result.reservation,
            ledger_commit_sha=commit_sha,
            budget_micro_cny=snapshot.ledger.budget_micro_cny,
            upper_micro_cny=result.reservation.upper_micro_cny,
            reserved_before_micro_cny=before,
            reserved_after_micro_cny=result.ledger.reserved_upper_micro_cny,
        )
    raise AssertionError("unreachable campaign ledger CAS state")


def settle_with_store(
    store: LedgerStore,
    *,
    expected_campaign_id: str,
    request: SettlementRequest,
    max_attempts: int = 5,
) -> SettlementReceipt:
    if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
        raise CampaignLedgerError("max_attempts must be between 1 and 10")
    for attempt in range(max_attempts):
        snapshot = store.read()
        if snapshot.ledger.campaign_id != expected_campaign_id:
            raise CampaignLedgerError("campaign identity does not match")
        before = snapshot.ledger.reserved_upper_micro_cny
        result = snapshot.ledger.settle(request)
        if not result.created:
            return SettlementReceipt(
                created=False,
                campaign_id=snapshot.ledger.campaign_id,
                settlement=result.settlement,
                ledger_commit_sha=snapshot.commit_sha,
                reserved_before_micro_cny=before,
                reserved_after_micro_cny=before,
            )
        try:
            commit_sha = store.compare_and_swap(snapshot, result.ledger)
        except CasConflict as error:
            if attempt + 1 == max_attempts:
                raise CasConflict("campaign ledger CAS retry limit was exhausted") from error
            continue
        return SettlementReceipt(
            created=True,
            campaign_id=snapshot.ledger.campaign_id,
            settlement=result.settlement,
            ledger_commit_sha=commit_sha,
            reserved_before_micro_cny=before,
            reserved_after_micro_cny=result.ledger.reserved_upper_micro_cny,
        )
    raise AssertionError("unreachable campaign ledger CAS state")


def _reserved_before(ledger: CampaignLedger, reservation: Reservation) -> int:
    return ledger.reserved_before(reservation)


def main(
    argv: list[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="campaign-ledger")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create-genesis")
    create.add_argument("--campaign-id", required=True)
    create.add_argument("--budget-cny", required=True)
    create.add_argument("--opening-reserved-upper-cny", required=True)
    create.add_argument("--output", required=True, type=Path)

    verify = commands.add_parser("verify")
    verify.add_argument("--ledger", required=True, type=Path)

    arguments = parser.parse_args(argv)
    if arguments.command == "create-genesis":
        ledger = CampaignLedger.create(
            campaign_id=arguments.campaign_id,
            budget_cny=arguments.budget_cny,
            opening_reserved_upper_cny=arguments.opening_reserved_upper_cny,
        )
        with arguments.output.open("xb") as output:
            output.write(ledger.to_bytes())
        return 0
    if arguments.command == "verify":
        CampaignLedger.from_bytes(arguments.ledger.read_bytes())
        return 0
    raise AssertionError("unreachable campaign ledger command")


if __name__ == "__main__":
    raise SystemExit(main())
