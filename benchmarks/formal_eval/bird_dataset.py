from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import urllib.request
import uuid
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable, Iterable, Mapping

import rfc8785
from sqlglot import exp, parse
from sqlglot.errors import ParseError


_DATABASE_ID = re.compile(r"[A-Za-z0-9_]{1,128}")
_DOWNLOAD_CHUNK_BYTES = 8 * 1024 * 1024
_MAX_METADATA_BYTES = 16 * 1024 * 1024
_MAX_DATABASE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_NESTED_ARCHIVE_BYTES = 10_000_000_000


@dataclass(frozen=True)
class BirdSourceExpectation:
    url: str
    size: int
    etag: str
    last_modified: str
    databases_member_size: int
    databases_member_crc32: int


@dataclass(frozen=True)
class BirdArchiveSource:
    url: str
    size: int
    etag: str
    last_modified: str
    sha256: str


OFFICIAL_TRAIN_SOURCE = BirdSourceExpectation(
    url="https://bird-bench.oss-cn-beijing.aliyuncs.com/train.zip",
    size=8_919_543_554,
    etag='"1FBA2891BD215A42CE1004736AB7F47F-400"',
    last_modified="Tue, 11 Jul 2023 06:13:29 GMT",
    databases_member_size=9_347_158_408,
    databases_member_crc32=0xE28EE04B,
)


def is_eligible_bird_sql(sql: str) -> bool:
    """Apply the preregistered structural eligibility rules without JoinLint output."""
    try:
        first = parse(sql, read="sqlite")
        second = parse(sql, read="sqlite")
    except ParseError:
        return False
    if len(first) != 1 or first != second:
        return False
    tree = first[0]
    if not isinstance(tree, exp.Expression):
        return False
    joins = list(tree.find_all(exp.Join))
    if not 1 <= len(joins) <= 4:
        return False
    if len({table.name.casefold() for table in tree.find_all(exp.Table)}) < 2:
        return False

    implicit_joins = 0
    for join in joins:
        kind = (join.args.get("kind") or "").upper()
        side = (join.args.get("side") or "").upper()
        if kind not in {"", "INNER"} or side not in {"", "LEFT"}:
            return False
        predicate = join.args.get("on")
        using = join.args.get("using")
        if predicate is not None:
            if not _only_cross_table_equalities(predicate):
                return False
        elif using:
            if side == "LEFT" or not all(isinstance(value, exp.Identifier) for value in using):
                return False
        else:
            if side == "LEFT":
                return False
            implicit_joins += 1

    if implicit_joins:
        where = tree.args.get("where")
        if where is None or len(_cross_table_equalities(where.this)) < implicit_joins:
            return False
    return True


def select_eligible_tasks(
    tasks: Iterable[Mapping[str, Any]], database_ids: tuple[str, ...]
) -> tuple[dict[str, Any], ...]:
    selected_ids = _validate_database_ids(database_ids)
    selected: list[dict[str, Any]] = []
    for index, task in enumerate(tasks):
        database_id = task.get("db_id")
        sql = task.get("SQL")
        if database_id not in selected_ids or not isinstance(sql, str):
            continue
        if not is_eligible_bird_sql(sql):
            continue
        question = task.get("question")
        evidence = task.get("evidence", "")
        if not isinstance(question, str) or not isinstance(evidence, str):
            raise ValueError("BIRD task text fields must be strings")
        selected.append(
            {
                "task_index": index,
                "db_id": database_id,
                "question": question,
                "evidence": evidence,
                "SQL": sql,
            }
        )
    return tuple(sorted(selected, key=lambda task: (task["db_id"], task["task_index"])))


