from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from benchmarks.formal_eval.cli import main
from benchmarks.formal_eval.contracts import (
    FormalManifestV2,
    FormalTask,
    InputLockV2,
    SealedAgentTask,
)
from benchmarks.formal_eval.fake import fake_manifest, fake_preregistration
from benchmarks.formal_eval.manifest import (
    require_locked_inputs,
    semantic_fingerprint,
    validate_manifest,
    verify_input_lock,
    verify_sealed_manifest,
    verify_sealed_task_hashes,
)


def test_fake_manifest_meets_formal_shape_without_cross_split_leakage() -> None:
    readiness = validate_manifest(fake_manifest(), fake_preregistration())

    assert readiness.ready
    assert readiness.confirmatory_task_count == 60
    assert readiness.confirmatory_database_count == 12
    assert readiness.diagnostic_task_count == 20


def test_manifest_rejects_exact_task_duplicate() -> None:
    manifest = fake_manifest()
    tasks = list(manifest.tasks)
    first = tasks[0]
    payload = tasks[1].model_dump(mode="python")
    payload["question_sha256"] = first.question_sha256
    payload["sql_shape_sha256"] = first.sql_shape_sha256
    tasks[1] = tasks[1].__class__.model_validate(payload)
    duplicated = FormalManifestV2(dataset_release=manifest.dataset_release, tasks=tuple(tasks))

    readiness = validate_manifest(duplicated, fake_preregistration())

    assert not readiness.ready
    assert "EXACT_TASK_DUPLICATE" in readiness.findings


def test_manifest_rejects_near_duplicate_template_and_sql_structure() -> None:
    manifest = fake_manifest()
    tasks = list(manifest.tasks)
    first = tasks[0]
    payload = tasks[1].model_dump(mode="python")
    payload["schema_family_id"] = first.schema_family_id
    payload["question_template_id"] = first.question_template_id
    payload["sql_structure_id"] = first.sql_structure_id
    tasks[1] = tasks[1].__class__.model_validate(payload)
    duplicated = FormalManifestV2(dataset_release=manifest.dataset_release, tasks=tuple(tasks))

    readiness = validate_manifest(duplicated, fake_preregistration())

    assert not readiness.ready
    assert "NEAR_TASK_DUPLICATE" in readiness.findings


def test_input_lock_detects_tampering(tmp_path: Path) -> None:
    source = tmp_path / "sealed.json"
    source.write_text("frozen", encoding="utf-8")
    lock = InputLockV2(files={"sealed.json": hashlib.sha256(b"frozen").hexdigest()})
    verify_input_lock(lock, tmp_path)

    source.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_input_lock(lock, tmp_path)


def test_preflight_rejects_required_input_missing_from_lock(tmp_path: Path) -> None:
    frozen = tmp_path / "frozen.json"
    omitted = tmp_path / "omitted.json"
    frozen.write_text("frozen", encoding="utf-8")
    omitted.write_text("omitted", encoding="utf-8")
    lock = InputLockV2(files={"frozen.json": hashlib.sha256(b"frozen").hexdigest()})

    require_locked_inputs(lock, tmp_path, [frozen])
    with pytest.raises(ValueError, match="required input is not frozen"):
        require_locked_inputs(lock, tmp_path, [omitted])


def test_sealed_task_hash_check_rejects_payload_drift() -> None:
    task = sealed_task()

    def digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    verify_sealed_task_hashes(
        digest(task.question),
        digest(task.schema_text),
        digest(task.sql_shape),
        task,
    )
    with pytest.raises(ValueError, match="sealed task hash mismatch"):
        verify_sealed_task_hashes(
            digest("different question"),
            digest(task.schema_text),
            digest(task.sql_shape),
            task,
        )


def test_semantic_fingerprint_normalizes_literals_and_numbers() -> None:
    first = semantic_fingerprint("question", "Top 10 orders for 'Alice'")
    second = semantic_fingerprint("question", 'Top 20 orders for "Bob"')

    assert first == second
    assert first != semantic_fingerprint("question", "Bottom 20 orders for 'Bob'")


