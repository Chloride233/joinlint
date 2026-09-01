from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

from joinlint.config import Limits, SourceConfig
from joinlint.contracts import canonical_json
from joinlint.errors import JoinLintError
from joinlint.paths import SafeProject
from joinlint.runtime.domain import (
    ColumnDefinition,
    Endpoint,
    EntityDefinition,
    RelationshipDefinition,
    RelationshipSeed,
    SourceCatalog,
    SourceIdentity,
    SourceSnapshot,
    relationship_id_for,
)
from joinlint.snapshots import Deadline, SourceSnapshot as LegacySnapshot
from joinlint.snapshots import _identity_tuple, _snapshot_sqlite, _sqlite_uri


_CONVENTIONAL_DIRECTORIES = ("data", "datasets")
_IGNORED_DIRECTORIES = {
    ".git",
    ".joinlint",
    ".venv",
    "venv",
    "node_modules",
    "vendor",
    "__pycache__",
}


@dataclass
class SQLiteSnapshot:
    identity: SourceIdentity
    document: SourceSnapshot
    path: Path
    _legacy: LegacySnapshot

    def __enter__(self) -> SQLiteSnapshot:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def close(self) -> None:
        self._legacy.close()


@dataclass
class SQLiteSourceMonitor:
    root: Path
    identity: SourceIdentity
    connection: sqlite3.Connection
    parent_descriptor: int
    file_identity: tuple[int, int, int, int]
    data_version: int
    _lock: threading.RLock

    @classmethod
    def open(cls, root: Path, identity: SourceIdentity) -> SQLiteSourceMonitor:
        parent_descriptor = -1
        connection: sqlite3.Connection | None = None
        try:
            with SafeProject(root) as boundary:
                parent_descriptor, leaf, entry_stat = boundary.open_parent_relative(
                    PurePosixPath(identity.relative_locator)
                )
                uri = _sqlite_uri(parent_descriptor, leaf)
                connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
            connection.execute("PRAGMA query_only = ON")
            data_version = _data_version(connection)
            return cls(
                root=root,
                identity=identity,
                connection=connection,
                parent_descriptor=parent_descriptor,
                file_identity=_identity_tuple(entry_stat),
                data_version=data_version,
                _lock=threading.RLock(),
            )
        except BaseException:
            if connection is not None:
                connection.close()
            if parent_descriptor != -1:
                os.close(parent_descriptor)
            raise

    def is_current(self) -> bool:
        with self._lock:
            try:
                with SafeProject(self.root) as boundary:
                    descriptor, _leaf, entry_stat = boundary.open_parent_relative(
                        PurePosixPath(self.identity.relative_locator)
                    )
                    os.close(descriptor)
                return (
                    _identity_tuple(entry_stat) == self.file_identity
                    and _data_version(self.connection) == self.data_version
                )
            except (OSError, sqlite3.Error, JoinLintError):
                return False

    def close(self) -> None:
        with self._lock:
            try:
                self.connection.close()
            finally:
                if self.parent_descriptor != -1:
                    os.close(self.parent_descriptor)
                    self.parent_descriptor = -1


def _data_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA data_version").fetchone()
    if row is None:
        raise sqlite3.OperationalError("SQLite data_version is unavailable")
    return int(row[0])


def locate_sqlite_sources(
    root: Path,
    explicit_sources: Iterable[str] = (),
    *,
    auto: bool = True,
) -> tuple[SourceIdentity, ...]:
    with SafeProject(root) as boundary:
        boundary_digest = hashlib.sha256(boundary.root.as_posix().encode("utf-8")).hexdigest()
        explicit = tuple(explicit_sources)
        locators = (
            tuple(_validate_explicit(boundary, value) for value in explicit)
            if explicit
            else _discover(boundary)
            if auto
            else ()
        )
    if not locators:
        raise JoinLintError("SOURCE_NOT_FOUND", "no supported SQLite source was found", 3)
    identities = tuple(
        _source_identity(boundary_digest, locator)
        for locator in sorted(set(locators), key=lambda value: value.as_posix().encode("utf-8"))
    )
    return identities


