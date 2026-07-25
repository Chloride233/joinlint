from __future__ import annotations

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
from joinlint.snapshots import snapshot_source


SOURCE_ID = "database"


def relationship_id(prefix: str, from_endpoint: str, to_endpoint: str) -> str:
    digest = hashlib.sha256(
        canonical_json({"from": from_endpoint, "to": to_endpoint})
    ).hexdigest()[:16]
    return f"{prefix}_{digest}"


def build_review_project(source: Path, destination: Path) -> Path:
    project = _create_empty_project(source, destination)
    scan_project(project)
    write_review_sheet(project)
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
