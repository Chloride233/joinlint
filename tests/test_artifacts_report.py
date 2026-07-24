from __future__ import annotations

import json
from pathlib import Path

import pytest

from joinlint import artifacts
from joinlint.artifacts import load_fresh_manifest, load_manifest, write_generated_transaction
from joinlint.errors import JoinLintError
from joinlint.report import render_report


def _files(scan_id: str) -> dict[str, bytes]:
    return {
        "manifest.json": json.dumps(
            {
                "schema_version": 1,
                "scan_id": scan_id,
                "source_fingerprints": ["source-one"],
                "model_digest": "model-one",
            }
        ).encode("utf-8"),
        "catalog.json": b'{"schema_version":1}',
    }


def test_failed_transaction_preserves_previous_generated_manifest(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_generated_transaction(project, _files("first"), {"identifiers": ["orders"]})

    def raise_disk_error(_path: Path, _body: bytes) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(artifacts, "write_file", raise_disk_error)
    with pytest.raises(JoinLintError) as captured:
        write_generated_transaction(project, _files("second"), {"identifiers": ["changed"]})

    assert captured.value.code == "GENERATED_WRITE_FAILED"
    assert load_manifest(project)["scan_id"] == "first"


def test_report_escapes_hostile_identifier_and_sets_csp() -> None:
    report = render_report(
        {
            "sources": [
                {
                    "source_id": "<img src=x onerror=alert(1)>",
                    "kind": "csv_directory",
                    "tables": [
                        {
                            "name": "orders",
                            "row_count": 1,
                            "columns": [
                                {
                                    "name": "id",
                                    "physical_type": "integer",
                                    "distinct_count": 1,
                                    "null_count": 0,
                                }
                            ],
                        }
                    ],
                }
            ],
            "relationships": [
                {"id": "orders_to_customers", "from": "orders.customer_id", "to": "customers.id", "cardinality": "many_to_one"}
            ],
            "candidates": [
                {
                    "id": "candidate_1",
                    "source_id": "sales",
                    "from_endpoint": "orders.customer_id",
                    "to_endpoint": "customers.id",
                    "cardinality": "many_to_one",
                    "confidence": "strong",
                    "evidence": {"matched_distinct_count": 2, "inclusion_numerator": 2, "inclusion_denominator": 2, "null_count": 0, "orphan_count": 0},
                }
            ],
            "validations": [
                {
                    "relationship_id": "orders_to_customers",
                    "observed_cardinality": "many_to_one",
                    "from_rows_per_to": 1,
                    "to_rows_per_from": 2,
                    "findings": [{"code": "GRAIN_CHANGE", "severity": "warning"}],
                }
            ],
        }
    )

    assert "<img src=x" not in report
    assert "default-src 'none'" in report
    assert "<script" not in report
    assert "Sources" in report
    assert "Confirmed relationships" in report
    assert "Candidates and evidence" in report
    assert "Validation evidence" in report
    assert "Risks" in report


def test_fresh_manifest_requires_all_matching_digests(project: Path) -> None:
    write_generated_transaction(project, _files("first"), {"identifiers": ["orders"]})

    manifest = load_fresh_manifest(project, ["source-one"], "model-one")
    assert manifest["scan_id"] == "first"
    with pytest.raises(JoinLintError) as captured:
        load_fresh_manifest(project, ["source-two"], "model-one")
    assert captured.value.code == "EVIDENCE_STALE"
