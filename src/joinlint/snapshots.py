from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import sys
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from time import monotonic
from typing import TypeAlias
from urllib.parse import quote

if sys.platform == "darwin":
    import fcntl

from joinlint.config import SourceConfig, SourceKind, _project_boundary, load_config
from joinlint.errors import JoinLintError
from joinlint.paths import SafeProject


_COPY_CHUNK_SIZE = 64 * 1024
_SOURCE_FINGERPRINT_PREFIX = b"joinlint-source-v1"
after_copy_hook: Callable[[], None] | None = None

Identity: TypeAlias = tuple[PurePosixPath, int, int, int, int]


@dataclass(frozen=True)
class SnapshotFile:
    relative_path: PurePosixPath
    path: Path
    size: int
    sha256: str


@dataclass
class SourceSnapshot:
    source_id: str
    kind: SourceKind
    root: Path
    files: tuple[SnapshotFile, ...]
    fingerprint: str
    _temporary_directory: tempfile.TemporaryDirectory[str] = field(repr=False)

    def __enter__(self) -> SourceSnapshot:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def close(self) -> None:
        self._temporary_directory.cleanup()


class Deadline:
    def __init__(self, source_id: str, seconds: int) -> None:
        self._source_id = source_id
        self._expires_at = monotonic() + seconds

    def check(self) -> None:
        if monotonic() > self._expires_at:
            raise JoinLintError("INCONCLUSIVE_SCAN", f"scan time limit exceeded for {self._source_id}", 3)


def fingerprint(entries: Iterable[SnapshotFile | tuple[str, int, str]]) -> str:
    normalized: list[tuple[str, int, str]] = []
    for entry in entries:
        if isinstance(entry, SnapshotFile):
            normalized.append((entry.relative_path.as_posix(), entry.size, entry.sha256))
        else:
            normalized.append(entry)

    digest = hashlib.sha256()
    digest.update(_SOURCE_FINGERPRINT_PREFIX)
    for relative_path, size, content_digest in sorted(normalized, key=lambda item: item[0].encode("utf-8")):
        path_bytes = relative_path.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(size.to_bytes(8, "big"))
        digest.update(bytes.fromhex(content_digest))
    return digest.hexdigest()


def snapshot_source(project: Path | SafeProject, source_id: str) -> SourceSnapshot:
    with _project_boundary(project) as boundary:
        config = load_config(boundary)
        source = config.sources.get(source_id)
        if source is None:
            raise JoinLintError("SOURCE_NOT_FOUND", "source ID does not exist", 2)
        deadline = Deadline(source_id, source.limits.max_scan_seconds)
        if source.kind == "csv_directory":
            return _snapshot_csv_directory(boundary, source_id, source, deadline)
        return _snapshot_sqlite(boundary, source_id, source, deadline)


def _snapshot_csv_directory(
    project: SafeProject, source_id: str, source: SourceConfig, deadline: Deadline
) -> SourceSnapshot:
    temporary_directory = tempfile.TemporaryDirectory(prefix="joinlint-csv-")
    root = Path(temporary_directory.name)
    try:
        original_identities = _walk_csv_directory(project, source.path, deadline)
        files = _copy_csv_files(project, source, original_identities, root, deadline)
        if after_copy_hook is not None:
            after_copy_hook()
        current_identities = _walk_csv_directory(project, source.path, deadline)
        if current_identities != original_identities:
            raise JoinLintError("SOURCE_CHANGED_DURING_SCAN", "source changed during snapshot", 3)
        return SourceSnapshot(
            source_id=source_id,
            kind=source.kind,
            root=root,
            files=tuple(files),
            fingerprint=fingerprint(files),
            _temporary_directory=temporary_directory,
        )
    except BaseException:
        temporary_directory.cleanup()
        raise


