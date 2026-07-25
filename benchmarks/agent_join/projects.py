from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path, PurePosixPath

import yaml

from joinlint.candidates import discover_candidates
from joinlint.config import ConfigV1, SourceConfig, write_yaml_atomically
from joinlint.contracts import canonical_json
from joinlint.model import Entity, Grain, ModelV1, Relationship, load_model, write_model
from joinlint.paths import SafeProject
from joinlint.scanner import ScanCatalog, scan_snapshot
from joinlint.services import (
    accept_candidate_by_id,
    list_candidates,
    reject_candidate_by_id,
    scan_project,
    validate_project,
)
from benchmarks.agent_join.contracts import SelectedTask
from joinlint.snapshots import snapshot_source


SOURCE_ID = "database"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parent


def relationship_id(prefix: str, from_endpoint: str, to_endpoint: str) -> str:
    digest = hashlib.sha256(
        canonical_json({"from": from_endpoint, "to": to_endpoint})
    ).hexdigest()[:16]
    return f"{prefix}_{digest}"


def build_review_project(
    source: Path,
    destination: Path,
    *,
    review_output: Path | None = None,
) -> Path:
    project = _create_empty_project(source, destination)
    scan_project(project)
    write_review_sheet(project, review_output)
    return project


def build_oracle_project(
    source: Path,
    oracle_schema: Mapping[str, object],
    destination: Path,
    *,
    model_output: Path | None = None,
) -> Path:
    project = _create_empty_project(source, destination)
    catalog = _catalog_for_project(project)
    tables = [str(value) for value in oracle_schema["table_names_original"]]  # type: ignore[index]
    columns = list(oracle_schema["column_names_original"])  # type: ignore[arg-type]
    endpoints = _column_endpoints(tables, columns)
    primary_keys = [int(value) for value in oracle_schema.get("primary_keys", [])]  # type: ignore[arg-type]
    primary_by_table: dict[str, list[str]] = {table: [] for table in tables}
    for column_index in primary_keys:
        table, column = endpoints[column_index].split(".", maxsplit=1)
        primary_by_table[table].append(column)

    entities = {
        table: Entity(
            source=SOURCE_ID,
            object=table,
            grain=Grain(keys=keys, status="confirmed"),
        )
        for table, keys in primary_by_table.items()
        if len(keys) == 1
    }
    unique_columns = {
        (table.name, column.name)
        for table in catalog.tables
        for column in table.columns
        if column.is_unique
    }
    relationships: list[Relationship] = []
    for child_index, parent_index in oracle_schema.get("foreign_keys", []):  # type: ignore[union-attr]
        child = endpoints[int(child_index)]
        parent = endpoints[int(parent_index)]
        child_table, child_column = child.split(".", maxsplit=1)
        parent_table = parent.split(".", maxsplit=1)[0]
        if child_table not in entities or parent_table not in entities:
            continue
        relationships.append(
            Relationship(
                id=relationship_id("oracle", child, parent),
                from_=child,
                to=parent,
                cardinality=(
                    "one_to_one"
                    if (child_table, child_column) in unique_columns
                    else "many_to_one"
                ),
                status="confirmed",
            )
        )
    relationships.sort(key=lambda item: item.id.encode("utf-8"))
    model = ModelV1(version=1, entities=entities, relationships=relationships)
    write_model(project, model)
    scan_project(project)
    if model_output is not None:
        _write_model_copy(model, model_output)
    return project


def write_review_sheet(project: Path, output: Path | None = None) -> Path:
    catalog = _catalog_for_project(project)
    model = load_model(project)
    if model.entities or model.relationships:
        raise ValueError("review sheets must be generated from an empty model")
    with snapshot_source(project, SOURCE_ID) as snapshot:
        candidates = discover_candidates(snapshot, catalog, model)

    entities = {
        table.name: {
            "object": table.name,
            "unique_choices": [
                column.name
                for column in table.columns
                if column.is_unique and column.physical_type != "unknown"
            ],
            "decision": "pending",
        }
        for table in catalog.tables
    }
    relationships = [
        {
            "id": candidate.id,
            "from": candidate.from_endpoint,
            "to": candidate.to_endpoint,
            "cardinality": candidate.cardinality,
            "confidence": candidate.confidence,
            "evidence": candidate.evidence.as_dict(),
            "decision": "pending",
        }
        for candidate in candidates
    ]
    document = {
        "schema_version": 1,
        "reviewer": "",
        "reviewed_at": "",
        "entities": entities,
        "relationships": relationships,
    }
    output = output or project / "review-decisions.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output


