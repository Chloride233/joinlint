from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from joinlint.contracts import canonical_json
from joinlint.errors import JoinLintError
from joinlint.model import ModelV1, Relationship, entity_id_for_table, model_digest, write_model
from joinlint.paths import SafeProject
from joinlint.scanner import ColumnProfile, ScanCatalog, TableProfile
from joinlint.snapshots import SourceSnapshot


POLICY_VERSION = "candidate-v1"
Confidence = Literal["strong", "medium", "low"]


@dataclass(frozen=True)
class CandidateEvidence:
    matched_distinct_count: int
    inclusion_numerator: int
    inclusion_denominator: int
    null_count: int
    orphan_count: int
    referenced_is_unique: bool
    cardinality: Literal["one_to_one", "many_to_one"]

    def as_dict(self) -> dict[str, int | bool | str]:
        return {
            "matched_distinct_count": self.matched_distinct_count,
            "inclusion_numerator": self.inclusion_numerator,
            "inclusion_denominator": self.inclusion_denominator,
            "null_count": self.null_count,
            "orphan_count": self.orphan_count,
            "referenced_is_unique": self.referenced_is_unique,
            "cardinality": self.cardinality,
        }


@dataclass(frozen=True)
class RelationshipCandidate:
    id: str
    source_id: str
    from_endpoint: str
    to_endpoint: str
    cardinality: Literal["one_to_one", "many_to_one"]
    confidence: Confidence
    evidence: CandidateEvidence
    source_fingerprints: tuple[str, ...]
    types: tuple[str, str]
    model_digest: str

    def identity(self) -> dict[str, str]:
        return {"source": self.source_id, "from": self.from_endpoint, "to": self.to_endpoint}


@dataclass
class RejectionState:
    by_source: dict[str, set[str]] = field(default_factory=dict)
    legacy: set[str] = field(default_factory=set)


def discover_candidates(
    snapshot: SourceSnapshot, catalog: ScanCatalog, model: ModelV1
) -> list[RelationshipCandidate]:
    values = snapshot_values(snapshot)
    candidates: list[RelationshipCandidate] = []
    for child_table in catalog.tables:
        for child_column in child_table.columns:
            child_values = values[child_table.name][child_column.name]
            for parent_table in catalog.tables:
                if parent_table.name == child_table.name:
                    continue
                for parent_column in parent_table.columns:
                    if not parent_column.is_unique or not _types_are_compatible(child_column, parent_column):
                        continue
                    parent_values = values[parent_table.name][parent_column.name]
                    candidate = _candidate_for_columns(
                        snapshot,
                        model,
                        child_table,
                        child_column,
                        child_values,
                        parent_table,
                        parent_column,
                        parent_values,
                    )
                    if candidate is not None:
                        candidates.append(candidate)
    return sorted(candidates, key=lambda candidate: candidate.id.encode("utf-8"))


def visible_candidates(
    project: Path,
    snapshot: SourceSnapshot,
    catalog: ScanCatalog,
    model: ModelV1,
) -> list[RelationshipCandidate]:
    rejected = _load_rejections(project)
    return [
        candidate
        for candidate in discover_candidates(snapshot, catalog, model)
        if rejection_key(candidate) not in rejected.legacy
        and rejection_key(candidate) not in rejected.by_source.get(candidate.source_id, set())
    ]