def _walk_csv_directory(
    project: SafeProject, source_path: PurePosixPath, deadline: Deadline
) -> tuple[Identity, ...]:
    directory_descriptor = project.open_relative(source_path, os.O_RDONLY | os.O_DIRECTORY)
    identities: list[Identity] = []
    try:
        _walk_csv_directory_descriptor(directory_descriptor, PurePosixPath(), identities, deadline)
    except OSError as exc:
        raise JoinLintError("SOURCE_CHANGED_DURING_SCAN", "CSV directory changed during scan", 3) from exc
    finally:
        os.close(directory_descriptor)
    return tuple(sorted(identities, key=lambda item: item[0].as_posix().encode("utf-8")))


def _walk_csv_directory_descriptor(
    directory_descriptor: int,
    relative_directory: PurePosixPath,
    identities: list[Identity],
    deadline: Deadline,
) -> None:
    deadline.check()
    for name in sorted(os.listdir(directory_descriptor), key=os.fsencode):
        deadline.check()
        entry_stat = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        relative_path = relative_directory / name
        if stat.S_ISLNK(entry_stat.st_mode):
            raise JoinLintError("SYMLINK_NOT_ALLOWED", "symlinks are not allowed", 2)
        if stat.S_ISDIR(entry_stat.st_mode):
            child_descriptor = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory_descriptor,
            )
            try:
                _walk_csv_directory_descriptor(child_descriptor, relative_path, identities, deadline)
            finally:
                os.close(child_descriptor)
            continue
        if not stat.S_ISREG(entry_stat.st_mode):
            raise JoinLintError("UNSUPPORTED_SOURCE", "CSV source contains an unsupported object", 2)
        if not name.endswith(".csv"):
            raise JoinLintError("UNSUPPORTED_SOURCE", "CSV source contains an unsupported file", 2)
        identities.append(
            (
                relative_path,
                entry_stat.st_dev,
                entry_stat.st_ino,
                entry_stat.st_size,
                entry_stat.st_mtime_ns,
            )
        )


def _copy_csv_files(
    project: SafeProject,
    source: SourceConfig,
    identities: tuple[Identity, ...],
    root: Path,
    deadline: Deadline,
) -> list[SnapshotFile]:
    copied_files: list[SnapshotFile] = []
    source_bytes = 0
    for relative_path, device, inode, expected_size, expected_mtime_ns in identities:
        deadline.check()
        source_descriptor = project.open_relative(source.path / relative_path, os.O_RDONLY)
        try:
            opened_stat = os.fstat(source_descriptor)
            if (opened_stat.st_dev, opened_stat.st_ino, opened_stat.st_size, opened_stat.st_mtime_ns) != (
                device,
                inode,
                expected_size,
                expected_mtime_ns,
            ):
                raise JoinLintError("SOURCE_CHANGED_DURING_SCAN", "source changed before copy", 3)
            destination_path = root / relative_path
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            content_hash = hashlib.sha256()
            copied_size = 0
            with destination_path.open("wb") as destination:
                while True:
                    deadline.check()
                    chunk = os.read(source_descriptor, _COPY_CHUNK_SIZE)
                    deadline.check()
                    if not chunk:
                        break
                    copied_size += len(chunk)
                    source_bytes += len(chunk)
                    if source_bytes > source.limits.max_source_bytes:
                        raise JoinLintError("INCONCLUSIVE_SCAN", "source byte limit exceeded", 3)
                    content_hash.update(chunk)
                    destination.write(chunk)
            if copied_size != expected_size:
                raise JoinLintError("SOURCE_CHANGED_DURING_SCAN", "source changed during copy", 3)
            copied_files.append(
                SnapshotFile(
                    relative_path=relative_path,
                    path=destination_path,
                    size=copied_size,
                    sha256=content_hash.hexdigest(),
                )
            )
        finally:
            os.close(source_descriptor)
    return copied_files