def apply_review_sheet(
    project: Path,
    sheet: Mapping[str, object] | Path,
    *,
    model_output: Path | None = None,
) -> Path:
    document = _load_sheet(sheet)
    _validate_top_level(document)
    reviewer = document["reviewer"]
    reviewed_at = document["reviewed_at"]
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("reviewer is required")
    if not isinstance(reviewed_at, str) or not _is_rfc3339(reviewed_at):
        raise ValueError("reviewed_at must be an RFC 3339 timestamp")

    catalog = _catalog_for_project(project)
    entities_document = document["entities"]
    if not isinstance(entities_document, dict):
        raise ValueError("entities must be an object")
    expected_tables = {table.name for table in catalog.tables}
    if set(entities_document) != expected_tables:
        raise ValueError("entity decisions must cover every physical table exactly once")

    entities: dict[str, Entity] = {}
    rejected_tables: set[str] = set()
    for table in catalog.tables:
        row = entities_document[table.name]
        if not isinstance(row, dict) or set(row) != {"object", "unique_choices", "decision"}:
            raise ValueError("entity decision has unknown or missing fields")
        choices = [
            column.name
            for column in table.columns
            if column.is_unique and column.physical_type != "unknown"
        ]
        if row["object"] != table.name or row["unique_choices"] != choices:
            raise ValueError("entity evidence changed after review-sheet generation")
        decision = row["decision"]
        if decision == "reject_table":
            rejected_tables.add(table.name)
            continue
        if not isinstance(decision, str) or decision not in choices:
            raise ValueError("entity decision must select one displayed unique grain")
        entities[table.name] = Entity(
            source=SOURCE_ID,
            object=table.name,
            grain=Grain(keys=[decision], status="confirmed"),
        )

    relationship_rows = document["relationships"]
    if not isinstance(relationship_rows, list) or not all(
        isinstance(row, dict) for row in relationship_rows
    ):
        raise ValueError("relationships must be an array of objects")
    relationship_by_id: dict[str, dict[str, object]] = {}
    for row in relationship_rows:
        if set(row) != {
            "id",
            "from",
            "to",
            "cardinality",
            "confidence",
            "evidence",
            "decision",
        }:
            raise ValueError("relationship decision has unknown or missing fields")
        candidate_id = row["id"]
        if not isinstance(candidate_id, str) or candidate_id in relationship_by_id:
            raise ValueError("relationship IDs must be unique strings")
        if row["decision"] not in {"accept", "reject"}:
            raise ValueError("relationship decision must be accept or reject")
        touched_tables = {
            str(row["from"]).split(".", maxsplit=1)[0],
            str(row["to"]).split(".", maxsplit=1)[0],
        }
        if row["decision"] == "accept" and touched_tables & rejected_tables:
            raise ValueError("accepted relationships cannot touch rejected tables")
        relationship_by_id[candidate_id] = row

    write_model(project, ModelV1(version=1, entities=entities, relationships=[]))
    scan_project(project)
    _require_unchanged_candidates(project, relationship_by_id)

    for candidate_id in sorted(relationship_by_id, key=lambda value: value.encode("utf-8")):
        decision = relationship_by_id[candidate_id]["decision"]
        if decision == "accept":
            accept_candidate_by_id(project, candidate_id)
            scan_project(project)
        else:
            reject_candidate_by_id(project, candidate_id)

    scan_project(project)
    validation = validate_project(project)
    if any(finding.severity == "blocking" for finding in validation.findings):
        raise ValueError("an accepted relationship has a blocking validation finding")
    model = load_model(project)
    if model_output is not None:
        _write_model_copy(model, model_output)
    return project


