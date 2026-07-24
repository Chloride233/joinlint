from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from joinlint.artifacts import load_fresh_manifest as load_fresh_manifest_file
from joinlint.artifacts import write_generated_transaction, write_report_atomically
from joinlint.candidates import (
    POLICY_VERSION,
    RelationshipCandidate,
    accept_candidate,
    discover_candidates,
    reject_candidate,
    visible_candidates,
)
from joinlint.errors import JoinLintError
from joinlint.config import ConfigV1, load_config
from joinlint.contracts import Envelope, Finding, canonical_json
from joinlint.model import ModelV1, Relationship, entity_table_name, load_model, model_digest
from joinlint.paths import SafeProject
from joinlint.scanner import ScanCatalog, TableProfile, scan_snapshot
from joinlint.snapshots import snapshot_source
from joinlint.validation import ValidationResult, relationship_digest, validate_relationship


SCANNER_VERSION = "0.1.0"
_CARDINALITIES = {"one_to_one", "one_to_many", "many_to_one", "many_to_many"}
_VALIDATION_CODES = {
    "REFERENCED_KEY_NOT_UNIQUE",
    "CHILD_KEY_NULL",
    "ORPHAN_CHILD_ROW",
    "CARDINALITY_DRIFT",
    "GRAIN_CHANGE",
    "MANY_TO_MANY_FANOUT",
    "COMPOUND_FANOUT",
}


@dataclass(frozen=True)
class CurrentEvidence:
    source_fingerprints: tuple[str, ...]
    schemas: list[dict[str, object]]
    relationship_results: list[dict[str, object]]
    findings: list[object]


def scan_project(project: Path) -> Envelope:
    """Scan every registered source and publish one generated-evidence transaction."""
    config = load_config(project)
    model = load_model(project)
    _require_model_sources(model, config)
    catalogs: list[ScanCatalog] = []
    candidates: list[dict[str, object]] = []
    source_fingerprints: list[str] = []
    validation_results: list[ValidationResult] = []
    relationships_by_source = _relationships_by_source(model)
    for source_id in sorted(config.sources, key=lambda value: value.encode("utf-8")):
        with snapshot_source(project, source_id) as snapshot:
            catalog = scan_snapshot(snapshot)
            catalogs.append(catalog)
            source_fingerprints.append(snapshot.fingerprint)
            _require_source_objects(model, config, source_id, catalog)
            candidates.extend(
                _candidate_document(candidate)
                for candidate in discover_candidates(snapshot, catalog, model)
            )
            validation_results.extend(
                validate_relationship(relationship, snapshot, catalog, model)
                for relationship in relationships_by_source.get(source_id, [])
            )
    validation_results.sort(key=lambda result: result.relationship_id.encode("utf-8"))
    digest = model_digest(model)
    scan_id = hashlib.sha256(
        canonical_json(
            {
                "source_fingerprints": source_fingerprints,
                "model_digest": digest,
                "relationship_digests": _relationship_digests(model),
                "scanner_version": SCANNER_VERSION,
                "policy_version": POLICY_VERSION,
            }
        )
    ).hexdigest()
    files = {
        "manifest.json": _json_bytes(
            {
                "schema_version": 1,
                "scan_id": scan_id,
                "source_fingerprints": source_fingerprints,
                "model_digest": digest,
                "relationship_digests": _relationship_digests(model),
                "scanner_version": SCANNER_VERSION,
                "policy_version": POLICY_VERSION,
            }
        ),
        "catalog.json": _json_bytes({"schema_version": 1, "sources": [_catalog_document(item) for item in catalogs]}),
        "profile.json": _json_bytes({"schema_version": 1, "sources": [_catalog_document(item) for item in catalogs]}),
        "relationship-candidates.json": _json_bytes(
            {"schema_version": 1, "candidates": sorted(candidates, key=lambda item: str(item["id"]).encode("utf-8"))}
        ),
        "validation.json": _json_bytes(
            {"schema_version": 1, "relationships": [_validation_document(result) for result in validation_results]}
        ),
    }
    write_generated_transaction(project, files, _report_data(config, model, catalogs, candidates, validation_results))
    return Envelope(
        command="scan",
        status="ok",
        data={"scan_id": scan_id, "sources": sorted(config.sources)},
    )


def list_candidates(project: Path) -> Envelope:
    candidates = _fresh_candidates(project)
    return Envelope(
        command="candidates",
        status="ok",
        data={"candidates": [_candidate_document(candidate) for candidate in candidates]},
    )


def reject_candidate_by_id(project: Path, candidate_id: str) -> Envelope:
    candidate = _candidate_by_id(project, candidate_id, include_rejected=False)
    reject_candidate(project, candidate)
    return Envelope(command="reject", status="ok", data={"candidate_id": candidate_id})


def accept_candidate_by_id(project: Path, candidate_id: str) -> Envelope:
    candidate = _candidate_by_id(project, candidate_id, include_rejected=False)
    accept_candidate(project, candidate)
    return Envelope(command="accept", status="ok", data={"candidate_id": candidate_id})


def validate_project(project: Path) -> Envelope:
    evidence = collect_current_evidence(project)
    return Envelope(
        command="validate",
        status="findings" if evidence.findings else "ok",
        data={"relationships": evidence.relationship_results},
        findings=evidence.findings,
    )


def collect_current_evidence(project: Path) -> CurrentEvidence:
    config = load_config(project)
    model = load_model(project)
    _require_model_sources(model, config)
    relationships_by_source = _relationships_by_source(model)

    results: list[ValidationResult] = []
    schemas: list[dict[str, object]] = []
    source_fingerprints: list[str] = []
    for source_id in sorted(config.sources, key=lambda value: value.encode("utf-8")):
        with snapshot_source(project, source_id) as snapshot:
            catalog = scan_snapshot(snapshot)
            _require_source_objects(model, config, source_id, catalog)
            schemas.append(_catalog_document(catalog))
            source_fingerprints.append(snapshot.fingerprint)
            results.extend(
                validate_relationship(relationship, snapshot, catalog, model)
                for relationship in relationships_by_source.get(source_id, [])
            )
    results.sort(key=lambda result: result.relationship_id.encode("utf-8"))
    findings = [finding for result in results for finding in result.findings]
    findings.sort(key=lambda finding: finding.code.encode("utf-8"))
    return CurrentEvidence(
        source_fingerprints=tuple(source_fingerprints),
        schemas=schemas,
        relationship_results=[_validation_document(result) for result in results],
        findings=findings,
    )


def run_check(project: Path) -> Envelope:
    from joinlint.baseline import compare_baseline, load_baseline

    baseline = load_baseline(project)
    evidence = collect_current_evidence(project)
    findings = [*evidence.findings, *compare_baseline(baseline, evidence)]
    findings.sort(key=lambda finding: finding.code.encode("utf-8"))
    return Envelope(
        command="check",
        status="findings" if findings else "ok",
        data={"relationships": evidence.relationship_results},
        findings=findings,
    )