def _snapshot_sqlite(
    project: SafeProject, source_id: str, source: SourceConfig, deadline: Deadline
) -> SourceSnapshot:
    temporary_directory = tempfile.TemporaryDirectory(prefix="joinlint-sqlite-")
    root = Path(temporary_directory.name)
    parent_descriptor = -1
    try:
        deadline.check()
        parent_descriptor, leaf, expected_identity = project.open_parent_relative(source.path)
        if expected_identity.st_size > source.limits.max_source_bytes:
            raise JoinLintError("INCONCLUSIVE_SCAN", "source byte limit exceeded", 3)
        destination = root / "source.sqlite"
        _backup_sqlite(parent_descriptor, leaf, destination, deadline)
        deadline.check()
        actual_identity = os.stat(leaf, dir_fd=parent_descriptor, follow_symlinks=False)
        if _identity_tuple(actual_identity) != _identity_tuple(expected_identity):
            raise JoinLintError("SOURCE_CHANGED_DURING_SCAN", "source changed during backup", 3)
        copied_size, copied_hash = _hash_file(destination, deadline)
        if copied_size > source.limits.max_source_bytes:
            raise JoinLintError("INCONCLUSIVE_SCAN", "source byte limit exceeded", 3)
        files = (
            SnapshotFile(
                relative_path=source.path,
                path=destination,
                size=copied_size,
                sha256=copied_hash,
            ),
        )
        return SourceSnapshot(
            source_id=source_id,
            kind=source.kind,
            root=root,
            files=files,
            fingerprint=fingerprint(files),
            _temporary_directory=temporary_directory,
        )
    except BaseException:
        temporary_directory.cleanup()
        raise
    finally:
        if parent_descriptor != -1:
            os.close(parent_descriptor)


def _backup_sqlite(parent_descriptor: int, leaf: str, destination: Path, deadline: Deadline) -> None:
    uri = _sqlite_uri(parent_descriptor, leaf)
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        deadline.check()
        source_connection = sqlite3.connect(uri, uri=True)
        source_connection.execute("PRAGMA query_only = ON")
        source_connection.execute("BEGIN")
        destination_connection = sqlite3.connect(destination)
        source_connection.backup(
            destination_connection,
            pages=128,
            progress=lambda _status, _remaining, _total: deadline.check(),
        )
    except JoinLintError:
        raise
    except sqlite3.Error as exc:
        raise JoinLintError("INCONCLUSIVE_SCAN", "SQLite backup could not complete", 3) from exc
    finally:
        if source_connection is not None:
            source_connection.close()
        if destination_connection is not None:
            destination_connection.close()


def _sqlite_uri(parent_descriptor: int, leaf: str) -> str:
    if sys.platform == "darwin":
        raw_path = fcntl.fcntl(parent_descriptor, fcntl.F_GETPATH, b"\0" * 1024)
        parent_path = Path(raw_path.split(b"\0", 1)[0].decode("utf-8"))
        descriptor_identity = os.fstat(parent_descriptor)
        path_identity = parent_path.stat(follow_symlinks=False)
        if _identity_tuple(descriptor_identity) != _identity_tuple(path_identity):
            raise JoinLintError("SOURCE_CHANGED_DURING_SCAN", "SQLite parent directory changed", 3)
        return f"file://{quote(parent_path.as_posix(), safe='/')}/{quote(leaf, safe='')}?mode=ro"
    return f"file:///proc/self/fd/{parent_descriptor}/{quote(leaf, safe='')}?mode=ro"


def _identity_tuple(entry_stat: os.stat_result) -> tuple[int, int, int, int]:
    return entry_stat.st_dev, entry_stat.st_ino, entry_stat.st_size, entry_stat.st_mtime_ns


def _hash_file(path: Path, deadline: Deadline) -> tuple[int, str]:
    content_hash = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while True:
            deadline.check()
            chunk = source.read(_COPY_CHUNK_SIZE)
            deadline.check()
            if not chunk:
                break
            size += len(chunk)
            content_hash.update(chunk)
    return size, content_hash.hexdigest()