def download_archive(
    url: str,
    destination: Path,
    *,
    expected_size: int,
    expected_etag: str,
    expected_last_modified: str | None = None,
    opener: Callable[[urllib.request.Request], BinaryIO] = urllib.request.urlopen,
) -> BirdArchiveSource:
    """Stream one immutable archive to an atomic local file."""
    if destination.exists() or destination.is_symlink():
        raise ValueError("download destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    partial.unlink(missing_ok=True)
    digest = hashlib.sha256()
    size = 0
    request = urllib.request.Request(url, headers={"User-Agent": "JoinLint-formal-eval/2"})
    try:
        with opener(request) as response:
            headers = response.headers
            content_length = _required_header(headers, "Content-Length")
            etag = _required_header(headers, "ETag")
            last_modified = _required_header(headers, "Last-Modified")
            if int(content_length) != expected_size:
                raise ValueError("remote archive size changed")
            if etag != expected_etag:
                raise ValueError("remote archive ETag changed")
            if expected_last_modified is not None and last_modified != expected_last_modified:
                raise ValueError("remote archive Last-Modified changed")
            with partial.open("xb") as output:
                while chunk := response.read(_DOWNLOAD_CHUNK_BYTES):
                    size += len(chunk)
                    if size > expected_size:
                        raise ValueError("download exceeded expected archive size")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        if size != expected_size:
            raise ValueError("download is incomplete")
        os.replace(partial, destination)
        return BirdArchiveSource(
            url=url,
            size=size,
            etag=etag,
            last_modified=last_modified,
            sha256=digest.hexdigest(),
        )
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def prepare_bird_subset(
    archive_path: Path,
    destination: Path,
    *,
    database_ids: tuple[str, ...],
    source: BirdArchiveSource,
    min_tasks_per_database: int = 5,
    max_database_bytes: int = _MAX_DATABASE_BYTES,
    expected_databases_member_size: int | None = None,
    expected_databases_member_crc32: int | None = None,
) -> dict[str, Any]:
    """Produce one sealed candidate subset without reading JoinLint output."""
    selected_ids = _validate_database_ids(database_ids)
    if min_tasks_per_database < 1:
        raise ValueError("minimum task count must be positive")
    if max_database_bytes < 1:
        raise ValueError("database size limit must be positive")
    if destination.exists() or destination.is_symlink():
        raise ValueError("destination already exists")
    if not archive_path.is_file() or archive_path.is_symlink():
        raise ValueError("BIRD archive must be one regular file")
    actual_size, actual_digest = _file_identity(archive_path)
    if actual_size != source.size or actual_digest != source.sha256:
        raise ValueError("BIRD archive does not match its source record")

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(f".{destination.name}.staging-{uuid.uuid4().hex}")
    staging.mkdir()
    nested_path = staging / ".train_databases.zip"
    try:
        with zipfile.ZipFile(archive_path) as outer:
            outer_infos = _validated_infos(outer)
            nested_info = _required_member(outer_infos, "train/train_databases.zip")
            if nested_info.file_size > _MAX_NESTED_ARCHIVE_BYTES:
                raise ValueError("nested database archive exceeds its size limit")
            if (
                expected_databases_member_size is not None
                and nested_info.file_size != expected_databases_member_size
            ):
                raise ValueError("nested database archive size changed")
            if (
                expected_databases_member_crc32 is not None
                and nested_info.CRC != expected_databases_member_crc32
            ):
                raise ValueError("nested database archive CRC changed")
            _copy_zip_member(outer, nested_info, nested_path, nested_info.file_size)
            tasks = _read_json_member(outer, outer_infos, "train/train.json")
            tables = _read_json_member(outer, outer_infos, "train/train_tables.json")

        if not isinstance(tasks, list) or not isinstance(tables, list):
            raise ValueError("BIRD metadata members must contain JSON arrays")
        candidates = select_eligible_tasks(tasks, tuple(sorted(selected_ids)))
        counts = {database_id: 0 for database_id in selected_ids}
        for candidate in candidates:
            counts[candidate["db_id"]] += 1
        insufficient = sorted(
            database_id
            for database_id, count in counts.items()
            if count < min_tasks_per_database
        )
        if insufficient:
            raise ValueError(f"selected database has insufficient eligible tasks: {insufficient}")
        selected_tables = _selected_table_metadata(tables, selected_ids)

        database_dir = staging / "databases"
        database_dir.mkdir()
        database_records = _extract_selected_databases(
            nested_path,
            database_dir,
            selected_ids,
            max_database_bytes=max_database_bytes,
        )
        nested_path.unlink()

        candidate_path = staging / "candidate-tasks.json"
        tables_path = staging / "selected-tables.json"
        _write_canonical(candidate_path, list(candidates))
        _write_canonical(tables_path, list(selected_tables))
        manifest = {
            "schema_version": 1,
            "dataset_release": "BIRD train 2023-07-11",
            "license": "CC BY-SA 4.0",
            "source": asdict(source),
            "selection": {
                "database_ids": sorted(selected_ids),
                "eligible_task_count": len(candidates),
                "eligible_task_counts": dict(sorted(counts.items())),
                "min_tasks_per_database": min_tasks_per_database,
                "join_count_range": [1, 4],
                "dialect": "sqlite",
                "uses_joinlint_output": False,
            },
            "selected_databases": database_records,
            "metadata_files": {
                "candidate-tasks.json": _file_record(candidate_path),
                "selected-tables.json": _file_record(tables_path),
            },
        }
        _write_canonical(staging / "source-manifest.json", manifest)
        os.replace(staging, destination)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify_bird_subset(root: Path) -> dict[str, Any]:
    if not root.is_dir() or root.is_symlink():
        raise ValueError("BIRD subset root must be one real directory")
    manifest_path = root / "source-manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("BIRD subset source manifest is unavailable")
    try:
        manifest = json.loads(manifest_path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("BIRD subset source manifest is invalid") from error
    if manifest.get("schema_version") != 1:
        raise ValueError("BIRD subset schema version is unsupported")
    selection = manifest.get("selection")
    if not isinstance(selection, dict) or selection.get("uses_joinlint_output") is not False:
        raise ValueError("BIRD subset selection provenance is invalid")

    expected_files = {"source-manifest.json"}
    metadata_files = manifest.get("metadata_files")
    if not isinstance(metadata_files, dict) or set(metadata_files) != {
        "candidate-tasks.json",
        "selected-tables.json",
    }:
        raise ValueError("BIRD subset metadata file manifest is invalid")
    for relative_name, record in metadata_files.items():
        _verify_file_record(root, relative_name, record)
        expected_files.add(relative_name)

    databases = manifest.get("selected_databases")
    if not isinstance(databases, list) or not databases:
        raise ValueError("BIRD subset contains no selected databases")
    selected_ids: list[str] = []
    for record in databases:
        if not isinstance(record, dict):
            raise ValueError("BIRD database file manifest is invalid")
        database_id = record.get("database_id")
        relative_name = record.get("path")
        if (
            not isinstance(database_id, str)
            or _DATABASE_ID.fullmatch(database_id) is None
            or relative_name != f"databases/{database_id}.sqlite"
        ):
            raise ValueError("BIRD database file identity is invalid")
        _verify_file_record(root, relative_name, record)
        _check_sqlite(root / relative_name)
        expected_files.add(relative_name)
        selected_ids.append(database_id)
    if selected_ids != sorted(set(selected_ids)):
        raise ValueError("BIRD selected database ordering is invalid")
    if selection.get("database_ids") != selected_ids:
        raise ValueError("BIRD selection does not match its database files")

    candidates = _read_regular_json(root / "candidate-tasks.json")
    tables = _read_regular_json(root / "selected-tables.json")
    if not isinstance(candidates, list) or not isinstance(tables, list):
        raise ValueError("BIRD subset metadata must contain JSON arrays")
    counts = {database_id: 0 for database_id in selected_ids}
    task_order: list[tuple[str, int]] = []
    for task in candidates:
        if not isinstance(task, dict) or task.get("db_id") not in counts:
            raise ValueError("BIRD candidate task references an unknown database")
        task_index = task.get("task_index")
        sql = task.get("SQL")
        if (
            not isinstance(task_index, int)
            or isinstance(task_index, bool)
            or not isinstance(sql, str)
            or not is_eligible_bird_sql(sql)
        ):
            raise ValueError("BIRD candidate task does not meet the eligibility contract")
        task_order.append((task["db_id"], task_index))
        counts[task["db_id"]] += 1
    if task_order != sorted(set(task_order)):
        raise ValueError("BIRD candidate task ordering is invalid")
    if selection.get("eligible_task_counts") != counts:
        raise ValueError("BIRD candidate task counts do not match the manifest")
    if selection.get("eligible_task_count") != len(candidates):
        raise ValueError("BIRD candidate task total does not match the manifest")
    minimum = selection.get("min_tasks_per_database")
    if (
        not isinstance(minimum, int)
        or isinstance(minimum, bool)
        or minimum < 1
        or any(count < minimum for count in counts.values())
    ):
        raise ValueError("BIRD candidate task minimum is not satisfied")
    if any(not isinstance(table, dict) for table in tables):
        raise ValueError("BIRD table metadata rows must be objects")
    if [table.get("db_id") for table in tables] != selected_ids:
        raise ValueError("BIRD table metadata does not match the selected databases")

    actual_files = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    if actual_files != expected_files:
        raise ValueError("BIRD subset contains unmanifested files")
    if any(path.is_symlink() for path in root.rglob("*")):
        raise ValueError("BIRD subset contains a symlink")
    return manifest


def _only_cross_table_equalities(predicate: exp.Expression) -> bool:
    if isinstance(predicate, exp.Paren):
        return _only_cross_table_equalities(predicate.this)
    if isinstance(predicate, exp.And):
        return _only_cross_table_equalities(predicate.left) and _only_cross_table_equalities(
            predicate.right
        )
    return _is_cross_table_equality(predicate)


def _cross_table_equalities(predicate: exp.Expression) -> tuple[exp.EQ, ...]:
    return tuple(
        node
        for node in predicate.walk(
            prune=lambda child: child is not predicate
            and isinstance(child, (exp.Select, exp.Subquery))
        )
        if isinstance(node, exp.EQ) and _is_cross_table_equality(node)
    )


def _is_cross_table_equality(predicate: exp.Expression) -> bool:
    if not isinstance(predicate, exp.EQ):
        return False
    if not isinstance(predicate.left, exp.Column) or not isinstance(predicate.right, exp.Column):
        return False
    left_table = predicate.left.table
    right_table = predicate.right.table
    return bool(left_table and right_table and left_table.casefold() != right_table.casefold())


def _validate_database_ids(database_ids: tuple[str, ...]) -> frozenset[str]:
    if not database_ids:
        raise ValueError("at least one database ID is required")
    if len(set(database_ids)) != len(database_ids):
        raise ValueError("database IDs must be unique")
    if any(_DATABASE_ID.fullmatch(database_id) is None for database_id in database_ids):
        raise ValueError("database ID is invalid")
    return frozenset(database_ids)


def _required_header(headers: Mapping[str, str], name: str) -> str:
    value = headers.get(name)
    if value is None or not value.strip():
        raise ValueError(f"remote archive omitted {name}")
    return value.strip()


def _file_identity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(_DOWNLOAD_CHUNK_BYTES):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _validated_infos(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos: dict[str, zipfile.ZipInfo] = {}
    for info in archive.infolist():
        name = info.filename
        path = PurePosixPath(name)
        if (
            not name
            or name.startswith(("/", "\\", "~/"))
            or "\\" in name
            or ".." in path.parts
        ):
            raise ValueError(f"unsafe archive member: {name}")
        if name in infos:
            raise ValueError(f"duplicate archive member: {name}")
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise ValueError(f"archive member is a symlink: {name}")
        infos[name] = info
    return infos


def _required_member(
    infos: Mapping[str, zipfile.ZipInfo], name: str
) -> zipfile.ZipInfo:
    try:
        info = infos[name]
    except KeyError as error:
        raise ValueError(f"required BIRD archive member is missing: {name}") from error
    if info.is_dir():
        raise ValueError(f"required BIRD archive member is not a file: {name}")
    return info


def _copy_zip_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    destination: Path,
    max_bytes: int,
) -> None:
    if info.file_size > max_bytes:
        raise ValueError(f"archive member exceeds its size limit: {info.filename}")
    written = 0
    with archive.open(info) as source, destination.open("xb") as output:
        while chunk := source.read(_DOWNLOAD_CHUNK_BYTES):
            written += len(chunk)
            if written > max_bytes:
                raise ValueError(f"archive member exceeded its size limit: {info.filename}")
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    if written != info.file_size:
        raise ValueError(f"archive member extraction was incomplete: {info.filename}")


def _read_json_member(
    archive: zipfile.ZipFile,
    infos: Mapping[str, zipfile.ZipInfo],
    name: str,
) -> Any:
    info = _required_member(infos, name)
    if info.file_size > _MAX_METADATA_BYTES:
        raise ValueError(f"BIRD metadata member is too large: {name}")
    try:
        return json.loads(archive.read(info))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"BIRD metadata member is invalid: {name}") from error


def _selected_table_metadata(
    tables: Iterable[Any], selected_ids: frozenset[str]
) -> tuple[dict[str, Any], ...]:
    selected: dict[str, dict[str, Any]] = {}
    for table in tables:
        if not isinstance(table, dict):
            raise ValueError("BIRD table metadata rows must be objects")
        database_id = table.get("db_id")
        if database_id not in selected_ids:
            continue
        if database_id in selected:
            raise ValueError(f"duplicate BIRD table metadata: {database_id}")
        selected[database_id] = table
    missing = sorted(selected_ids - selected.keys())
    if missing:
        raise ValueError(f"selected table metadata is unavailable: {missing}")
    return tuple(selected[database_id] for database_id in sorted(selected))


def _extract_selected_databases(
    nested_path: Path,
    destination: Path,
    selected_ids: frozenset[str],
    *,
    max_database_bytes: int,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with zipfile.ZipFile(nested_path) as nested:
        infos = _validated_infos(nested)
        for database_id in sorted(selected_ids):
            member_name = f"train_databases/{database_id}/{database_id}.sqlite"
            info = infos.get(member_name)
            if info is None or info.is_dir():
                raise ValueError(f"selected database is unavailable: {database_id}")
            database_path = destination / f"{database_id}.sqlite"
            _copy_zip_member(nested, info, database_path, max_database_bytes)
            _check_sqlite(database_path)
            record = _file_record(database_path)
            record["database_id"] = database_id
            record["path"] = f"databases/{database_id}.sqlite"
            records.append(record)
    return records


def _check_sqlite(path: Path) -> None:
    uri = f"file:{path.as_posix()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        rows = connection.execute("PRAGMA quick_check").fetchall()
    except sqlite3.DatabaseError as error:
        raise ValueError(f"selected database is not valid SQLite: {path.name}") from error
    finally:
        connection.close()
    if rows != [("ok",)]:
        raise ValueError(f"selected database failed SQLite quick_check: {path.name}")


def _file_record(path: Path) -> dict[str, Any]:
    size, digest = _file_identity(path)
    return {"size": size, "sha256": digest}


def _verify_file_record(root: Path, relative_name: str, record: Any) -> None:
    if not isinstance(record, dict):
        raise ValueError("BIRD subset file record is invalid")
    path = PurePosixPath(relative_name)
    if relative_name.startswith("/") or ".." in path.parts or "\\" in relative_name:
        raise ValueError("BIRD subset file path is unsafe")
    candidate = root / relative_name
    if not candidate.is_file() or candidate.is_symlink():
        raise ValueError(f"BIRD subset file is unavailable: {relative_name}")
    actual = _file_record(candidate)
    if record.get("size") != actual["size"] or record.get("sha256") != actual["sha256"]:
        raise ValueError(f"BIRD subset file hash mismatch: {relative_name}")


def _read_regular_json(path: Path) -> Any:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"BIRD subset metadata is unavailable: {path.name}")
    try:
        return json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"BIRD subset metadata is invalid: {path.name}") from error


def _write_canonical(path: Path, value: Any) -> None:
    payload = rfc8785.dumps(value)
    with path.open("xb") as output:
        output.write(payload)
        output.write(b"\n")
        output.flush()
        os.fsync(output.fileno())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify one sealed BIRD subset")
    parser.add_argument("--root", type=Path, required=True)
    arguments = parser.parse_args(argv)
    manifest = verify_bird_subset(arguments.root)
    print(
        json.dumps(
            {
                "schema_version": manifest["schema_version"],
                "source_sha256": manifest["source"]["sha256"],
                "eligible_task_count": manifest["selection"]["eligible_task_count"],
                "selected_database_count": len(manifest["selected_databases"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
