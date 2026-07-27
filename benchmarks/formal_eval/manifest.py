from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel
from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from benchmarks.formal_eval.contracts import (
    FormalManifestV2,
    InputLockV2,
    PreregistrationV2,
    SealedAgentTask,
    StrictModel,
)


ModelT = TypeVar("ModelT", bound=BaseModel)


class ManifestReadiness(StrictModel):
    confirmatory_task_count: int
    confirmatory_database_count: int
    diagnostic_task_count: int
    model_count: int
    host_count: int
    duplicate_pair_count: int
    near_duplicate_pair_count: int
    cross_split_variant_count: int
    preregistration_matches_manifest: bool
    ready: bool
    findings: tuple[str, ...]


def load_document(path: Path, model: type[ModelT]) -> ModelT:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"required input is not one regular file: {path.name}")
    try:
        if path.suffix in {".yaml", ".yml"}:
            payload: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
            return model.model_validate(payload, strict=False)
        else:
            return model.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, yaml.YAMLError, ValueError) as error:
        raise ValueError(f"invalid frozen document: {path.name}") from error


def validate_manifest(
    manifest: FormalManifestV2,
    preregistration: PreregistrationV2,
    *,
    min_databases: int = 12,
    min_tasks: int = 60,
    min_tasks_per_database: int = 5,
    min_diagnostic_tasks: int = 20,
) -> ManifestReadiness:
    confirmatory = [
        task for task in manifest.tasks if task.corpus == "natural" and task.split == "confirmatory"
    ]
    diagnostic = [task for task in manifest.tasks if task.split == "diagnostic"]
    databases = {task.database_id for task in confirmatory}
    per_database = Counter(task.database_id for task in confirmatory)

    exact_pairs: dict[tuple[str, str], list[str]] = defaultdict(list)
    for task in manifest.tasks:
        exact_pairs[(task.question_sha256, task.sql_shape_sha256)].append(task.task_id)
    duplicate_pairs = sum(len(task_ids) - 1 for task_ids in exact_pairs.values())

    near_pairs: dict[tuple[str, str], list[str]] = defaultdict(list)
    for task in manifest.tasks:
        near_pairs[(task.question_template_id, task.sql_structure_id)].append(task.task_id)
    near_duplicate_pairs = sum(len(task_ids) - 1 for task_ids in near_pairs.values())

    variant_splits: dict[str, set[str]] = defaultdict(set)
    for task in manifest.tasks:
        variant_splits[task.database_variant_group].add(task.split)
    cross_split_variants = sum(len(splits) > 1 for splits in variant_splits.values())

    findings: list[str] = []
    if len(confirmatory) < min_tasks:
        findings.append("INSUFFICIENT_CONFIRMATORY_TASKS")
    if len(databases) < min_databases:
        findings.append("INSUFFICIENT_CONFIRMATORY_DATABASES")
    if any(count < min_tasks_per_database for count in per_database.values()):
        findings.append("INSUFFICIENT_TASKS_PER_DATABASE")
    if len(diagnostic) < min_diagnostic_tasks:
        findings.append("INSUFFICIENT_DIAGNOSTIC_TASKS")
    if duplicate_pairs:
        findings.append("EXACT_TASK_DUPLICATE")
    if near_duplicate_pairs:
        findings.append("NEAR_TASK_DUPLICATE")
    if cross_split_variants:
        findings.append("DATABASE_VARIANT_SPLIT_LEAKAGE")
    if any(task.ambiguous_ground_truth for task in confirmatory):
        findings.append("AMBIGUOUS_CONFIRMATORY_GROUND_TRUTH")
    matches = manifest.dataset_release == preregistration.dataset_release
    if not matches:
        findings.append("DATASET_RELEASE_MISMATCH")

    return ManifestReadiness(
        confirmatory_task_count=len(confirmatory),
        confirmatory_database_count=len(databases),
        diagnostic_task_count=len(diagnostic),
        model_count=len(preregistration.models),
        host_count=len(preregistration.hosts),
        duplicate_pair_count=duplicate_pairs,
        near_duplicate_pair_count=near_duplicate_pairs,
        cross_split_variant_count=cross_split_variants,
        preregistration_matches_manifest=matches,
        ready=not findings,
        findings=tuple(findings),
    )