def audit_review_bundle(review_root: Path, forbidden_hashes: set[str]) -> None:
    for path in review_root.rglob("*"):
        if path.is_symlink():
            raise ValueError("review bundles cannot contain symlinks")
        if not path.is_file():
            continue
        if not (path.name.endswith("-decisions.json") or path.name == "audit.txt"):
            raise ValueError(f"review bundle contains an unexpected file: {path.name}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in forbidden_hashes:
            raise ValueError("review bundle contains a forbidden source artifact")


def prepare_evaluation_projects(
    work_dir: Path,
    hashes_path: Path,
    oracle_models: Path,
    review_dir: Path,
) -> None:
    tasks = _load_frozen_tasks(work_dir)
    spider_root = _find_spider_root(work_dir)
    metadata = {
        str(item["db_id"]): item
        for item in json.loads((spider_root / "tables.json").read_text(encoding="utf-8"))
    }
    database_ids = sorted({task.db_id for task in tasks}, key=_utf8_key)
    if len(database_ids) != 4 or any(db_id not in metadata for db_id in database_ids):
        raise ValueError("frozen tasks must cover four databases present in tables.json")
    _require_absent_or_empty(oracle_models)
    _require_absent_or_empty(review_dir)

    for db_id in database_ids:
        source = spider_root / "database" / db_id / f"{db_id}.sqlite"
        build_oracle_project(
            source,
            metadata[db_id],
            work_dir / "projects" / "oracle" / db_id,
            model_output=oracle_models / f"{db_id}.yaml",
        )
        build_review_project(
            source,
            work_dir / "projects" / "joinlint" / db_id,
            review_output=review_dir / f"{db_id}-decisions.json",
        )

    lock = _read_hash_lock(hashes_path)
    audit_review_bundle(review_dir, _hash_values(lock))
    lock["harness"] = {
        "git_commit": _git_commit(),
        "files": _harness_hashes(),
    }
    lock["models"] = {
        "oracle": {
            path.stem: _sha256(path)
            for path in sorted(oracle_models.glob("*.yaml"), key=lambda item: _utf8_key(item.name))
        }
    }
    lock["review_bundle"] = {
        "schema_version": 1,
        "database_ids": database_ids,
    }
    hashes_path.write_bytes(canonical_json(lock))


def apply_evaluation_reviews(
    work_dir: Path,
    review_dir: Path,
    joinlint_models: Path,
    hashes_path: Path,
) -> None:
    tasks = _load_frozen_tasks(work_dir)
    database_ids = sorted({task.db_id for task in tasks}, key=_utf8_key)
    expected_files = {f"{db_id}-decisions.json" for db_id in database_ids} | {"audit.txt"}
    actual_files = {path.name for path in review_dir.iterdir() if path.is_file()}
    if actual_files != expected_files:
        raise ValueError("review bundle must contain four decisions plus audit.txt")
    lock = _read_hash_lock(hashes_path)
    audit_review_bundle(review_dir, _hash_values(lock))
    _require_absent_or_empty(joinlint_models)

    review_summary: dict[str, dict[str, int]] = {}
    for db_id in database_ids:
        sheet_path = review_dir / f"{db_id}-decisions.json"
        sheet = _load_sheet(sheet_path)
        review_summary[db_id] = _review_counts(sheet)
        apply_review_sheet(
            work_dir / "projects" / "joinlint" / db_id,
            sheet,
            model_output=joinlint_models / f"{db_id}.yaml",
        )
    _build_safety_projects(work_dir / "projects" / "safety")

    models = lock.get("models")
    if not isinstance(models, dict) or "oracle" not in models:
        raise ValueError("hash lock is missing frozen oracle models")
    models["joinlint"] = {
        path.stem: _sha256(path)
        for path in sorted(joinlint_models.glob("*.yaml"), key=lambda item: _utf8_key(item.name))
    }
    lock["review_bundle"] = {
        "schema_version": 1,
        "database_ids": database_ids,
        "audit_sha256": _sha256(review_dir / "audit.txt"),
        "decision_counts": review_summary,
    }
    hashes_path.write_bytes(canonical_json(lock))


def _create_empty_project(source: Path, destination: Path) -> Path:
    if not source.is_file() or source.is_symlink():
        raise ValueError("source must be one regular SQLite file")
    if source.with_name(source.name + "-wal").exists() or source.with_name(
        source.name + "-shm"
    ).exists():
        raise ValueError("source must be a closed frozen SQLite database without sidecars")
    if destination.exists():
        if not destination.is_dir() or any(destination.iterdir()):
            raise ValueError("project destination must be absent or empty")
    (destination / "data").mkdir(parents=True, exist_ok=True)
    for directory in ("state", "analyses", "generated"):
        (destination / ".joinlint" / directory).mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination / "data" / "database.sqlite")
    with SafeProject(destination) as safe_project:
        write_yaml_atomically(
            safe_project,
            PurePosixPath(".joinlint/config.yaml"),
            ConfigV1(
                version=1,
                sources={
                    SOURCE_ID: SourceConfig(kind="sqlite", path="data/database.sqlite")
                },
            ),
        )
        write_yaml_atomically(
            safe_project,
            PurePosixPath(".joinlint/model.yaml"),
            ModelV1(version=1, entities={}, relationships=[]),
        )
    return destination


