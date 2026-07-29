from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Literal

from pydantic import Field

from benchmarks.formal_eval.contracts import StrictModel
from benchmarks.formal_eval.pilot_canary import dependency_versions
from joinlint.contracts import canonical_json


MODAL_READINESS_BUDGET_CNY = 2.05
MODAL_READINESS_COMPUTE_UPPER_CNY = 0.031728
MODAL_IMAGE_BUILD_RESERVE_CNY = 2.0


class ModalReadinessReport(StrictModel):
    schema_version: Literal[1] = 1
    status: Literal["passed"] = "passed"
    workflow_run_id: int = Field(gt=0)
    joinlint_commit: str
    modal_image_builder_version: Literal["2025.06 Stable"] = "2025.06 Stable"
    host: Literal["codex"] = "codex"
    agent_version: Literal["0.144.1"] = "0.144.1"
    host_binary_sha256: str
    infrastructure_preparation_duration_seconds: float = Field(ge=0)
    model_usage_count: Literal[0] = 0
    modal_compute_upper_cny: Literal[0.031728] = MODAL_READINESS_COMPUTE_UPPER_CNY
    modal_image_build_reserve_cny: Literal[2.0] = MODAL_IMAGE_BUILD_RESERVE_CNY
    total_cost_upper_cny: Literal[2.031728] = 2.031728
    dependency_versions: dict[str, str]


def verify_modal_readiness(
    log_dir: Path,
    output: Path,
    *,
    workflow_run_id: int,
    joinlint_commit: str,
) -> ModalReadinessReport:
    from inspect_ai.log import list_eval_logs, read_eval_log

    infos = list_eval_logs(str(log_dir), recursive=True)
    if len(infos) != 1:
        raise RuntimeError("Modal readiness did not produce exactly one Inspect log")
    log = read_eval_log(infos[0].name, header_only=False)
    samples = log.samples or []
    if len(samples) != 1:
        raise RuntimeError("Modal readiness did not produce exactly one sample")
    sample = samples[0]
    if sample.error is not None:
        raise RuntimeError("Modal readiness sample has a runtime error")
    if sample.model_usage:
        raise RuntimeError("Modal readiness unexpectedly called a model")
    score = (sample.scores or {}).get("formal_modal_readiness_scorer")
    metadata = score.metadata if score is not None and isinstance(score.metadata, dict) else {}
    if score is None or score.value != 1 or metadata.get("readiness_attested") is not True:
        raise RuntimeError("Modal readiness was not attested")
    digest = metadata.get("host_binary_sha256")
    duration = metadata.get("infrastructure_preparation_duration_seconds")
    if not isinstance(digest, str) or len(digest) != 64:
        raise RuntimeError("Modal readiness host digest is missing")
    if not isinstance(duration, int | float) or duration < 0:
        raise RuntimeError("Modal readiness duration is missing")
    report = ModalReadinessReport(
        workflow_run_id=workflow_run_id,
        joinlint_commit=joinlint_commit,
        host_binary_sha256=digest,
        infrastructure_preparation_duration_seconds=float(duration),
        dependency_versions=dependency_versions(),
    )
    output.mkdir(parents=True, exist_ok=True)
    (output / "modal-readiness.json").write_bytes(
        canonical_json(report.model_dump(mode="json")) + b"\n"
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify one no-model Modal readiness run")
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workflow-run-id", type=int, required=True)
    parser.add_argument("--joinlint-commit", required=True)
    arguments = parser.parse_args(argv)
    report = verify_modal_readiness(
        arguments.log_dir,
        arguments.output,
        workflow_run_id=arguments.workflow_run_id,
        joinlint_commit=arguments.joinlint_commit,
    )
    print(json.dumps(report.model_dump(mode="json"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