def verify_input_lock(lock: InputLockV2, root: Path) -> None:
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        raise ValueError("input root is unavailable") from error
    if not resolved_root.is_dir() or root.is_symlink():
        raise ValueError("input root must be one real directory")
    for relative_name, expected_digest in lock.files.items():
        candidate = root / relative_name
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError(f"locked input is unavailable: {relative_name}")
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(resolved_root)
        except (OSError, ValueError) as error:
            raise ValueError(f"locked input escapes its root: {relative_name}") from error
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if digest != expected_digest:
            raise ValueError(f"locked input hash mismatch: {relative_name}")


def require_locked_inputs(lock: InputLockV2, root: Path, paths: list[Path]) -> None:
    resolved_root = root.resolve(strict=True)
    for path in paths:
        candidates = [path]
        if path.is_dir() and not path.is_symlink():
            candidates = sorted(candidate for candidate in path.rglob("*") if candidate.is_file())
            if not candidates:
                raise ValueError(f"required input directory is empty: {path.name}")
        for candidate in candidates:
            if candidate.is_symlink() or not candidate.is_file():
                raise ValueError(f"required input is unavailable: {candidate.name}")
            try:
                relative = candidate.resolve(strict=True).relative_to(resolved_root).as_posix()
            except (OSError, ValueError) as error:
                raise ValueError(f"required input escapes its root: {candidate.name}") from error
            if relative not in lock.files:
                raise ValueError(f"required input is not frozen: {relative}")


def verify_sealed_task_hashes(
    question_hash: str,
    schema_hash: str,
    sql_shape_hash: str,
    task: SealedAgentTask,
) -> None:
    values = (task.question, task.schema_text, task.sql_shape)
    expected = (question_hash, schema_hash, sql_shape_hash)
    actual = tuple(hashlib.sha256(value.encode("utf-8")).hexdigest() for value in values)
    if actual != expected:
        raise ValueError(f"sealed task hash mismatch: {task.task_id}")


def verify_sealed_manifest(
    manifest: FormalManifestV2,
    sealed_tasks: list[SealedAgentTask],
) -> None:
    by_id = {task.task_id: task for task in sealed_tasks}
    if len(by_id) != len(sealed_tasks):
        raise ValueError("sealed tasks contain duplicate task IDs")
    if set(by_id) != {task.task_id for task in manifest.tasks}:
        raise ValueError("sealed tasks do not match the frozen manifest")
    for manifest_task in manifest.tasks:
        sealed = by_id[manifest_task.task_id]
        if (
            sealed.database_id != manifest_task.database_id
            or sealed.allowed_graphs != manifest_task.allowed_graphs
            or sealed.oracle_has_safe_path != manifest_task.oracle_has_safe_path
        ):
            raise ValueError(f"sealed task contract mismatch: {sealed.task_id}")
        verify_sealed_task_hashes(
            manifest_task.question_sha256,
            manifest_task.schema_sha256,
            manifest_task.sql_shape_sha256,
            sealed,
        )
        expected_fingerprints = (
            semantic_fingerprint("schema", sealed.schema_text),
            semantic_fingerprint("question", sealed.question),
            semantic_fingerprint("sql", sealed.gold_sql),
        )
        actual_fingerprints = (
            manifest_task.schema_family_id,
            manifest_task.question_template_id,
            manifest_task.sql_structure_id,
        )
        if actual_fingerprints != expected_fingerprints:
            raise ValueError(f"near-duplicate fingerprint mismatch: {sealed.task_id}")


def semantic_fingerprint(kind: str, value: str) -> str:
    if kind == "sql":
        try:
            tree = parse_one(value, read="sqlite")
        except ParseError as error:
            raise ValueError("sealed gold SQL cannot be normalized") from error

        def anonymize(node: exp.Expression) -> exp.Expression:
            if isinstance(node, exp.Identifier):
                return exp.Identifier(this="_id", quoted=False)
            if isinstance(node, exp.Literal):
                return (
                    exp.Literal.string("_string")
                    if node.is_string
                    else exp.Literal.number("0")
                )
            return node

        normalized = tree.transform(anonymize).sql(
            dialect="sqlite", normalize=True, pretty=False
        )
        return hashlib.sha256(f"{kind}\0{normalized}".encode("utf-8")).hexdigest()
    normalized = value.casefold()
    normalized = re.sub(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"", " <string> ", normalized)
    normalized = re.sub(r"\b\d+(?:\.\d+)?\b", " <number> ", normalized)
    normalized = " ".join(normalized.split())
    return hashlib.sha256(f"{kind}\0{normalized}".encode("utf-8")).hexdigest()
