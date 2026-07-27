from __future__ import annotations

import argparse
import json
import os
import shutil
import uuid
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

import rfc8785
from pydantic import TypeAdapter

from benchmarks.formal_eval.bird_dataset import _file_record
from benchmarks.formal_eval.contracts import FormalManifestV2, SealedAgentTask
from benchmarks.formal_eval.manifest import load_document, verify_sealed_manifest


DATASET_RELEASE = "joinlint-bird-declared-v1-semantic-join-failure-v1"


def build_formal_candidate(
    natural_root: Path,
    diagnostic_root: Path,
    sealed_root: Path,
    output: Path,
) -> dict[str, Any]:
    resolved_sealed = _real_directory(sealed_root, "sealed root")
    resolved_natural = _inside_root(natural_root, resolved_sealed, "natural input")
    resolved_diagnostic = _inside_root(diagnostic_root, resolved_sealed, "diagnostic input")
    resolved_parent = _inside_root(output.parent, resolved_sealed, "candidate output parent")
    output = resolved_parent / output.name
    if output.exists() or output.is_symlink():
        raise ValueError("formal candidate output already exists")

    natural_tasks, natural_manifest = _load_component(resolved_natural)
    diagnostic_tasks, diagnostic_manifest = _load_component(resolved_diagnostic)
    tasks = sorted(
        [*natural_tasks, *diagnostic_tasks],
        key=lambda task: task.task_id.encode("utf-8"),
    )
    manifest = FormalManifestV2(
        dataset_release=DATASET_RELEASE,
        tasks=tuple(
            sorted(
                [*natural_manifest.tasks, *diagnostic_manifest.tasks],
                key=lambda task: task.task_id.encode("utf-8"),
            )
        ),
    )
    verify_sealed_manifest(manifest, tasks)
    findings = _structural_findings(manifest)
    if findings:
        raise ValueError(f"formal candidate is structurally invalid: {','.join(findings)}")
    for task in tasks:
        _verify_database_path(resolved_sealed, task.database_path)

    staging = output.with_name(f".{output.name}.staging-{uuid.uuid4().hex}")
    staging.mkdir()
    try:
        tasks_path = staging / "agent-tasks.json"
        manifest_path = staging / "manifest.json"
        _write_canonical(
            tasks_path,
            [task.model_dump(mode="json", by_alias=True) for task in tasks],
        )
        _write_canonical(manifest_path, manifest.model_dump(mode="json"))
        report = {
            "schema_version": 1,
            "status": "candidate_inputs_not_preregistered",
            "dataset_release": DATASET_RELEASE,
            "confirmatory_task_count": 60,
            "confirmatory_database_count": 12,
            "diagnostic_task_count": 20,
            "diagnostic_database_count": 4,
            "agent_tasks": _file_record(tasks_path),
            "manifest": _file_record(manifest_path),
            "natural_component_manifest_sha256": _file_record(
                resolved_natural / "manifest.json"
            )["sha256"],
            "diagnostic_component_manifest_sha256": _file_record(
                resolved_diagnostic / "manifest.json"
            )["sha256"],
            "structural_findings": [],
            "remaining_blockers": [
                "PREREGISTRATION_NOT_FROZEN",
                "DETERMINISTIC_SUITE_NOT_FROZEN",
                "INPUT_LOCK_NOT_GENERATED",
                "POST_RUN_BLIND_REVIEW_NOT_RESOLVED",
            ],
            "claim_boundary": "join_graph_only_not_query_correctness",
        }
        _write_canonical(staging / "candidate-report.json", report)
        os.replace(staging, output)
        return report
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _load_component(root: Path) -> tuple[list[SealedAgentTask], FormalManifestV2]:
    tasks_path = root / "agent-tasks.json"
    if tasks_path.is_symlink() or not tasks_path.is_file():
        raise ValueError(f"component tasks are unavailable: {root.name}")
    try:
        tasks = TypeAdapter(list[SealedAgentTask]).validate_json(tasks_path.read_bytes())
    except (OSError, ValueError) as error:
        raise ValueError(f"component tasks are invalid: {root.name}") from error
    manifest = load_document(root / "manifest.json", FormalManifestV2)
    verify_sealed_manifest(manifest, tasks)
    return tasks, manifest


def _structural_findings(manifest: FormalManifestV2) -> tuple[str, ...]:
    confirmatory = [
        task for task in manifest.tasks if task.corpus == "natural" and task.split == "confirmatory"
    ]
    diagnostic = [
        task
        for task in manifest.tasks
        if task.corpus == "semantic_join_failure" and task.split == "diagnostic"
    ]
    findings: list[str] = []
    if len(confirmatory) != 60 or len({task.database_id for task in confirmatory}) != 12:
        findings.append("INVALID_CONFIRMATORY_ALLOCATION")
    if any(count != 5 for count in Counter(task.database_id for task in confirmatory).values()):
        findings.append("INVALID_CONFIRMATORY_DATABASE_BALANCE")
    if len(diagnostic) != 20 or len({task.database_id for task in diagnostic}) != 4:
        findings.append("INVALID_DIAGNOSTIC_ALLOCATION")
    if any(count != 5 for count in Counter(task.database_id for task in diagnostic).values()):
        findings.append("INVALID_DIAGNOSTIC_DATABASE_BALANCE")
    exact = Counter((task.question_sha256, task.sql_shape_sha256) for task in manifest.tasks)
    if any(count > 1 for count in exact.values()):
        findings.append("EXACT_TASK_DUPLICATE")
    near = Counter(
        (task.question_template_id, task.sql_structure_id) for task in manifest.tasks
    )
    if any(count > 1 for count in near.values()):
        findings.append("NEAR_TASK_DUPLICATE")
    variant_splits: dict[str, set[str]] = defaultdict(set)
    for task in manifest.tasks:
        variant_splits[task.database_variant_group].add(task.split)
    if any(len(splits) > 1 for splits in variant_splits.values()):
        findings.append("DATABASE_VARIANT_SPLIT_LEAKAGE")
    if any(task.ambiguous_ground_truth for task in confirmatory):
        findings.append("AMBIGUOUS_CONFIRMATORY_GROUND_TRUTH")
    return tuple(findings)


def _verify_database_path(root: Path, relative_name: str) -> None:
    relative = PurePosixPath(relative_name)
    if relative_name.startswith("/") or "\\" in relative_name or ".." in relative.parts:
        raise ValueError("sealed database path escapes its root")
    candidate = root / relative_name
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError(f"sealed database is unavailable: {relative_name}")
    try:
        candidate.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as error:
        raise ValueError("sealed database path escapes its root") from error
    if candidate.with_name(candidate.name + "-wal").exists() or candidate.with_name(
        candidate.name + "-shm"
    ).exists():
        raise ValueError(f"sealed database has unfrozen WAL state: {relative_name}")


def _inside_root(path: Path, root: Path, label: str) -> Path:
    resolved = _real_directory(path, label)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError(f"{label} must stay inside the sealed root") from error
    return resolved


def _real_directory(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"{label} must be one real directory")
    return path.resolve(strict=True)


def _write_canonical(path: Path, value: Any) -> None:
    with path.open("xb") as output:
        output.write(rfc8785.dumps(value))
        output.write(b"\n")
        output.flush()
        os.fsync(output.fileno())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Merge natural and diagnostic v2 inputs")
    parser.add_argument("--natural-root", type=Path, required=True)
    parser.add_argument("--diagnostic-root", type=Path, required=True)
    parser.add_argument("--sealed-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    report = build_formal_candidate(
        arguments.natural_root,
        arguments.diagnostic_root,
        arguments.sealed_root,
        arguments.output,
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