def rejection_key(candidate: RelationshipCandidate) -> str:
    payload = {
        "policy": POLICY_VERSION,
        "candidate": candidate.identity(),
        "types": list(candidate.types),
        "source_fingerprints": list(candidate.source_fingerprints),
        "evidence": candidate.evidence.as_dict(),
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def reject_candidate(project: Path, candidate: RelationshipCandidate) -> None:
    rejections = _load_rejections(project)
    rejections.by_source.setdefault(candidate.source_id, set()).add(rejection_key(candidate))
    _write_rejections(project, rejections)


def invalidate_rejections_for_source(project: Path, source_id: str) -> None:
    rejections = _load_rejections(project)
    rejections.by_source.pop(source_id, None)
    # Legacy v1 entries predate source metadata, so they cannot be selectively retained.
    rejections.legacy.clear()
    if rejections.by_source:
        _write_rejections(project, rejections)
        return
    with SafeProject(project) as safe_project:
        safe_project.unlink_relative(PurePosixPath(".joinlint/state/rejections.json"), missing_ok=True)


def accept_candidate(project: Path, candidate: RelationshipCandidate) -> None:
    model = _load_model_for_candidate(project)
    if model_digest(model) != candidate.model_digest:
        raise JoinLintError("CANDIDATE_STALE", "candidate model evidence is stale", 3)
    if any(relationship.id == candidate.id for relationship in model.relationships):
        raise JoinLintError("CANDIDATE_ALREADY_ACCEPTED", "candidate is already confirmed", 2)

    relationship = Relationship(
        id=candidate.id,
        from_=candidate.from_endpoint,
        to=candidate.to_endpoint,
        cardinality=candidate.cardinality,
        status="confirmed",
    )
    try:
        updated = ModelV1(
            version=1,
            entities=model.entities,
            relationships=[*model.relationships, relationship],
        )
    except ValueError as exc:
        raise JoinLintError("CANDIDATE_STALE", "candidate no longer matches the model", 3) from exc
    write_model(project, updated)


def _load_model_for_candidate(project: Path) -> ModelV1:
    from joinlint.model import load_model

    return load_model(project)


def _candidate_for_columns(
    snapshot: SourceSnapshot,
    model: ModelV1,
    child_table: TableProfile,
    child_column: ColumnProfile,
    child_values: Iterable[object],
    parent_table: TableProfile,
    parent_column: ColumnProfile,
    parent_values: Iterable[object],
) -> RelationshipCandidate | None:
    normalized_parent_values = {
        normalize_value(value, parent_column.physical_type) for value in parent_values if value not in (None, "")
    }
    normalized_child_values = [
        normalize_value(value, child_column.physical_type) for value in child_values if value not in (None, "")
    ]
    if not normalized_child_values:
        return None
    inclusion_numerator = sum(value in normalized_parent_values for value in normalized_child_values)
    if inclusion_numerator == 0:
        return None
    matched_distinct_count = len(set(normalized_child_values) & normalized_parent_values)
    null_count = child_table.row_count - len(normalized_child_values)
    orphan_count = len(normalized_child_values) - inclusion_numerator
    cardinality: Literal["one_to_one", "many_to_one"] = (
        "one_to_one" if child_column.is_unique else "many_to_one"
    )
    evidence = CandidateEvidence(
        matched_distinct_count=matched_distinct_count,
        inclusion_numerator=inclusion_numerator,
        inclusion_denominator=len(normalized_child_values),
        null_count=null_count,
        orphan_count=orphan_count,
        referenced_is_unique=parent_column.is_unique,
        cardinality=cardinality,
    )
    child_entity = entity_id_for_table(model, snapshot.source_id, child_table.name, snapshot.kind)
    parent_entity = entity_id_for_table(model, snapshot.source_id, parent_table.name, snapshot.kind)
    from_endpoint = f"{child_entity or child_table.name}.{child_column.name}"
    to_endpoint = f"{parent_entity or parent_table.name}.{parent_column.name}"
    identity = {"source": snapshot.source_id, "from": from_endpoint, "to": to_endpoint}
    candidate_id = "candidate_" + hashlib.sha256(canonical_json(identity)).hexdigest()[:16]
    return RelationshipCandidate(
        id=candidate_id,
        source_id=snapshot.source_id,
        from_endpoint=from_endpoint,
        to_endpoint=to_endpoint,
        cardinality=cardinality,
        confidence=_confidence(evidence),
        evidence=evidence,
        source_fingerprints=(snapshot.fingerprint,),
        types=(child_column.physical_type, parent_column.physical_type),
        model_digest=model_digest(model),
    )


def _confidence(evidence: CandidateEvidence) -> Confidence:
    if evidence.orphan_count == 0 and evidence.null_count == 0:
        return "strong"
    if evidence.inclusion_numerator * 10 >= evidence.inclusion_denominator * 8:
        return "medium"
    return "low"


def _types_are_compatible(child: ColumnProfile, parent: ColumnProfile) -> bool:
    if child.physical_type == parent.physical_type:
        return child.physical_type != "unknown"
    return {child.physical_type, parent.physical_type} <= {"integer", "number"}


def snapshot_values(snapshot: SourceSnapshot) -> dict[str, dict[str, list[object]]]:
    return _csv_snapshot_values(snapshot) if snapshot.kind == "csv_directory" else _sqlite_snapshot_values(snapshot)


def _csv_snapshot_values(snapshot: SourceSnapshot) -> dict[str, dict[str, list[object]]]:
    values: dict[str, dict[str, list[object]]] = {}
    for snapshot_file in snapshot.files:
        with snapshot_file.path.open(encoding="utf-8", newline="") as source:
            reader = csv.reader(source)
            headers = next(reader)
            table_values = {header: [] for header in headers}
            for row in reader:
                for header, value in zip(headers, row, strict=True):
                    table_values[header].append(value)
        values[snapshot_file.relative_path.with_suffix("").as_posix()] = table_values
    return values


def _sqlite_snapshot_values(snapshot: SourceSnapshot) -> dict[str, dict[str, list[object]]]:
    connection = sqlite3.connect(f"file:{snapshot.files[0].path}?mode=ro", uri=True)
    try:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        result: dict[str, dict[str, list[object]]] = {}
        for (table_name,) in tables:
            quoted_name = _quote_identifier(table_name)
            headers = [row[1] for row in connection.execute(f"PRAGMA table_info({quoted_name})")]
            table_values = {header: [] for header in headers}
            for row in connection.execute(f"SELECT * FROM {quoted_name}"):
                for header, value in zip(headers, row, strict=True):
                    table_values[header].append(value)
            result[table_name] = table_values
        return result
    finally:
        connection.close()


def _quote_identifier(identifier: str) -> str:
    return f'"{identifier.replace('"', '""')}"'


def normalize_value(value: object, physical_type: str) -> str:
    text = str(value)
    if physical_type in {"integer", "number"}:
        try:
            return format(Decimal(text).normalize(), "f")
        except InvalidOperation:
            return text
    if physical_type == "boolean":
        return "true" if text.lower() == "true" else "false" if text.lower() == "false" else text
    return text


def _load_rejections(project: Path) -> RejectionState:
    try:
        with SafeProject(project) as safe_project:
            descriptor = safe_project.open_relative(PurePosixPath(".joinlint/state/rejections.json"), os.O_RDONLY)
            with os.fdopen(descriptor, "r", encoding="utf-8") as source:
                document: Any = json.load(source)
        values = document["rejections"]
        if document["version"] != 1 or not isinstance(values, list):
            raise ValueError("invalid rejection state")
        state = RejectionState()
        for value in values:
            if isinstance(value, str):
                state.legacy.add(value)
                continue
            if (
                not isinstance(value, dict)
                or set(value) != {"source_id", "key"}
                or not isinstance(value["source_id"], str)
                or not value["source_id"]
                or not isinstance(value["key"], str)
            ):
                raise ValueError("invalid rejection state")
            state.by_source.setdefault(value["source_id"], set()).add(value["key"])
        return state
    except JoinLintError as exc:
        if exc.code == "SAFE_PATH_OPEN_FAILED":
            return RejectionState()
        raise JoinLintError("MALFORMED_REJECTIONS", "local rejection state is malformed", 2) from exc
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise JoinLintError("MALFORMED_REJECTIONS", "local rejection state is malformed", 2) from exc


def _write_rejections(project: Path, rejections: RejectionState) -> None:
    values: list[object] = sorted(rejections.legacy)
    values.extend(
        {"source_id": source_id, "key": key}
        for source_id in sorted(rejections.by_source, key=lambda value: value.encode("utf-8"))
        for key in sorted(rejections.by_source[source_id])
    )
    payload = json.dumps({"version": 1, "rejections": values}, separators=(",", ":")).encode("utf-8")
    with SafeProject(project) as safe_project:
        safe_project.ensure_directory_relative(PurePosixPath(".joinlint/state"))
        safe_project.write_relative_atomically(PurePosixPath(".joinlint/state/rejections.json"), payload)
