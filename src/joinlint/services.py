from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from joinlint.artifacts import load_fresh_manifest, write_generated_transaction
from joinlint.candidates import (
    POLICY_VERSION,
    RelationshipCandidate,
    accept_candidate,
    discover_candidates,
    reject_candidate,
    visible_candidates,
)
from joinlint.errors import JoinLintError
from joinlint.config import load_config
from joinlint.contracts import Envelope, Finding, canonical_json
from joinlint.model import Relationship, load_model, model_digest
from joinlint.scanner import ScanCatalog, scan_snapshot
from joinlint.snapshots import snapshot_source
from joinlint.validation import ValidationResult, validate_relationship


SCANNER_VERSION = "0.1.0"


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
    catalogs: list[ScanCatalog] = []
    candidates: list[dict[str, object]] = []
    source_fingerprints: list[str] = []
    report_identifiers: list[str] = []
    validation_results: list[ValidationResult] = []
    relationships_by_source = _relationships_by_source(model)
    for source_id in sorted(config.sources, key=lambda value: value.encode("utf-8")):
        with snapshot_source(project, source_id) as snapshot:
            catalog = scan_snapshot(snapshot)
            catalogs.append(catalog)
            source_fingerprints.append(snapshot.fingerprint)
            report_identifiers.extend(f"{source_id}.{table.name}" for table in catalog.tables)
            candidates.extend(
                _candidate_document(candidate)
                for candidate in discover_candidates(snapshot, catalog, model)
            )
            validation_results.extend(
                validate_relationship(relationship, snapshot, catalog)
                for relationship in relationships_by_source.get(source_id, [])
            )
    digest = model_digest(model)
    scan_id = hashlib.sha256(
        canonical_json(
            {
                "source_fingerprints": source_fingerprints,
                "model_digest": digest,
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
    write_generated_transaction(project, files, {"identifiers": report_identifiers})
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
    relationships_by_source = _relationships_by_source(model)

    results: list[ValidationResult] = []
    schemas: list[dict[str, object]] = []
    source_fingerprints: list[str] = []
    for source_id in sorted(config.sources, key=lambda value: value.encode("utf-8")):
        with snapshot_source(project, source_id) as snapshot:
            catalog = scan_snapshot(snapshot)
            schemas.append(_catalog_document(catalog))
            source_fingerprints.append(snapshot.fingerprint)
            results.extend(
                validate_relationship(relationship, snapshot, catalog)
                for relationship in relationships_by_source.get(source_id, [])
            )
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


def get_data_model(project: Path) -> Envelope:
    model = load_model(project)
    return Envelope(command="get_data_model", status="ok", data=model.model_dump(mode="json", by_alias=True))


def find_join_path(project: Path, source_entity: str, target_entity: str, max_depth: int) -> Envelope:
    if not 1 <= max_depth <= 4:
        raise JoinLintError("INVALID_DEPTH", "max_depth must be between 1 and 4", 2)
    model = load_model(project)
    if source_entity not in model.entities or target_entity not in model.entities:
        raise JoinLintError("ENTITY_NOT_FOUND", "entity is not confirmed", 2)
    paths: list[list[dict[str, str]]] = []
    queue: list[tuple[str, list[dict[str, str]], set[str]]] = [(source_entity, [], {source_entity})]
    while queue and len(paths) < 20:
        entity, path, seen = queue.pop(0)
        if entity == target_entity and path:
            paths.append(path)
            continue
        if len(path) == max_depth:
            continue
        for relationship in model.relationships:
            from_entity = relationship.from_.split(".", maxsplit=1)[0]
            to_entity = relationship.to.split(".", maxsplit=1)[0]
            if entity == from_entity and to_entity not in seen:
                queue.append((to_entity, [*path, _path_edge(relationship, "forward")], seen | {to_entity}))
            if entity == to_entity and from_entity not in seen:
                queue.append((from_entity, [*path, _path_edge(relationship, "reverse")], seen | {from_entity}))
    return Envelope(command="find_join_path", status="ok", data={"paths": paths})


def validate_cached_edges(project: Path, edge_ids: list[str]) -> Envelope:
    if not edge_ids or len(edge_ids) > 16:
        raise JoinLintError("INVALID_ARGUMENT", "edge_ids must contain between 1 and 16 IDs", 2)
    model = load_model(project)
    _require_fresh_manifest(project, model)
    try:
        document = json.loads((project / ".joinlint" / "generated" / "validation.json").read_text(encoding="utf-8"))
        results = {item["relationship_id"]: item for item in document["relationships"]}
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise JoinLintError("EVIDENCE_STALE", "validation evidence is unavailable", 3) from exc
    if any(edge_id not in {relationship.id for relationship in model.relationships} or edge_id not in results for edge_id in edge_ids):
        raise JoinLintError("EVIDENCE_STALE", "validation evidence is stale", 3)
    findings = [
        Finding.model_validate(finding)
        for edge_id in edge_ids
        for finding in results[edge_id]["findings"]
    ]
    findings.sort(key=lambda finding: finding.code.encode("utf-8"))
    return Envelope(command="validate_join", status="findings" if findings else "ok", data={"relationships": [results[item] for item in edge_ids]}, findings=findings)


def _relationships_by_source(model: object) -> dict[str, list[Relationship]]:
    relationships_by_source: dict[str, list[Relationship]] = {}
    for relationship in model.relationships:
        from_entity = model.entities[relationship.from_.split(".", maxsplit=1)[0]]
        to_entity = model.entities[relationship.to.split(".", maxsplit=1)[0]]
        if from_entity.source != to_entity.source:
            raise JoinLintError("RELATIONSHIP_CROSS_SOURCE_UNSUPPORTED", "cross-source relationship validation is not available", 2)
        relationships_by_source.setdefault(from_entity.source, []).append(relationship)
    return relationships_by_source


def _require_fresh_manifest(project: Path, model: object) -> None:
    config = load_config(project)
    fingerprints: list[str] = []
    for source_id in sorted(config.sources, key=lambda value: value.encode("utf-8")):
        with snapshot_source(project, source_id) as snapshot:
            fingerprints.append(snapshot.fingerprint)
    load_fresh_manifest(project, fingerprints, model_digest(model))


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
    candidates: list[RelationshipCandidate] = []
    source_fingerprints: list[str] = []
    for source_id in sorted(config.sources, key=lambda value: value.encode("utf-8")):
        with snapshot_source(project, source_id) as snapshot:
            catalog = scan_snapshot(snapshot)
            source_fingerprints.append(snapshot.fingerprint)
            if include_rejected:
                candidates.extend(discover_candidates(snapshot, catalog, model))
            else:
                candidates.extend(visible_candidates(project, snapshot, catalog, model))
    load_fresh_manifest(project, source_fingerprints, model_digest(model))
    return sorted(candidates, key=lambda candidate: candidate.id.encode("utf-8"))


def _catalog_document(catalog: ScanCatalog) -> dict[str, object]:
    return {"source_id": catalog.source_id, "tables": [asdict(table) for table in catalog.tables]}


def _candidate_document(candidate: RelationshipCandidate) -> dict[str, object]:
    return asdict(candidate)


def _validation_document(result: ValidationResult) -> dict[str, object]:
    return {
        "relationship_id": result.relationship_id,
        "observed_cardinality": result.observed_cardinality,
        "from_rows_per_to": result.from_rows_per_to,
        "to_rows_per_from": result.to_rows_per_from,
        "findings": [finding.model_dump() for finding in result.findings],
    }


def _json_bytes(document: object) -> bytes:
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