def regenerate_report(project: Path) -> Envelope:
    model = load_model(project)
    _require_fresh_manifest(project, model)
    try:
        config = load_config(project)
        catalog = _read_generated_json(project, "catalog.json")
        candidates = _read_generated_json(project, "relationship-candidates.json")
        validations = _read_generated_json(project, "validation.json")
        report_data = _report_data_from_documents(config, model, catalog, candidates, validations)
    except (JoinLintError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise JoinLintError("EVIDENCE_STALE", "generated evidence is unavailable", 3) from exc
    write_report_atomically(project, report_data)
    return Envelope(command="report", status="ok", data={"sources": sorted(config.sources)})


def get_data_model(project: Path) -> Envelope:
    config = load_config(project)
    model = load_model(project)
    _require_model_sources(model, config)
    return Envelope(command="get_data_model", status="ok", data=_model_document(model))


def find_join_path(project: Path, source_entity: str, target_entity: str, max_depth: int) -> Envelope:
    if not 1 <= max_depth <= 4:
        raise JoinLintError("INVALID_DEPTH", "max_depth must be between 1 and 4", 2)
    config = load_config(project)
    model = load_model(project)
    _require_model_sources(model, config)
    if source_entity not in model.entities or target_entity not in model.entities:
        raise JoinLintError("ENTITY_NOT_FOUND", "entity is not confirmed", 2)
    paths: list[list[dict[str, str]]] = []
    queue: list[tuple[str, list[dict[str, str]], set[str]]] = [(source_entity, [], {source_entity})]
    while queue:
        entity, path, seen = queue.pop(0)
        if entity == target_entity and path:
            paths.append(path)
            if len(paths) > 20:
                raise JoinLintError("OUTPUT_LIMIT_EXCEEDED", "more than 20 confirmed paths exist", 3)
            continue
        if len(path) == max_depth:
            continue
        for relationship in sorted(model.relationships, key=lambda item: item.id.encode("utf-8")):
            from_entity = relationship.from_.split(".", maxsplit=1)[0]
            to_entity = relationship.to.split(".", maxsplit=1)[0]
            if entity == from_entity and to_entity not in seen:
                queue.append((to_entity, [*path, _path_edge(relationship, "forward")], seen | {to_entity}))
            if entity == to_entity and from_entity not in seen:
                queue.append((from_entity, [*path, _path_edge(relationship, "reverse")], seen | {from_entity}))
    paths.sort(
        key=lambda path: tuple(
            (edge["id"].encode("utf-8"), edge["direction"].encode("utf-8")) for edge in path
        )
    )
    return Envelope(command="find_join_path", status="ok", data={"paths": paths})


def validate_cached_edges(project: Path, edge_ids: list[str]) -> Envelope:
    if (
        not edge_ids
        or len(edge_ids) > 16
        or not all(isinstance(edge_id, str) for edge_id in edge_ids)
        or len(set(edge_ids)) != len(edge_ids)
    ):
        raise JoinLintError("INVALID_ARGUMENT", "edge_ids must contain 1 to 16 unique IDs", 2)
    model = load_model(project)
    _require_fresh_manifest(project, model)
    results = _load_cached_validation_results(project)
    relationships = {relationship.id: relationship for relationship in model.relationships}
    if any(
        edge_id not in relationships
        or edge_id not in results
        or results[edge_id]["relationship_digest"] != relationship_digest(relationships[edge_id])
        for edge_id in edge_ids
    ):
        raise JoinLintError("EVIDENCE_STALE", "validation evidence is stale", 3)
    ordered_edge_ids = sorted(edge_ids, key=lambda edge_id: edge_id.encode("utf-8"))
    findings = [
        Finding.model_validate(finding)
        for edge_id in ordered_edge_ids
        for finding in results[edge_id]["findings"]
    ]
    findings.sort(key=lambda finding: finding.code.encode("utf-8"))
    return Envelope(
        command="validate_join",
        status="findings" if findings else "ok",
        data={"relationships": [results[item] for item in ordered_edge_ids]},
        findings=findings,
    )


def validate_cached_path(project: Path, path: list[dict[str, str]]) -> Envelope:
    if (
        not path
        or len(path) > 16
        or any(
            not isinstance(edge, dict)
            or set(edge) != {"id", "direction", "cardinality"}
            or not all(isinstance(value, str) for value in edge.values())
            for edge in path
        )
    ):
        raise JoinLintError("INVALID_ARGUMENT", "path must be a returned path of at most 16 edges", 2)
    return validate_cached_edges(project, [edge["id"] for edge in path])


def _relationships_by_source(model: ModelV1) -> dict[str, list[Relationship]]:
    relationships_by_source: dict[str, list[Relationship]] = {}
    for relationship in model.relationships:
        from_entity = model.entities[relationship.from_.split(".", maxsplit=1)[0]]
        to_entity = model.entities[relationship.to.split(".", maxsplit=1)[0]]
        if from_entity.source != to_entity.source:
            raise JoinLintError("RELATIONSHIP_CROSS_SOURCE_UNSUPPORTED", "cross-source relationship validation is not available", 2)
        relationships_by_source.setdefault(from_entity.source, []).append(relationship)
    for relationships in relationships_by_source.values():
        relationships.sort(key=lambda relationship: relationship.id.encode("utf-8"))
    return relationships_by_source


def _require_model_sources(model: ModelV1, config: ConfigV1) -> None:
    missing = sorted(
        {entity.source for entity in model.entities.values()} - set(config.sources),
        key=lambda source_id: source_id.encode("utf-8"),
    )
    if missing:
        raise JoinLintError("MODEL_REFERENCE_MISSING", "model references an unregistered source", 2)


def _require_source_objects(
    model: ModelV1, config: ConfigV1, source_id: str, catalog: ScanCatalog
) -> None:
    source_kind = config.sources[source_id].kind
    for entity in model.entities.values():
        if entity.source != source_id:
            continue
        table_name = entity_table_name(entity, source_kind)
        try:
            table = catalog.table(table_name)
            table.column(entity.grain.keys[0])
        except KeyError as exc:
            raise JoinLintError("MODEL_REFERENCE_MISSING", "model object or grain key is missing from its source", 2) from exc


def _require_fresh_manifest(project: Path, model: ModelV1) -> None:
    config = load_config(project)
    _require_model_sources(model, config)
    fingerprints: list[str] = []
    for source_id in sorted(config.sources, key=lambda value: value.encode("utf-8")):
        with snapshot_source(project, source_id) as snapshot:
            fingerprints.append(snapshot.fingerprint)
    _load_fresh_evidence(project, fingerprints, model)


def _path_edge(relationship: Relationship, direction: str) -> dict[str, str]:
    return {"id": relationship.id, "direction": direction, "cardinality": relationship.cardinality}


def _candidate_by_id(project: Path, candidate_id: str, *, include_rejected: bool) -> RelationshipCandidate:
    candidates = _fresh_candidates(project, include_rejected=include_rejected)
    for candidate in candidates:
        if candidate.id == candidate_id:
            return candidate
    raise JoinLintError("CANDIDATE_NOT_FOUND", "candidate is not current", 2)


def _fresh_candidates(project: Path, *, include_rejected: bool = False) -> list[RelationshipCandidate]:
    config = load_config(project)
    model = load_model(project)
    _require_model_sources(model, config)
    candidates: list[RelationshipCandidate] = []
    source_fingerprints: list[str] = []
    for source_id in sorted(config.sources, key=lambda value: value.encode("utf-8")):
        with snapshot_source(project, source_id) as snapshot:
            catalog = scan_snapshot(snapshot)
            _require_source_objects(model, config, source_id, catalog)
            source_fingerprints.append(snapshot.fingerprint)
            if include_rejected:
                candidates.extend(discover_candidates(snapshot, catalog, model))
            else:
                candidates.extend(visible_candidates(project, snapshot, catalog, model))
    _load_fresh_evidence(project, source_fingerprints, model)
    return sorted(candidates, key=lambda candidate: candidate.id.encode("utf-8"))


def _load_fresh_evidence(project: Path, source_fingerprints: list[str], model: ModelV1) -> None:
    manifest = load_fresh_manifest_file(project, source_fingerprints, model_digest(model))
    if (
        manifest.get("scanner_version") != SCANNER_VERSION
        or manifest.get("policy_version") != POLICY_VERSION
        or manifest.get("relationship_digests") != _relationship_digests(model)
    ):
        raise JoinLintError("EVIDENCE_STALE", "generated evidence uses a different scanner or policy", 3)


def _load_cached_validation_results(project: Path) -> dict[str, dict[str, object]]:
    try:
        document = _read_generated_json(project, "validation.json")
        if document.get("schema_version") != 1 or not isinstance(document.get("relationships"), list):
            raise ValueError("validation schema is invalid")
        results = [_sanitize_validation_result(item) for item in document["relationships"]]
        by_id = {str(item["relationship_id"]): item for item in results}
        if len(by_id) != len(results):
            raise ValueError("validation relationship IDs are not unique")
        return by_id
    except (JoinLintError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise JoinLintError("EVIDENCE_STALE", "validation evidence is unavailable", 3) from exc


def _sanitize_validation_result(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "relationship_id",
        "relationship_digest",
        "observed_cardinality",
        "from_rows_per_to",
        "to_rows_per_from",
        "findings",
    }:
        raise ValueError("validation result is invalid")
    relationship_id = value["relationship_id"]
    digest = value["relationship_digest"]
    cardinality = value["observed_cardinality"]
    from_rows_per_to = value["from_rows_per_to"]
    to_rows_per_from = value["to_rows_per_from"]
    findings = value["findings"]
    if (
        not isinstance(relationship_id, str)
        or not isinstance(digest, str)
        or len(digest) != 64
        or cardinality not in _CARDINALITIES
        or type(from_rows_per_to) is not int
        or type(to_rows_per_from) is not int
        or from_rows_per_to < 0
        or to_rows_per_from < 0
        or not isinstance(findings, list)
    ):
        raise ValueError("validation result fields are invalid")
    sanitized_findings: list[Finding] = []
    for finding_document in findings:
        finding = Finding.model_validate(finding_document)
        if finding.code not in _VALIDATION_CODES:
            raise ValueError("validation finding code is invalid")
        sanitized_findings.append(Finding(code=finding.code, severity=finding.severity, message=finding.code))
    sanitized_findings.sort(key=lambda finding: finding.code.encode("utf-8"))
    return {
        "relationship_id": relationship_id,
        "relationship_digest": digest,
        "observed_cardinality": cardinality,
        "from_rows_per_to": from_rows_per_to,
        "to_rows_per_from": to_rows_per_from,
        "findings": [finding.model_dump() for finding in sanitized_findings],
    }


def _read_generated_json(project: Path, name: str) -> dict[str, Any]:
    with SafeProject(project) as safe_project:
        descriptor = safe_project.open_relative(
            PurePosixPath(".joinlint") / "generated" / name, os.O_RDONLY
        )
        with os.fdopen(descriptor, "r", encoding="utf-8") as source:
            document: Any = json.load(source)
    if not isinstance(document, dict):
        raise ValueError("generated document must be an object")
    return document


def _catalog_document(catalog: ScanCatalog) -> dict[str, object]:
    return {"source_id": catalog.source_id, "tables": [_table_document(table) for table in catalog.tables]}


def _table_document(table: TableProfile) -> dict[str, object]:
    return {
        "name": table.name,
        "row_count": table.row_count,
        "columns": [asdict(column) for column in table.columns],
    }


def _model_document(model: ModelV1) -> dict[str, object]:
    document = model.model_dump(mode="json", by_alias=True)
    document["entities"] = {
        entity_id: document["entities"][entity_id]
        for entity_id in sorted(document["entities"], key=lambda value: value.encode("utf-8"))
    }
    document["relationships"] = sorted(
        document["relationships"], key=lambda relationship: str(relationship["id"]).encode("utf-8")
    )
    return document


def _relationship_digests(model: ModelV1) -> dict[str, str]:
    return {
        relationship.id: relationship_digest(relationship)
        for relationship in sorted(model.relationships, key=lambda item: item.id.encode("utf-8"))
    }


def _candidate_document(candidate: RelationshipCandidate) -> dict[str, object]:
    return asdict(candidate)


def _validation_document(result: ValidationResult) -> dict[str, object]:
    return {
        "relationship_id": result.relationship_id,
        "relationship_digest": result.relationship_digest,
        "observed_cardinality": result.observed_cardinality,
        "from_rows_per_to": result.from_rows_per_to,
        "to_rows_per_from": result.to_rows_per_from,
        "findings": [finding.model_dump() for finding in result.findings],
    }


def _report_data(
    config: object,
    model: object,
    catalogs: list[ScanCatalog],
    candidates: list[dict[str, object]],
    validations: list[ValidationResult],
) -> dict[str, object]:
    return {
        "sources": [
            {
                "source_id": catalog.source_id,
                "kind": config.sources[catalog.source_id].kind,
                "tables": [_table_document(table) for table in catalog.tables],
            }
            for catalog in catalogs
        ],
        "relationships": _model_document(model)["relationships"],
        "candidates": candidates,
        "validations": [_validation_document(result) for result in validations],
    }


def _report_data_from_documents(
    config: object,
    model: object,
    catalog: dict[str, object],
    candidates: dict[str, object],
    validations: dict[str, object],
) -> dict[str, object]:
    source_catalogs = catalog["sources"]
    candidate_rows = candidates["candidates"]
    validation_rows = validations["relationships"]
    if not all(isinstance(value, list) for value in (source_catalogs, candidate_rows, validation_rows)):
        raise TypeError("generated evidence has invalid collections")
    sources = [
        {
            "source_id": item["source_id"],
            "kind": config.sources[item["source_id"]].kind,
            "tables": item["tables"],
        }
        for item in source_catalogs
    ]
    return {
        "sources": sources,
        "relationships": _model_document(model)["relationships"],
        "candidates": candidate_rows,
        "validations": validation_rows,
    }


def _json_bytes(document: object) -> bytes:
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
