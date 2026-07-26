from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from benchmarks.formal_eval.contracts import FormalManifestV2, SealedAgentTask
from benchmarks.formal_eval.manifest import load_document, verify_sealed_manifest
from benchmarks.formal_eval.semantic_failure import build_semantic_failure_v1


def test_semantic_failure_bundle_is_deterministic_and_self_validating(tmp_path: Path) -> None:
    first_root = tmp_path / "one" / "sealed"
    second_root = tmp_path / "two" / "sealed"
    first_root.mkdir(parents=True)
    second_root.mkdir(parents=True)
    first = first_root / "semantic-join-failure-v1"
    second = second_root / "semantic-join-failure-v1"

    first_report = build_semantic_failure_v1(first_root, first)
    second_report = build_semantic_failure_v1(second_root, second)

    assert first_report == second_report
    for name in ("agent-tasks.json", "manifest.json", "diagnostic-catalog.json"):
        assert (first / name).read_bytes() == (second / name).read_bytes()
    for database in first_report["databases"]:
        first_database = first / database["path"]
        second_database = second / database["path"]
        assert first_database.read_bytes() == second_database.read_bytes()
        assert not first_database.with_name(first_database.name + "-wal").exists()
        assert not first_database.with_name(first_database.name + "-shm").exists()

    tasks = TypeAdapter(list[SealedAgentTask]).validate_json(
        (first / "agent-tasks.json").read_bytes()
    )
    manifest = load_document(first / "manifest.json", FormalManifestV2)
    verify_sealed_manifest(manifest, tasks)
    catalog = json.loads((first / "diagnostic-catalog.json").read_bytes())

    assert len(tasks) == len(manifest.tasks) == len(catalog) == 20
    assert len({task.database_id for task in tasks}) == 4
    assert all(task.corpus == "semantic_join_failure" for task in manifest.tasks)
    assert all(task.split == "diagnostic" for task in manifest.tasks)
    assert all(row["dangerous_result_differs_from_gold"] for row in catalog)
