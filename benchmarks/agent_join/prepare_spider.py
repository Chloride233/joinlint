from __future__ import annotations

import argparse
import hashlib
import json
import stat
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import sqlglot
import yaml
from sqlglot import exp

from benchmarks.agent_join.contracts import Edge, SelectedTask
from benchmarks.agent_join.sql_edges import canonical_edge, extract_join_edges
from joinlint.contracts import canonical_json


MAX_ARCHIVE_MEMBER_BYTES = 1 << 30
MAX_ARCHIVE_TOTAL_BYTES = 8 << 30
MAX_ARCHIVE_MEMBERS = 100_000
OFFICIAL_SPIDER_URL = (
    "https://drive.google.com/file/d/1403EGqzIDoHMdQF4c9Bkyl7dZLZ5Wt6J/"
    "view?usp=sharing"
)


def seeded_rank(seed: int, *parts: str) -> bytes:
    payload = "\0".join((str(seed), *parts)).encode("utf-8")
    return hashlib.sha256(payload).digest()


def find_spider_root(archive: Path, work_dir: Path) -> Path:
    if not archive.is_file() or archive.suffix.lower() != ".zip":
        raise ValueError("Spider source must be one regular .zip archive")
    source_root = work_dir / "source"
    if source_root.exists() and not source_root.is_dir():
        raise ValueError("work_dir/source must be a directory")
    archive_digest = _sha256(archive)
    marker = source_root / ".archive-sha256"
    if source_root.exists() and any(source_root.iterdir()):
        if not marker.is_file() or marker.read_text(encoding="ascii").strip() != archive_digest:
            raise ValueError("non-empty work_dir/source does not match the requested archive")
        return _require_one_spider_root(source_root)
    source_root.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise ValueError("archive contains too many members")
        total_size = 0
        for member in members:
            _validate_archive_member(member)
            total_size += member.file_size
            if total_size > MAX_ARCHIVE_TOTAL_BYTES:
                raise ValueError("archive expands beyond the total size limit")
        bundle.extractall(source_root)

    marker.write_text(archive_digest + "\n", encoding="ascii")
    return _require_one_spider_root(source_root)


def _require_one_spider_root(source_root: Path) -> Path:
    candidates = {
        path.parent.resolve()
        for path in source_root.rglob("dev.json")
        if (path.parent / "tables.json").is_file() and (path.parent / "database").is_dir()
    }
    if len(candidates) != 1:
        raise ValueError("archive must contain exactly one Spider root")
    root = candidates.pop()
    if not root.is_relative_to(source_root.resolve()):
        raise ValueError("Spider root escaped the extraction directory")
    return root


def _validate_archive_member(member: zipfile.ZipInfo) -> None:
    path = PurePosixPath(member.filename)
    mode = member.external_attr >> 16
    if (
        path.is_absolute()
        or (path.parts and path.parts[0].endswith(":"))
        or ".." in path.parts
        or not path.parts
        or stat.S_ISLNK(mode)
        or member.file_size > MAX_ARCHIVE_MEMBER_BYTES
    ):
        raise ValueError(f"unsafe archive member: {member.filename!r}")


def select_pilot(
    spider_root: Path,
    *,
    seed: int,
    database_count: int,
    tasks_per_database: int,
    split: str = "dev",
) -> list[SelectedTask]:
    split_filename = _split_filename(split)
    records = _read_json_list(spider_root / split_filename)
    tables = _read_json_list(spider_root / "tables.json")
    metadata_by_db = {str(document["db_id"]): document for document in tables}
    if len(metadata_by_db) != len(tables):
        raise ValueError("tables.json contains duplicate database IDs")
    eligible: dict[str, list[SelectedTask]] = defaultdict(list)

    for source_index, record in enumerate(records):
        db_id = str(record.get("db_id", ""))
        metadata = metadata_by_db.get(db_id)
        if metadata is None:
            continue
        task_id = f"spider-{split.replace('_', '-')}-{source_index:04d}"
        schema_map = physical_schema(metadata)
        query = str(record.get("query", ""))
        if classify_eligibility(query, metadata, schema_map) != "ELIGIBLE":
            continue
        graph = sorted(extract_join_edges(query, schema_map), key=_edge_sort_key)
        database = spider_root / "database" / db_id / f"{db_id}.sqlite"
        if not database.is_file():
            continue
        eligible[db_id].append(
            SelectedTask(
                task_id=task_id,
                db_id=db_id,
                question=str(record.get("question", "")),
                schema_text=render_schema(metadata),
                schema=schema_map,
                gold_sql=query,
                allowed_graphs=[graph],
                oracle_relationships=build_oracle_graph(metadata),
                database_path=str(database.resolve()),
            )
        )

    ranked_databases = sorted(
        (db_id for db_id, tasks in eligible.items() if len(tasks) >= tasks_per_database),
        key=lambda db_id: seeded_rank(seed, db_id),
    )
    selected_databases = ranked_databases[:database_count]
    if len(selected_databases) != database_count:
        raise ValueError("not enough eligible databases for the frozen pilot")

    selected: list[SelectedTask] = []
    for db_id in selected_databases:
        tasks = sorted(
            eligible[db_id],
            key=lambda task: seeded_rank(seed, db_id, task.task_id),
        )
        selected.extend(tasks[:tasks_per_database])
    return selected