def snapshot_sqlite(root: Path, identity: SourceIdentity, *, limits: Limits | None = None) -> SQLiteSnapshot:
    source = SourceConfig(
        kind="sqlite",
        path=identity.relative_locator,
        limits=limits or Limits(),
    )
    with SafeProject(root) as boundary:
        legacy = _snapshot_sqlite(
            boundary,
            identity.source_id,
            source,
            Deadline(identity.source_id, source.limits.max_scan_seconds),
        )
    try:
        path = legacy.files[0].path
        schema_digest = _schema_digest(path)
        content_digest = legacy.fingerprint
        snapshot_id = hashlib.sha256(
            canonical_json(
                {
                    "domain": "joinlint-sqlite-snapshot-v2",
                    "source_id": identity.source_id,
                    "schema_digest": schema_digest,
                    "content_digest": content_digest,
                }
            )
        ).hexdigest()
        return SQLiteSnapshot(
            identity=identity,
            document=SourceSnapshot(
                source_id=identity.source_id,
                snapshot_id=snapshot_id,
                schema_digest=schema_digest,
                content_digest=content_digest,
            ),
            path=path,
            _legacy=legacy,
        )
    except BaseException:
        legacy.close()
        raise


def extract_sqlite_catalog(snapshot: SQLiteSnapshot) -> SourceCatalog:
    connection = sqlite3.connect(f"file:{snapshot.path}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    try:
        table_names = tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        )
        entities = tuple(_entity(connection, snapshot.identity.source_id, name) for name in table_names)
        by_table = {entity.physical_name: entity for entity in entities}
        seeds: list[RelationshipSeed] = []
        for table_name in table_names:
            seeds.extend(_foreign_keys(connection, snapshot.identity.source_id, table_name, by_table))
        seeds_by_id = {
            seed.definition.relationship_id: seed
            for seed in seeds
        }
        seeds = sorted(
            seeds_by_id.values(),
            key=lambda seed: seed.definition.relationship_id.encode("utf-8"),
        )
        return SourceCatalog(
            source_id=snapshot.identity.source_id,
            snapshot_id=snapshot.document.snapshot_id,
            entities=entities,
            declared_relationships=tuple(seeds),
        )
    finally:
        connection.close()


def _validate_explicit(boundary: SafeProject, value: str) -> PurePosixPath:
    try:
        source = SourceConfig(kind="sqlite", path=value)
    except ValueError as error:
        raise JoinLintError("INVALID_ARGUMENT", "SQLite source path is invalid", 2) from error
    if source.path.suffix.lower() not in {".sqlite", ".db", ".sqlite3"}:
        raise JoinLintError("UNSUPPORTED_SOURCE", "source must be a SQLite file", 2)
    descriptor = boundary.open_relative(source.path, os.O_RDONLY)
    os.close(descriptor)
    return source.path


def _discover(boundary: SafeProject) -> tuple[PurePosixPath, ...]:
    found: list[PurePosixPath] = []
    for path in _directory_entries(boundary.root):
        if _is_sqlite(path):
            found.append(PurePosixPath(path.name))
    for directory_name in _CONVENTIONAL_DIRECTORIES:
        directory = boundary.root / directory_name
        if not directory.exists():
            continue
        if directory.is_symlink() or not directory.is_dir():
            raise JoinLintError("SYMLINK_NOT_ALLOWED", "data discovery boundary is unsafe", 2)
        _walk_discovery(boundary.root, directory, depth=0, found=found)
    for locator in found:
        descriptor = boundary.open_relative(locator, os.O_RDONLY)
        os.close(descriptor)
    return tuple(found)


def _walk_discovery(
    root: Path,
    directory: Path,
    *,
    depth: int,
    found: list[PurePosixPath],
) -> None:
    if depth > 2:
        return
    for path in _directory_entries(directory):
        entry_stat = path.lstat()
        if stat.S_ISLNK(entry_stat.st_mode):
            raise JoinLintError("SYMLINK_NOT_ALLOWED", "symlinks are not allowed in data discovery", 2)
        if stat.S_ISDIR(entry_stat.st_mode):
            if depth < 2 and not path.name.startswith(".") and path.name not in _IGNORED_DIRECTORIES:
                _walk_discovery(root, path, depth=depth + 1, found=found)
            continue
        if _is_sqlite(path):
            found.append(PurePosixPath(path.relative_to(root).as_posix()))


def _directory_entries(directory: Path) -> tuple[Path, ...]:
    try:
        return tuple(sorted(directory.iterdir(), key=lambda path: os.fsencode(path.name)))
    except OSError as error:
        raise JoinLintError("SOURCE_DISCOVERY_FAILED", "source directory could not be read", 3) from error


def _is_sqlite(path: Path) -> bool:
    try:
        entry_stat = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(entry_stat.st_mode) and path.suffix.lower() in {".sqlite", ".db", ".sqlite3"}


