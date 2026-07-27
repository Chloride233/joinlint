from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from benchmarks.formal_eval.contracts import (
    FormalManifestV2,
    InputLockV2,
    PreregistrationV2,
    StrictModel,
)
from joinlint.contracts import canonical_json


class EvaluationLineage(StrictModel):
    schema_version: Literal[2] = 2
    lineage_id: str
    evaluation_id: str
    dataset_release: str
    joinlint_commit: str
    image_digest: str
    inference_policy_version: str
    preregistration_sha256: str
    manifest_sha256: str
    input_lock_sha256: str


def build_lineage(
    preregistration: PreregistrationV2,
    manifest: FormalManifestV2,
    input_lock: InputLockV2,
) -> EvaluationLineage:
    preregistration_digest = _digest_model(preregistration)
    manifest_digest = _digest_model(manifest)
    input_lock_digest = _digest_model(input_lock)
    identity = {
        "evaluation_id": preregistration.evaluation_id,
        "dataset_release": preregistration.dataset_release,
        "joinlint_commit": preregistration.joinlint_commit,
        "image_digest": preregistration.image_digest,
        "inference_policy_version": preregistration.inference_policy_version,
        "preregistration_sha256": preregistration_digest,
        "manifest_sha256": manifest_digest,
        "input_lock_sha256": input_lock_digest,
    }
    return EvaluationLineage(
        lineage_id=hashlib.sha256(canonical_json(identity)).hexdigest(),
        **identity,
    )


def verify_lineage(expected: EvaluationLineage, actual_lineage_id: str) -> None:
    if actual_lineage_id != expected.lineage_id:
        raise ValueError("evaluation artifact lineage mismatch")


def digest_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"digest input is not one regular file: {path.name}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def digest_value(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _digest_model(value: StrictModel) -> str:
    return digest_value(value.model_dump(mode="json"))
