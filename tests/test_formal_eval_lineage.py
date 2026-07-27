from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks.formal_eval.contracts import InputLockV2
from benchmarks.formal_eval.cli import main
from benchmarks.formal_eval.contracts import AgentResultBundle
from benchmarks.formal_eval.deterministic import DeterministicSuite, PerformanceSpec
from benchmarks.formal_eval.fake import (
    fake_agent_rows,
    fake_blind_review,
    fake_deterministic_evidence,
    fake_manifest,
    fake_preregistration,
)
from benchmarks.formal_eval.lineage import build_lineage, digest_value, verify_lineage
from benchmarks.formal_eval.manifest import load_document
from benchmarks.formal_eval.run_plan import build_run_plan
from joinlint.contracts import canonical_json


def test_lineage_changes_when_any_frozen_identity_changes() -> None:
    preregistration = fake_preregistration()
    manifest = fake_manifest()
    lock = InputLockV2(files={"manifest.json": "a" * 64})
    original = build_lineage(preregistration, manifest, lock)
    changed_image_digest = f"sha256:{'9' * 64}"
    preregistration_variants = (
        preregistration.model_copy(update={"evaluation_id": "different-evaluation"}),
        preregistration.model_copy(update={"dataset_release": "different-release"}),
        preregistration.model_copy(update={"joinlint_commit": "9" * 40}),
        preregistration.model_copy(
            update={
                "image_digest": changed_image_digest,
                "image_reference": f"ghcr.io/example/joinlint-formal@{changed_image_digest}",
            }
        ),
        preregistration.model_copy(
            update={"inference_policy_version": "different-policy"}
        ),
    )
    variants = [
        build_lineage(changed, manifest, lock)
        for changed in preregistration_variants
    ]
    variants.append(
        build_lineage(
            preregistration,
            manifest.model_copy(update={"dataset_release": "manifest-change"}),
            lock,
        )
    )
    variants.append(
        build_lineage(
            preregistration,
            manifest,
            InputLockV2(files={"manifest.json": "b" * 64}),
        )
    )

    assert all(updated.lineage_id != original.lineage_id for updated in variants)
    for updated in variants:
        with pytest.raises(ValueError, match="lineage mismatch"):
            verify_lineage(original, updated.lineage_id)


def test_strict_json_document_round_trips_tuple_contracts(tmp_path: Path) -> None:
    manifest = fake_manifest()
    path = tmp_path / "manifest.json"
    path.write_bytes(canonical_json(manifest.model_dump(mode="json")))

    loaded = load_document(path, type(manifest))

    assert loaded == manifest


def test_score_rejects_deterministic_evidence_from_another_lineage(tmp_path: Path) -> None:
    preregistration = fake_preregistration().model_copy(update={"bootstrap_draws": 1_000})
    manifest = fake_manifest()
    lock = InputLockV2(files={"frozen.json": "a" * 64})
    lineage = build_lineage(preregistration, manifest, lock)
    run_plan = build_run_plan(manifest, preregistration, lineage.lineage_id)
    agent_bundle = AgentResultBundle(
        lineage_id=lineage.lineage_id,
        run_plan_sha256=digest_value(run_plan.model_dump(mode="json")),
        rows=tuple(fake_agent_rows()),
    )
    review = fake_blind_review(agent_bundle, run_plan)
    evidence = fake_deterministic_evidence().model_copy(update={"lineage_id": "f" * 64})
    suite = fake_deterministic_suite()

    documents = {
        "preregistration.json": preregistration,
        "manifest.json": manifest,
        "input-lock.json": lock,
        "run-plan.json": run_plan,
        "agent-results.json": agent_bundle,
        "blind-review.json": review,
        "deterministic-evidence.json": evidence,
        "deterministic-suite.json": suite,
    }
    for name, document in documents.items():
        (tmp_path / name).write_bytes(canonical_json(document.model_dump(mode="json")))

    with pytest.raises(ValueError, match="lineage mismatch"):
        main(
            [
                "score",
                "--preregistration",
                str(tmp_path / "preregistration.json"),
                "--manifest",
                str(tmp_path / "manifest.json"),
                "--input-lock",
                str(tmp_path / "input-lock.json"),
                "--deterministic-evidence",
                str(tmp_path / "deterministic-evidence.json"),
                "--deterministic-suite",
                str(tmp_path / "deterministic-suite.json"),
                "--run-plan",
                str(tmp_path / "run-plan.json"),
                "--agent-results",
                str(tmp_path / "agent-results.json"),
                "--blind-review",
                str(tmp_path / "blind-review.json"),
                "--output",
                str(tmp_path / "report"),
            ]
        )


def test_score_rejects_deterministic_evidence_for_another_raw_suite(
    tmp_path: Path,
) -> None:
    preregistration = fake_preregistration().model_copy(update={"bootstrap_draws": 1_000})
    manifest = fake_manifest()
    lock = InputLockV2(files={"frozen.json": "a" * 64})
    lineage = build_lineage(preregistration, manifest, lock)
    evidence = fake_deterministic_evidence().model_copy(
        update={"lineage_id": lineage.lineage_id}
    )
    documents = {
        "preregistration.json": preregistration,
        "manifest.json": manifest,
        "input-lock.json": lock,
        "deterministic-evidence.json": evidence,
        "deterministic-suite.json": fake_deterministic_suite(),
    }
    for name, document in documents.items():
        (tmp_path / name).write_bytes(canonical_json(document.model_dump(mode="json")))

    with pytest.raises(ValueError, match="does not match the frozen raw suite"):
        main(
            [
                "score",
                "--preregistration",
                str(tmp_path / "preregistration.json"),
                "--manifest",
                str(tmp_path / "manifest.json"),
                "--input-lock",
                str(tmp_path / "input-lock.json"),
                "--deterministic-evidence",
                str(tmp_path / "deterministic-evidence.json"),
                "--deterministic-suite",
                str(tmp_path / "deterministic-suite.json"),
                "--run-plan",
                str(tmp_path / "missing-run-plan.json"),
                "--agent-results",
                str(tmp_path / "missing-results.json"),
                "--output",
                str(tmp_path / "report"),
            ]
        )


def fake_deterministic_suite() -> DeterministicSuite:
    return DeterministicSuite(
        relationship_scope="verified_inference",
        relationship_cases=(),
        sql_validation_cases=(),
        performance=PerformanceSpec(
            project_path="sealed/project",
            entities=("orders", "customers"),
            safe_sql="SELECT 1",
            million_row_project_path="sealed/million-row-project",
            million_row_sql="SELECT 1",
            repeats=5,
        ),
    )
