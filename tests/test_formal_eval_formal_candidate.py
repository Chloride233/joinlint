from __future__ import annotations

from benchmarks.formal_eval.fake import fake_manifest
from benchmarks.formal_eval.formal_candidate import _structural_findings


def test_fake_complete_manifest_meets_candidate_structure() -> None:
    assert _structural_findings(fake_manifest()) == ()


def test_candidate_structure_rejects_cross_split_database_variant() -> None:
    manifest = fake_manifest()
    tasks = list(manifest.tasks)
    diagnostic_index = next(index for index, task in enumerate(tasks) if task.split == "diagnostic")
    tasks[diagnostic_index] = tasks[diagnostic_index].model_copy(
        update={"database_variant_group": tasks[0].database_variant_group}
    )

    changed = manifest.model_copy(update={"tasks": tuple(tasks)})

    assert "DATABASE_VARIANT_SPLIT_LEAKAGE" in _structural_findings(changed)
