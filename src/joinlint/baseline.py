from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
from typing import Any

from joinlint.contracts import Finding
from joinlint.errors import JoinLintError
from joinlint.paths import SafeProject


class BaselineValidationError(JoinLintError):
    def __init__(self, findings: list[Finding]) -> None:
        super().__init__("BASELINE_VALIDATION_FAILED", "blocking findings prevent baseline update", 1)
        self.findings = findings


def load_baseline(project: Path) -> dict[str, Any]:
    try:
        with SafeProject(project) as safe_project:
            descriptor = safe_project.open_relative(PurePosixPath(".joinlint/baseline.json"), os.O_RDONLY)
            with os.fdopen(descriptor, "r", encoding="utf-8") as source:
                baseline: Any = json.load(source)
    except JoinLintError as exc:
        if exc.code == "SAFE_PATH_OPEN_FAILED":
            raise JoinLintError("BASELINE_MISSING", "baseline.json has not been created", 3) from exc
        raise JoinLintError("MALFORMED_BASELINE", "baseline.json is invalid", 2) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise JoinLintError("MALFORMED_BASELINE", "baseline.json is invalid", 2) from exc
    if not _is_valid_baseline(baseline):
        raise JoinLintError("MALFORMED_BASELINE", "baseline.json has an unsupported schema", 2)
    return baseline


def update_baseline(project: Path) -> dict[str, Any]:
    from joinlint.services import collect_current_evidence

    evidence = collect_current_evidence(project)
    if any(finding.severity == "blocking" for finding in evidence.findings):
        raise BaselineValidationError(evidence.findings)
    baseline = make_baseline(evidence)
    _write_baseline(project, baseline)
    return baseline


def make_baseline(evidence: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "source_fingerprints": list(evidence.source_fingerprints),
        "schemas": evidence.schemas,
        "relationship_results": evidence.relationship_results,
    }


def compare_baseline(baseline: dict[str, Any], evidence: Any) -> list[Finding]:
    findings: list[Finding] = []
    if baseline.get("schemas") != evidence.schemas:
        findings.append(Finding(code="SCHEMA_DRIFT", severity="blocking", message="SCHEMA_DRIFT"))
    expected = {item["relationship_id"]: item for item in baseline.get("relationship_results", [])}
    actual = {item["relationship_id"]: item for item in evidence.relationship_results}
    for relationship_id in sorted(expected.keys() | actual.keys(), key=lambda value: value.encode("utf-8")):
        if expected.get(relationship_id) != actual.get(relationship_id):
            findings.append(Finding(code="CARDINALITY_DRIFT", severity="blocking", message="CARDINALITY_DRIFT"))
            break
    return findings


def _is_valid_baseline(baseline: Any) -> bool:
    if not isinstance(baseline, dict) or baseline.get("version") != 1:
        return False
    source_fingerprints = baseline.get("source_fingerprints")
    schemas = baseline.get("schemas")
    results = baseline.get("relationship_results")
    return (
        isinstance(source_fingerprints, list)
        and all(isinstance(value, str) for value in source_fingerprints)
        and isinstance(schemas, list)
        and all(isinstance(schema, dict) for schema in schemas)
        and isinstance(results, list)
        and all(isinstance(result, dict) and isinstance(result.get("relationship_id"), str) for result in results)
        and len({result["relationship_id"] for result in results}) == len(results)
    )


def _write_baseline(project: Path, baseline: dict[str, Any]) -> None:
    payload = json.dumps(baseline, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    with SafeProject(project) as safe_project:
        safe_project.write_relative_atomically(PurePosixPath(".joinlint/baseline.json"), payload)