def _source_identity(boundary_digest: str, locator: PurePosixPath) -> SourceIdentity:
    digest = hashlib.sha256(
        canonical_json(
            {
                "domain": "joinlint-source-identity-v2",
                "boundary_digest": boundary_digest,
                "kind": "sqlite",
                "relative_locator": locator.as_posix(),
            }
        )
    ).hexdigest()
    return SourceIdentity(
        source_id=f"source_{digest[:24]}",
        boundary_digest=boundary_digest,
        relative_locator=locator.as_posix(),
    )


def _schema_digest(path: Path) -> str:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT type, name, tbl_name, COALESCE(sql, '') FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name, tbl_name"
        ).fetchall()
        return hashlib.sha256(canonical_json(rows)).hexdigest()
    finally:
        connection.close()


def _entity(connection: sqlite3.Connection, source_id: str, table_name: str) -> EntityDefinition:
    table_info = connection.execute(f"PRAGMA table_info({_quote(table_name)})").fetchall()
    unique_keys = _unique_keys(connection, table_name, table_info)
    columns = tuple(
        ColumnDefinition(
            name=str(row[1]),
            physical_type=str(row[2] or "unknown").lower(),
            nullable=not bool(row[3]) and int(row[5]) == 0,
            primary_key_position=int(row[5]),
            unique=(str(row[1]),) in unique_keys,
        )
        for row in table_info
    )
    primary_key = tuple(
        str(row[1]) for row in sorted(table_info, key=lambda row: int(row[5])) if int(row[5]) > 0
    )
    return EntityDefinition(
        entity_id=_entity_id(source_id, table_name),
        source_id=source_id,
        physical_name=table_name,
        columns=columns,
        primary_key=primary_key,
        unique_keys=unique_keys,
    )


def _unique_keys(
    connection: sqlite3.Connection,
    table_name: str,
    table_info: list[tuple[object, ...]],
) -> tuple[tuple[str, ...], ...]:
    keys: set[tuple[str, ...]] = set()
    primary = tuple(
        str(row[1]) for row in sorted(table_info, key=lambda row: int(row[5])) if int(row[5]) > 0
    )
    if primary:
        keys.add(primary)
    for row in connection.execute(f"PRAGMA index_list({_quote(table_name)})"):
        if not bool(row[2]) or bool(row[4]):
            continue
        columns = tuple(
            str(item[2])
            for item in sorted(
                connection.execute(f"PRAGMA index_info({_quote(str(row[1]))})").fetchall(),
                key=lambda item: int(item[0]),
            )
        )
        if columns:
            keys.add(columns)
    return tuple(sorted(keys, key=lambda key: tuple(value.encode("utf-8") for value in key)))


def _foreign_keys(
    connection: sqlite3.Connection,
    source_id: str,
    table_name: str,
    entities: dict[str, EntityDefinition],
) -> list[RelationshipSeed]:
    grouped: dict[int, list[tuple[object, ...]]] = {}
    for row in connection.execute(f"PRAGMA foreign_key_list({_quote(table_name)})"):
        grouped.setdefault(int(row[0]), []).append(row)
    seeds: list[RelationshipSeed] = []
    child_entity = entities[table_name]
    for rows in grouped.values():
        ordered = sorted(rows, key=lambda row: int(row[1]))
        parent_table = str(ordered[0][2])
        parent_entity = entities.get(parent_table)
        if parent_entity is None:
            continue
        child = Endpoint(
            entity_id=child_entity.entity_id,
            columns=tuple(str(row[3]) for row in ordered),
        )
        parent_columns = tuple(row[4] for row in ordered)
        if any(column is None for column in parent_columns):
            if not all(column is None for column in parent_columns) or len(
                parent_entity.primary_key
            ) != len(parent_columns):
                continue
            parent_columns = parent_entity.primary_key
        parent = Endpoint(
            entity_id=parent_entity.entity_id,
            columns=tuple(str(column) for column in parent_columns),
        )
        relationship_id = relationship_id_for(source_id, child, parent)
        cardinality = "one_to_one" if child.columns in child_entity.unique_keys else "many_to_one"
        seeds.append(
            RelationshipSeed(
                definition=RelationshipDefinition(
                    relationship_id=relationship_id,
                    source_id=source_id,
                    child=child,
                    parent=parent,
                ),
                provenance="declared",
                declared_cardinality=cardinality,
            )
        )
    return seeds


def _entity_id(source_id: str, table_name: str) -> str:
    if table_name and all(character.isalnum() or character in "_-" for character in table_name):
        return f"{source_id}.{table_name}"
    digest = hashlib.sha256(table_name.encode("utf-8")).hexdigest()[:24]
    return f"{source_id}.entity_{digest}"


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'