def test_sql_structure_fingerprint_normalizes_identifiers_aliases_and_literals() -> None:
    first = semantic_fingerprint(
        "sql",
        "SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id "
        "WHERE c.name = 'Alice' LIMIT 10",
    )
    second = semantic_fingerprint(
        "sql",
        "SELECT x.key FROM sales x JOIN users y ON x.user_id = y.key "
        "WHERE y.label = 'Bob' LIMIT 20",
    )

    assert first == second
    assert first != semantic_fingerprint(
        "sql",
        "SELECT x.key FROM sales x LEFT JOIN users y ON x.user_id = y.key "
        "WHERE y.label = 'Bob' LIMIT 20",
    )


def test_sealed_manifest_rejects_arbitrary_near_duplicate_fingerprint() -> None:
    task = sealed_task()
    manifest_task = formal_task(task)
    payload = manifest_task.model_dump(mode="python")
    payload["question_template_id"] = "arbitrary-cluster"
    manifest = FormalManifestV2(
        dataset_release="release-1",
        tasks=(FormalTask.model_validate(payload),),
    )

    with pytest.raises(ValueError, match="near-duplicate fingerprint mismatch"):
        verify_sealed_manifest(manifest, [task])


def test_sealed_manifest_rejects_contract_mismatch() -> None:
    task = sealed_task()
    manifest_task = formal_task(task)
    payload = manifest_task.model_dump(mode="python")
    payload["database_id"] = "different-database"
    manifest = FormalManifestV2(
        dataset_release="release-1",
        tasks=(FormalTask.model_validate(payload),),
    )

    with pytest.raises(ValueError, match="sealed task contract mismatch"):
        verify_sealed_manifest(manifest, [task])


def sealed_task() -> SealedAgentTask:
    return SealedAgentTask(
        task_id="task-1",
        database_id="database-1",
        question="Which customers ordered?",
        schema_text="orders(customer_id), customers(id)",
        schema={
            "orders": {"customer_id": "INTEGER"},
            "customers": {"id": "INTEGER"},
        },
        sql_shape="orders JOIN customers",
        gold_sql=(
            "SELECT * FROM orders JOIN customers "
            "ON orders.customer_id = customers.id"
        ),
        database_path="databases/database-1.sqlite",
        expected_entities=("orders", "customers"),
        allowed_graphs=((("orders.customer_id", "customers.id"),),),
        oracle_has_safe_path=True,
    )


def formal_task(task: SealedAgentTask) -> FormalTask:
    def digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    return FormalTask(
        task_id=task.task_id,
        database_id=task.database_id,
        database_variant_group="database-family-1",
        corpus="natural",
        split="confirmatory",
        domain="retail",
        source_type="sqlite",
        database_scale="small",
        ambiguity="none",
        fanout_type="one_to_many",
        question_sha256=digest(task.question),
        schema_sha256=digest(task.schema_text),
        sql_shape_sha256=digest(task.sql_shape),
        schema_family_id=semantic_fingerprint("schema", task.schema_text),
        question_template_id=semantic_fingerprint("question", task.question),
        sql_structure_id=semantic_fingerprint("sql", task.gold_sql),
        allowed_graphs=task.allowed_graphs,
        oracle_has_safe_path=task.oracle_has_safe_path,
        join_depth=1,
    )


def test_fake_model_cli_writes_rebuildable_reports(tmp_path: Path) -> None:
    output = tmp_path / "fake"

    assert main(["fake-model", "--output", str(output)]) == 0

    report = json.loads((output / "formal-evaluation.json").read_text(encoding="utf-8"))
    assert report["evaluation_id"] == "synthetic-pipeline-smoke"
    assert report["provenance"]["model_identities"] == [
        "fake/high-capability-v1",
        "fake/cost-efficient-v1",
    ]
    assert report["sql_validation"]["performance"]["cached_plan_p95_ms"] == 100
    assert report["evidence_class"] == "synthetic_non_evidentiary"
    assert report["public_quantitative_claim_allowed"] is False
    markdown = (output / "formal-evaluation.md").read_text(encoding="utf-8")
    assert "Join correctness is not proof" in markdown
    assert "SYNTHETIC_NON_EVIDENTIARY" in markdown
