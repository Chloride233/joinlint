from __future__ import annotations

import fcntl
import os
import secrets
import stat
import hashlib
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TypeVar

from pydantic import BaseModel, ValidationError

from joinlint.contracts import canonical_json
from joinlint.runtime.domain import EvidenceRecord, JoinProof


ModelT = TypeVar("ModelT", bound=BaseModel)


class RuntimeCache:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or _default_cache_root()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        for name in ("evidence", "evidence-index", "proofs", "locks"):
            directory = self.root / name
            directory.mkdir(exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)

    def load_evidence(self, evidence_id: str) -> EvidenceRecord | None:
        return self._load("evidence", evidence_id, EvidenceRecord)

    def store_evidence(self, record: EvidenceRecord) -> None:
        self._store("evidence", record.evidence_id, record)
        selector = self.evidence_selector(
            record.relationship_id,
            record.snapshot_id,
            record.provenance,
            record.verifier_version,
        )
        self._store_pointer("evidence-index", selector, record.evidence_id)

    def load_evidence_for(
        self,
        relationship_id: str,
        snapshot_id: str,
        provenance: str,
        verifier_version: str,
    ) -> EvidenceRecord | None:
        selector = self.evidence_selector(
            relationship_id,
            snapshot_id,
            provenance,
            verifier_version,
        )
        evidence_id = self._load_pointer("evidence-index", selector)
        if evidence_id is None:
            return None
        record = self.load_evidence(evidence_id)
        if record is None:
            (self.root / "evidence-index" / f"{selector}.json").unlink(missing_ok=True)
            return None
        if (
            record.relationship_id != relationship_id
            or record.snapshot_id != snapshot_id
            or record.provenance != provenance
            or record.verifier_version != verifier_version
        ):
            (self.root / "evidence-index" / f"{selector}.json").unlink(missing_ok=True)
            return None
        return record

    def load_proof(self, plan_id: str) -> JoinProof | None:
        return self._load("proofs", plan_id, JoinProof)

    def store_proof(self, proof: JoinProof) -> None:
        self._store("proofs", proof.plan_id, proof)

    @contextmanager
    def locked(self, key: str) -> Iterator[None]:
        path = self.root / "locks" / f"{_safe_digest(key)}.lock"
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _load(self, namespace: str, key: str, model: type[ModelT]) -> ModelT | None:
        path = self.root / namespace / f"{_safe_digest(key)}.json"
        try:
            if stat.S_ISLNK(path.lstat().st_mode):
                path.unlink()
                return None
            value = model.model_validate_json(path.read_bytes())
        except FileNotFoundError:
            return None
        except (OSError, ValueError, ValidationError):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return None
        identity = getattr(value, "evidence_id", getattr(value, "plan_id", None))
        if identity != key:
            path.unlink(missing_ok=True)
            return None
        return value

    def _store(self, namespace: str, key: str, value: BaseModel) -> None:
        path = self.root / namespace / f"{_safe_digest(key)}.json"
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        body = canonical_json(value.model_dump(mode="json"))
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as destination:
                destination.write(body)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary, path)
            directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            temporary.unlink(missing_ok=True)

    def _load_pointer(self, namespace: str, key: str) -> str | None:
        path = self.root / namespace / f"{_safe_digest(key)}.json"
        try:
            if stat.S_ISLNK(path.lstat().st_mode):
                path.unlink()
                return None
            value = path.read_text(encoding="ascii")
            return _safe_digest(value)
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, ValueError):
            path.unlink(missing_ok=True)
            return None

    def _store_pointer(self, namespace: str, key: str, value: str) -> None:
        _safe_digest(value)
        path = self.root / namespace / f"{_safe_digest(key)}.json"
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as destination:
                destination.write(value)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def evidence_selector(
        relationship_id: str,
        snapshot_id: str,
        provenance: str,
        verifier_version: str,
    ) -> str:
        return hashlib.sha256(
            canonical_json(
                {
                    "relationship_id": _safe_digest(relationship_id),
                    "snapshot_id": _safe_digest(snapshot_id),
                    "provenance": provenance,
                    "verifier_version": verifier_version,
                }
            )
        ).hexdigest()


def _safe_digest(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("cache key must be a lowercase SHA-256 digest")
    return value


def _default_cache_root() -> Path:
    configured = os.environ.get("XDG_CACHE_HOME")
    if configured:
        return Path(configured) / "joinlint" / "v2"
    if os.uname().sysname == "Darwin":
        return Path.home() / "Library" / "Caches" / "joinlint" / "v2"
    return Path.home() / ".cache" / "joinlint" / "v2"
