from __future__ import annotations

import html
from collections.abc import Iterable
from typing import Any


def render_report(data: dict[str, Any]) -> str:
    """Render a static report from sanitized metadata, never source rows."""
    sources = _records(data.get("sources"))
    relationships = _records(data.get("relationships"))
    candidates = _records(data.get("candidates"))
    validations = _records(data.get("validations"))
    risks = _risks(validations)
    sections = (
        _sources_section(sources)
        + _relationships_section(relationships)
        + _candidates_section(candidates)
        + _validation_section(validations)
        + _risks_section(risks)
    )
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta http-equiv=\"Content-Security-Policy\" "
        "content=\"default-src 'none'; style-src 'unsafe-inline'\">"
        "<title>JoinLint report</title>"
        "<style>body{font-family:system-ui,sans-serif;margin:2rem;line-height:1.4}"
        "table{border-collapse:collapse;margin:1rem 0 2rem;max-width:100%}"
        "th,td{border:1px solid #bbb;padding:.4rem;text-align:left;vertical-align:top}"
        "th{background:#f1f1f1}code{white-space:pre-wrap;overflow-wrap:anywhere}</style>"
        "</head><body><h1>JoinLint report</h1>"
        f"{sections}</body></html>"
    )


def _sources_section(sources: list[dict[str, Any]]) -> str:
    rows: list[list[object]] = []
    for source in _sorted_records(sources, "source_id"):
        source_id = source.get("source_id")
        kind = source.get("kind")
        for table in _sorted_records(_records(source.get("tables")), "name"):
            rows.append(
                [
                    source_id,
                    kind,
                    table.get("name"),
                    table.get("row_count"),
                    _columns(table),
                ]
            )
    return _section("Sources", ["Source", "Adapter", "Object", "Rows", "Columns"], rows)


def _relationships_section(relationships: list[dict[str, Any]]) -> str:
    rows = [
        [relationship.get("id"), relationship.get("from"), relationship.get("to"), relationship.get("cardinality")]
        for relationship in _sorted_records(relationships, "id")
    ]
    return _section("Confirmed relationships", ["ID", "From", "To", "Declared cardinality"], rows)


def _candidates_section(candidates: list[dict[str, Any]]) -> str:
    rows: list[list[object]] = []
    for candidate in _sorted_records(candidates, "id"):
        evidence = candidate.get("evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        rows.append(
            [
                candidate.get("id"),
                candidate.get("source_id"),
                candidate.get("from_endpoint"),
                candidate.get("to_endpoint"),
                candidate.get("cardinality"),
                candidate.get("confidence"),
                evidence.get("matched_distinct_count"),
                _ratio(evidence.get("inclusion_numerator"), evidence.get("inclusion_denominator")),
                evidence.get("null_count"),
                evidence.get("orphan_count"),
            ]
        )
    return _section(
        "Candidates and evidence",
        ["ID", "Source", "From", "To", "Cardinality", "Confidence", "Matched keys", "Inclusion", "Nulls", "Orphans"],
        rows,
    )


def _validation_section(validations: list[dict[str, Any]]) -> str:
    rows = [
        [
            validation.get("relationship_id"),
            validation.get("observed_cardinality"),
            validation.get("from_rows_per_to"),
            validation.get("to_rows_per_from"),
            _finding_codes(validation),
        ]
        for validation in _sorted_records(validations, "relationship_id")
    ]
    return _section(
        "Validation evidence",
        ["Relationship", "Observed cardinality", "From rows per to", "To rows per from", "Risks"],
        rows,
    )


def _risks_section(risks: list[dict[str, Any]]) -> str:
    rows = [[risk["relationship_id"], risk["severity"], risk["code"]] for risk in risks]
    return _section("Risks", ["Relationship", "Severity", "Code"], rows)


def _section(title: str, headings: list[str], rows: list[list[object]]) -> str:
    header = "".join(f"<th>{_text(heading)}</th>" for heading in headings)
    body = "".join(
        "<tr>" + "".join(f"<td><code>{_text(value)}</code></td>" for value in row) + "</tr>"
        for row in rows
    ) or f"<tr><td colspan=\"{len(headings)}\">None</td></tr>"
    return f"<section><h2>{_text(title)}</h2><table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table></section>"


def _columns(table: dict[str, Any]) -> str:
    columns = _sorted_records(_records(table.get("columns")), "name")
    return ", ".join(
        f"{_plain_text(column.get('name'))} ({_plain_text(column.get('physical_type'))}, "
        f"distinct={_plain_text(column.get('distinct_count'))}, nulls={_plain_text(column.get('null_count'))})"
        for column in columns
    )


def _ratio(numerator: object, denominator: object) -> str:
    return f"{_plain_text(numerator)}/{_plain_text(denominator)}"


def _finding_codes(validation: dict[str, Any]) -> str:
    return ", ".join(
        _plain_text(finding.get("code"))
        for finding in _sorted_records(_records(validation.get("findings")), "code")
    )


def _risks(validations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    risks = [
        {
            "relationship_id": validation.get("relationship_id"),
            "severity": finding.get("severity"),
            "code": finding.get("code"),
        }
        for validation in validations
        for finding in _records(validation.get("findings"))
    ]
    return sorted(
        risks,
        key=lambda risk: (
            _plain_text(risk["relationship_id"]).encode("utf-8"),
            _plain_text(risk["code"]).encode("utf-8"),
        ),
    )


def _records(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _sorted_records(records: Iterable[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    return sorted(records, key=lambda record: _plain_text(record.get(key)).encode("utf-8"))


def _text(value: object) -> str:
    return html.escape(_plain_text(value), quote=True)


def _plain_text(value: object) -> str:
    return "" if value is None else str(value)