def _catalog_for_project(project: Path) -> ScanCatalog:
    with snapshot_source(project, SOURCE_ID) as snapshot:
        return scan_snapshot(snapshot)


def _load_sheet(sheet: Mapping[str, object] | Path) -> dict[str, object]:
    if isinstance(sheet, Path):
        document = json.loads(sheet.read_text(encoding="utf-8"))
    else:
        document = dict(sheet)
    if not isinstance(document, dict):
        raise ValueError("review sheet must be an object")
    return document


def _validate_top_level(document: dict[str, object]) -> None:
    if set(document) != {
        "schema_version",
        "reviewer",
        "reviewed_at",
        "entities",
        "relationships",
    } or document["schema_version"] != 1:
        raise ValueError("review sheet has unknown fields or schema version")


def _is_rfc3339(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _require_unchanged_candidates(
    project: Path,
    reviewed: dict[str, dict[str, object]],
) -> None:
    envelope = list_candidates(project)
    current = {
        str(candidate["id"]): candidate
        for candidate in envelope.data["candidates"]  # type: ignore[index]
    }
    if set(current) != set(reviewed):
        raise ValueError("candidate set changed after entity review")
    for candidate_id, row in reviewed.items():
        candidate = current[candidate_id]
        if (
            candidate["from_endpoint"] != row["from"]
            or candidate["to_endpoint"] != row["to"]
            or candidate["cardinality"] != row["cardinality"]
            or candidate["confidence"] != row["confidence"]
            or candidate["evidence"] != row["evidence"]
        ):
            raise ValueError("candidate evidence changed after entity review")


def _column_endpoints(tables: list[str], columns: list[object]) -> dict[int, str]:
    endpoints: dict[int, str] = {}
    for index, raw_column in enumerate(columns):
        table_index, column_name = raw_column  # type: ignore[misc]
        if int(table_index) >= 0:
            endpoints[index] = f"{tables[int(table_index)]}.{column_name}"
    return endpoints


def _write_model_copy(model: ModelV1, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        yaml.safe_dump(
            model.model_dump(mode="json", by_alias=True),
            allow_unicode=True,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _load_frozen_tasks(work_dir: Path) -> list[SelectedTask]:
    path = work_dir / "sealed" / "spider-pilot.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, list):
        raise ValueError("sealed pilot must be an array")
    tasks = [
        SelectedTask.model_validate_json(json.dumps(item, ensure_ascii=False))
        for item in document
    ]
    if (
        len(tasks) != 16
        or len({task.task_id for task in tasks}) != 16
        or len({task.db_id for task in tasks}) != 4
    ):
        raise ValueError("sealed pilot must contain 16 unique tasks from four databases")
    return tasks


def _find_spider_root(work_dir: Path) -> Path:
    source_root = work_dir / "source"
    candidates = {
        path.parent.resolve()
        for path in source_root.rglob("tables.json")
        if (path.parent / "dev.json").is_file() and (path.parent / "database").is_dir()
    }
    if len(candidates) != 1:
        raise ValueError("work directory must contain exactly one extracted Spider root")
    return candidates.pop()


def _require_absent_or_empty(path: Path) -> None:
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError(f"output must be absent or empty: {path.name}")
    path.mkdir(parents=True, exist_ok=True)


def _read_hash_lock(path: Path) -> dict[str, object]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("pilot hash lock has an invalid schema")
    return document


def _hash_values(value: object) -> set[str]:
    hashes: set[str] = set()
    if isinstance(value, dict):
        for item in value.values():
            hashes.update(_hash_values(item))
    elif isinstance(value, list):
        for item in value:
            hashes.update(_hash_values(item))
    elif isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    ):
        hashes.add(value)
    return hashes


def _harness_hashes() -> dict[str, str]:
    paths = [
        REPOSITORY_ROOT / "pyproject.toml",
        REPOSITORY_ROOT / "uv.lock",
        PACKAGE_ROOT / "preregistration.yaml",
        PACKAGE_ROOT / "prompts" / "base.txt",
        PACKAGE_ROOT / "prompts" / "mcp-integration.txt",
        PACKAGE_ROOT / "fixtures" / "mcp-safety" / "manifest.json",
    ]
    paths.extend(sorted((REPOSITORY_ROOT / "src" / "joinlint").glob("*.py")))
    paths.extend(sorted(PACKAGE_ROOT.glob("*.py")))
    paths.extend(sorted((PACKAGE_ROOT / "vendor").glob("*")))
    files = [path for path in paths if path.is_file()]
    return {
        path.relative_to(REPOSITORY_ROOT).as_posix(): _sha256(path)
        for path in sorted(files, key=lambda item: _utf8_key(item.as_posix()))
    }


def _git_commit() -> str:
    head = (REPOSITORY_ROOT / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    if head.startswith("ref: "):
        ref = REPOSITORY_ROOT / ".git" / head[5:]
        if ref.is_file():
            head = ref.read_text(encoding="utf-8").strip()
        else:
            packed = (REPOSITORY_ROOT / ".git" / "packed-refs").read_text(
                encoding="utf-8"
            )
            matches = [
                line.split(" ", maxsplit=1)[0]
                for line in packed.splitlines()
                if line.endswith(f" {head[5:]}")
            ]
            if len(matches) != 1:
                raise ValueError("cannot resolve the repository commit")
            head = matches[0]
    if len(head) != 40 or any(character not in "0123456789abcdef" for character in head):
        raise ValueError("repository HEAD is not a full commit hash")
    return head


def _review_counts(sheet: Mapping[str, object]) -> dict[str, int]:
    entities = sheet.get("entities")
    relationships = sheet.get("relationships")
    if not isinstance(entities, dict) or not isinstance(relationships, list):
        raise ValueError("review sheet has invalid decision collections")
    entity_decisions = [
        row.get("decision") for row in entities.values() if isinstance(row, dict)
    ]
    relationship_decisions = [
        row.get("decision") for row in relationships if isinstance(row, dict)
    ]
    if len(entity_decisions) != len(entities) or len(relationship_decisions) != len(
        relationships
    ):
        raise ValueError("review sheet contains malformed decision rows")
    return {
        "accepted_entities": sum(decision != "reject_table" for decision in entity_decisions),
        "rejected_entities": sum(decision == "reject_table" for decision in entity_decisions),
        "accepted_relationships": sum(
            decision == "accept" for decision in relationship_decisions
        ),
        "rejected_relationships": sum(
            decision == "reject" for decision in relationship_decisions
        ),
    }


def _build_safety_projects(root: Path) -> None:
    _require_absent_or_empty(root)
    manifest = json.loads(
        (PACKAGE_ROOT / "fixtures" / "mcp-safety" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    cases = manifest.get("cases") if isinstance(manifest, dict) else None
    if not isinstance(cases, list) or len(cases) != 4:
        raise ValueError("MCP safety manifest must contain exactly four cases")
    allowed_recipes = {
        "safe_many_to_one",
        "cardinality_drift",
        "compound_fanout",
    }
    seen_ids: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("MCP safety case must be an object")
        case_id = str(case.get("id", ""))
        recipe = str(case.get("recipe", ""))
        stale = case.get("mutation") == "rescan_after_model_change"
        if (
            not case_id
            or case_id in seen_ids
            or "/" in case_id
            or "\\" in case_id
            or recipe not in allowed_recipes
        ):
            raise ValueError("MCP safety case has an unsupported ID or recipe")
        seen_ids.add(case_id)
        _build_safety_project(root / case_id, recipe, stale=stale)


def _build_safety_project(project: Path, recipe: str, *, stale: bool) -> None:
    data = project / "data"
    data.mkdir(parents=True)
    for directory in ("state", "analyses", "generated"):
        (project / ".joinlint" / directory).mkdir(parents=True, exist_ok=True)
    model = _write_safety_data(data, recipe)
    with SafeProject(project) as safe_project:
        write_yaml_atomically(
            safe_project,
            PurePosixPath(".joinlint/config.yaml"),
            ConfigV1(
                version=1,
                sources={
                    "fixture": SourceConfig(kind="csv_directory", path="data")
                },
            ),
        )
        write_yaml_atomically(
            safe_project,
            PurePosixPath(".joinlint/model.yaml"),
            model,
        )
    scan_project(project)
    if stale:
        relationship = model.relationships[0].model_copy(
            update={"cardinality": "one_to_one"}
        )
        write_model(
            project,
            model.model_copy(update={"relationships": [relationship]}),
        )


def _write_safety_data(data: Path, recipe: str) -> ModelV1:
    if recipe == "compound_fanout":
        (data / "children.csv").write_text(
            "id,parent_id\n1,a\n2,a\n3,b\n",
            encoding="utf-8",
        )
        (data / "parents.csv").write_text(
            "id,grand_id\na,g1\nb,g1\n",
            encoding="utf-8",
        )
        (data / "grands.csv").write_text("id\ng1\n", encoding="utf-8")
        entities = {
            name: Entity(
                source="fixture",
                object=f"{name}.csv",
                grain=Grain(keys=["id"], status="confirmed"),
            )
            for name in ("children", "parents", "grands")
        }
        return ModelV1(
            version=1,
            entities=entities,
            relationships=[
                Relationship(
                    id="child_to_parent",
                    from_="children.parent_id",
                    to="parents.id",
                    cardinality="many_to_one",
                    status="confirmed",
                ),
                Relationship(
                    id="parent_to_grand",
                    from_="parents.grand_id",
                    to="grands.id",
                    cardinality="many_to_one",
                    status="confirmed",
                ),
            ],
        )

    left_values = ["a", "a", "b"]
    right_values = ["a", "b"]
    (data / "left.csv").write_text(
        "key\n" + "\n".join(left_values) + "\n",
        encoding="utf-8",
    )
    (data / "right.csv").write_text(
        "key\n" + "\n".join(right_values) + "\n",
        encoding="utf-8",
    )
    entities = {
        name: Entity(
            source="fixture",
            object=f"{name}.csv",
            grain=Grain(keys=["key"], status="confirmed"),
        )
        for name in ("left", "right")
    }
    return ModelV1(
        version=1,
        entities=entities,
        relationships=[
            Relationship(
                id="left_to_right",
                from_="left.key",
                to="right.key",
                cardinality=("one_to_one" if recipe == "cardinality_drift" else "many_to_one"),
                status="confirmed",
            )
        ],
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utf8_key(value: str) -> bytes:
    return value.encode("utf-8")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare and freeze JoinLint Agent evaluation projects"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--work-dir", type=Path, required=True)
    prepare.add_argument("--hashes", type=Path, required=True)
    prepare.add_argument("--oracle-models", type=Path, required=True)
    prepare.add_argument("--review-dir", type=Path, required=True)
    apply = subparsers.add_parser("apply")
    apply.add_argument("--work-dir", type=Path, required=True)
    apply.add_argument("--review-dir", type=Path, required=True)
    apply.add_argument("--joinlint-models", type=Path, required=True)
    apply.add_argument("--hashes", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    arguments = _parse_args()
    if arguments.command == "prepare":
        prepare_evaluation_projects(
            arguments.work_dir,
            arguments.hashes,
            arguments.oracle_models,
            arguments.review_dir,
        )
    else:
        apply_evaluation_reviews(
            arguments.work_dir,
            arguments.review_dir,
            arguments.joinlint_models,
            arguments.hashes,
        )


if __name__ == "__main__":
    main()
