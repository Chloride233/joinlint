from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from joinlint.candidates import normalize_value, snapshot_values
from joinlint.contracts import Finding
from joinlint.errors import JoinLintError
from joinlint.model import ModelV1, Relationship, entity_table_name
from joinlint.scanner import ScanCatalog
from joinlint.snapshots import SourceSnapshot


@dataclass(frozen=True)
class ValidationResult:
    relationship_id: str
    observed_cardinality: str
    from_rows_per_to: int
    to_rows_per_from: int
    findings: tuple[Finding, ...]


@dataclass(frozen=True)
class PathValidationResult:
    results: tuple[ValidationResult, ...]
    findings: tuple[Finding, ...]


def validate_relationship(
    relationship: Relationship, snapshot: SourceSnapshot, catalog: ScanCatalog, model: ModelV1 | None = None
) -> ValidationResult:
    from_table, from_column = _endpoint(relationship.from_, snapshot, catalog, model)
    to_table, to_column = _endpoint(relationship.to, snapshot, catalog, model)
    values = snapshot_values(snapshot)
    from_values = values[from_table][from_column]
    to_values = values[to_table][to_column]
    normalized_from = [normalize_value(value) for value in from_values if value not in (None, "")]
    normalized_to = [normalize_value(value) for value in to_values if value not in (None, "")]
    from_counts = Counter(normalized_from)
    to_counts = Counter(normalized_to)
    matched_values = from_counts.keys() & to_counts.keys()
    from_rows_per_to = max((from_counts[value] for value in matched_values), default=0)
    to_rows_per_from = max((to_counts[value] for value in matched_values), default=0)
    observed = _cardinality(from_rows_per_to, to_rows_per_from)
    findings: list[Finding] = []
    if not catalog.table(to_table).column(to_column).is_unique:
        findings.append(_finding("REFERENCED_KEY_NOT_UNIQUE", "blocking"))
    null_count = len(from_values) - len(normalized_from)
    if null_count:
        findings.append(_finding("CHILD_KEY_NULL", "warning"))
    orphan_count = sum(count for value, count in from_counts.items() if value not in to_counts)
    if orphan_count:
        findings.append(_finding("ORPHAN_CHILD_ROW", "blocking"))
    if observed != relationship.cardinality:
        findings.append(_finding("CARDINALITY_DRIFT", "blocking"))
    if observed != "one_to_one":
        findings.append(_finding("GRAIN_CHANGE", "warning"))
    if observed == "many_to_many":
        findings.append(_finding("MANY_TO_MANY_FANOUT", "blocking"))
    return ValidationResult(
        relationship_id=relationship.id,
        observed_cardinality=observed,
        from_rows_per_to=from_rows_per_to,
        to_rows_per_from=to_rows_per_from,
        findings=tuple(sorted(findings, key=lambda finding: finding.code.encode("utf-8"))),
    )


def validate_path(
    relationships: list[Relationship], snapshot: SourceSnapshot, catalog: ScanCatalog, model: ModelV1 | None = None
) -> PathValidationResult:
    results = tuple(validate_relationship(relationship, snapshot, catalog, model) for relationship in relationships)
    findings = {finding.code: finding for result in results for finding in result.findings}
    fanout_edges = sum(
        result.from_rows_per_to > 1 or result.to_rows_per_from > 1 for result in results
    )
    if fanout_edges > 1:
        findings["COMPOUND_FANOUT"] = _finding("COMPOUND_FANOUT", "blocking")
    return PathValidationResult(
        results=results,
        findings=tuple(sorted(findings.values(), key=lambda finding: finding.code.encode("utf-8"))),
    )


def _endpoint(
    endpoint: str, snapshot: SourceSnapshot, catalog: ScanCatalog, model: ModelV1 | None
) -> tuple[str, str]:
    entity_id, column_name = endpoint.split(".", maxsplit=1)
    table_name = entity_id
    if model is not None:
        entity = model.entities.get(entity_id)
        if entity is None or entity.source != snapshot.source_id:
            raise JoinLintError("MODEL_REFERENCE_MISSING", "relationship entity is missing from source", 2)
        table_name = entity_table_name(entity, snapshot.kind)
    try:
        catalog.table(table_name).column(column_name)
    except KeyError as exc:
        raise JoinLintError("MODEL_REFERENCE_MISSING", "relationship endpoint is missing from source", 2) from exc
    return table_name, column_name


def _cardinality(from_rows_per_to: int, to_rows_per_from: int) -> str:
    if from_rows_per_to <= 1 and to_rows_per_from <= 1:
        return "one_to_one"
    if from_rows_per_to <= 1:
        return "one_to_many"
    if to_rows_per_from <= 1:
        return "many_to_one"
    return "many_to_many"


def _finding(code: str, severity: str) -> Finding:
    return Finding(code=code, severity=severity, message=code)