def classify_eligibility(
    query: str,
    metadata: dict[str, object],
    schema_map: dict[str, dict[str, str]] | None = None,
) -> str:
    schema_map = schema_map or physical_schema(metadata)
    try:
        expression = sqlglot.parse_one(query, read="sqlite")
    except sqlglot.errors.ParseError:
        return "SQL_PARSE_ERROR"
    if not isinstance(expression, exp.Select):
        return "NOT_SELECT"

    physical_tables = [table.name for table in expression.find_all(exp.Table)]
    if any(count > 1 for count in Counter(physical_tables).values()):
        return "SELF_JOIN"
    if any("." in table for table in physical_tables):
        return "DOTTED_TABLE_NAME"

    try:
        edges = extract_join_edges(query, schema_map)
    except ValueError:
        return "SQL_QUALIFICATION_ERROR"
    if _has_cross_table_non_equality(expression):
        return "NON_EQUALITY_JOIN"
    if not 1 <= len(edges) <= 3:
        return "JOIN_EDGE_COUNT"

    table_pairs = [tuple(sorted(_edge_tables(edge), key=_utf8_key)) for edge in edges]
    if len(table_pairs) != len(set(table_pairs)):
        return "COMPOSITE_JOIN"

    declared = declared_fk_edges(metadata)
    if not edges <= declared:
        return "MISSING_DECLARED_FK"
    primary_key_counts = _primary_key_counts(metadata)
    joined_tables = {table for edge in edges for table in _edge_tables(edge)}
    if any(primary_key_counts.get(table, 0) != 1 for table in joined_tables):
        return "UNSUPPORTED_GRAIN"
    return "ELIGIBLE"


def _has_cross_table_non_equality(expression: exp.Expression) -> bool:
    for comparison_type in (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NEQ):
        for comparison in expression.find_all(comparison_type):
            left = comparison.left
            right = comparison.right
            if (
                isinstance(left, exp.Column)
                and isinstance(right, exp.Column)
                and left.table
                and right.table
                and left.table != right.table
            ):
                return True
    return False


def physical_schema(metadata: dict[str, object]) -> dict[str, dict[str, str]]:
    tables = [str(name) for name in metadata["table_names_original"]]  # type: ignore[index]
    columns = metadata["column_names_original"]  # type: ignore[index]
    types = metadata["column_types"]  # type: ignore[index]
    schema: dict[str, dict[str, str]] = {table: {} for table in tables}
    for raw_column, raw_type in zip(columns, types, strict=True):
        table_index, column_name = raw_column
        if int(table_index) >= 0:
            schema[tables[int(table_index)]][str(column_name)] = str(raw_type).upper()
    return schema


def render_schema(metadata: dict[str, object]) -> str:
    schema = physical_schema(metadata)
    tables = [str(name) for name in metadata["table_names_original"]]  # type: ignore[index]
    columns = metadata["column_names_original"]  # type: ignore[index]
    primary_keys = {int(index) for index in metadata.get("primary_keys", [])}  # type: ignore[arg-type]
    column_index: dict[tuple[str, str], int] = {}
    for index, raw_column in enumerate(columns):
        table_index, column_name = raw_column
        if int(table_index) >= 0:
            column_index[(tables[int(table_index)], str(column_name))] = index

    lines: list[str] = []
    for table in sorted(schema, key=_utf8_key):
        lines.append(f"TABLE {table}")
        for column, physical_type in schema[table].items():
            suffix = " PRIMARY KEY" if column_index[(table, column)] in primary_keys else ""
            lines.append(f"  {column} {physical_type}{suffix}")
    return "\n".join(lines)


def declared_fk_edges(metadata: dict[str, object]) -> frozenset[Edge]:
    endpoints = _column_endpoints(metadata)
    return frozenset(
        canonical_edge(endpoints[int(child)], endpoints[int(parent)])
        for child, parent in metadata.get("foreign_keys", [])  # type: ignore[union-attr]
    )


def build_oracle_graph(metadata: dict[str, object]) -> list[dict[str, object]]:
    endpoints = _column_endpoints(metadata)
    relationships = []
    for child, parent in metadata.get("foreign_keys", []):  # type: ignore[union-attr]
        from_endpoint = endpoints[int(child)]
        to_endpoint = endpoints[int(parent)]
        identity = canonical_json({"from": from_endpoint, "to": to_endpoint})
        relationships.append(
            {
                "id": f"oracle_{hashlib.sha256(identity).hexdigest()[:16]}",
                "from": from_endpoint,
                "to": to_endpoint,
                "cardinality": "many_to_one",
                "status": "confirmed",
            }
        )
    return sorted(relationships, key=lambda item: str(item["id"]).encode("utf-8"))


