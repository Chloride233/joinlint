from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from joinlint.contracts import Finding
from joinlint.errors import JoinLintError


class BaselineValidationError(JoinLintError):
    def __init__(self, findings: list[Finding]) -> None:
        super().__init__("BASELINE_VALIDATION_FAILED", "blocking findings prevent baseline update", 1)
        self.findings = findings


def load_baseline(project: Path) -> dict[str, Any]:
    path = project / ".joinlint" / "baseline.json"
    if not path.exists():
        raise JoinLintError("BASELINE_MISSING", "baseline.json has not been created", 3)
    try:
        baseline: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JoinLintError("MALFORMED_BASELINE", "baseline.json is invalid", 2) from exc
    if not isinstance(baseline, dict) or baseline.get("version") != 1:
        raise JoinLintError("MALFORMED_BASELINE", "baseline.json has an unsupported schema", 2)
    return baseline


def update_baseline(project: Path) -> dict[str, Any]:
    from joinlint.services import collect_current_evidence

    evidence = collect_current_evidence(project)
    if any(finding.severity == "blocking" for finding in evidence.findings):
        raise BaselineValidationError(evidence.findings)
    baseline = make_baseline(evidence)
    _write_baseline(project / ".joinlint" / "baseline.json", baseline)
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


def _write_baseline(path: Path, baseline: dict[str, Any]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".baseline.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            json.dump(baseline, destination, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise
