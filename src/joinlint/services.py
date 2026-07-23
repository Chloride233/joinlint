from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
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
from joinlint.contracts import Envelope, canonical_json
from joinlint.model import load_model, model_digest
from joinlint.scanner import ScanCatalog, scan_snapshot
from joinlint.snapshots import snapshot_source


SCANNER_VERSION = "0.1.0"


def scan_project(project: Path) -> Envelope:
    """Scan every registered source and publish one generated-evidence transaction."""
    config = load_config(project)
    model = load_model(project)
    catalogs: list[ScanCatalog] = []
    candidates: list[dict[str, object]] = []
    source_fingerprints: list[str] = []
    report_identifiers: list[str] = []
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


def _json_bytes(document: object) -> bytes:
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