def write_pilot_lock(
    tasks: list[SelectedTask],
    *,
    spider_root: Path,
    archive: Path,
    work_dir: Path,
    manifest_path: Path,
    hashes_path: Path,
    split: str = "dev",
) -> None:
    split_filename = _split_filename(split)
    sealed_path = work_dir / "sealed" / "spider-pilot.json"
    sealed_path.parent.mkdir(parents=True, exist_ok=True)
    sealed_path.write_bytes(
        canonical_json([task.model_dump(mode="json", by_alias=True) for task in tasks])
    )

    manifest = {
        "schema_version": 1,
        "split": split,
        "tasks": [
            {
                "task_id": task.task_id,
                "db_id": task.db_id,
                "eligibility_reason": "ELIGIBLE",
                "database_sha256": _sha256(Path(task.database_path)),
                "question_sha256": _sha256_bytes(task.question.encode("utf-8")),
                "gold_sql_sha256": _sha256_bytes(task.gold_sql.encode("utf-8")),
                "schema_sha256": _sha256_bytes(task.schema_text.encode("utf-8")),
            }
            for task in tasks
        ],
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(canonical_json(manifest))

    selected_databases = sorted({task.db_id for task in tasks}, key=_utf8_key)
    sources = {
        "archive": _sha256(archive),
        split_filename: _sha256(spider_root / split_filename),
        "tables.json": _sha256(spider_root / "tables.json"),
        "databases": {
            db_id: _sha256(spider_root / "database" / db_id / f"{db_id}.sqlite")
            for db_id in selected_databases
        },
    }
    hashes_path.parent.mkdir(parents=True, exist_ok=True)
    downloaded_at = datetime.fromtimestamp(
        archive.stat().st_mtime,
        tz=timezone.utc,
    ).replace(microsecond=0)
    hashes_path.write_bytes(
        canonical_json(
            {
                "schema_version": 1,
                "acquisition": {
                    "url": OFFICIAL_SPIDER_URL,
                    "downloaded_at": downloaded_at.isoformat().replace("+00:00", "Z"),
                    "archive_sha256": sources["archive"],
                    "digest_provenance": "computed_locally_not_published_by_spider",
                },
                "sources": sources,
            }
        )
    )


def _primary_key_counts(metadata: dict[str, object]) -> Counter[str]:
    endpoints = _column_endpoints(metadata)
    return Counter(
        endpoints[int(index)].rsplit(".", 1)[0]
        for index in metadata.get("primary_keys", [])  # type: ignore[union-attr]
    )


def _column_endpoints(metadata: dict[str, object]) -> dict[int, str]:
    tables = [str(name) for name in metadata["table_names_original"]]  # type: ignore[index]
    endpoints: dict[int, str] = {}
    for index, raw_column in enumerate(metadata["column_names_original"]):  # type: ignore[index]
        table_index, column_name = raw_column
        if int(table_index) >= 0:
            endpoints[index] = f"{tables[int(table_index)]}.{column_name}"
    return endpoints


def _edge_tables(edge: Edge) -> tuple[str, str]:
    return edge[0].rsplit(".", 1)[0], edge[1].rsplit(".", 1)[0]


def _edge_sort_key(edge: Edge) -> tuple[bytes, bytes]:
    return edge[0].encode("utf-8"), edge[1].encode("utf-8")


def _utf8_key(value: str) -> bytes:
    return value.encode("utf-8")


def _read_json_list(path: Path) -> list[dict[str, object]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, list) or not all(isinstance(item, dict) for item in document):
        raise ValueError(f"{path.name} must contain one JSON array of objects")
    return document


def _split_filename(split: str) -> str:
    filenames = {"dev": "dev.json", "train_spider": "train_spider.json"}
    try:
        return filenames[split]
    except KeyError as error:
        raise ValueError("unsupported Spider split") from error


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare the frozen Spider Agent join pilot")
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--hashes", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    root = find_spider_root(args.archive, args.work_dir)
    preregistration = yaml.safe_load(
        (Path(__file__).with_name("preregistration.yaml")).read_text(encoding="utf-8")
    )
    split = str(preregistration["pilot"]["split"])
    tasks = select_pilot(
        root,
        seed=20260725,
        database_count=4,
        tasks_per_database=4,
        split=split,
    )
    write_pilot_lock(
        tasks,
        spider_root=root,
        archive=args.archive,
        work_dir=args.work_dir,
        manifest_path=args.manifest,
        hashes_path=args.hashes,
        split=split,
    )


if __name__ == "__main__":
    main()
