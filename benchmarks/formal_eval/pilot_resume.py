from __future__ import annotations

import argparse
import json
from pathlib import Path

from benchmarks.formal_eval.lineage import digest_value
from benchmarks.formal_eval.pilot import verify_pilot_input_bundle
from benchmarks.formal_eval.pilot_stage import (
    contract_safety_resume_budget_envelope,
    flash_stage_run_plan,
    verify_contract_safety_resume_source,
)


def verify_resume_inputs(
    root: Path,
    source_root: Path,
    run_metadata: Path,
    artifact_metadata: Path,
    *,
    source_run_id: int,
    source_artifact_sha256: str,
    source_workflow_commit: str,
    source_reservation_id: str,
) -> dict[str, object]:
    registration, _, full_run_plan, lock = verify_pilot_input_bundle(root)
    stage_run_plan = flash_stage_run_plan(registration, full_run_plan)
    source_bundle, _, artifact_id, artifact_size = (
        verify_contract_safety_resume_source(
            source_root,
            run_metadata,
            artifact_metadata,
            registration=registration,
            full_run_plan=full_run_plan,
            stage_run_plan=stage_run_plan,
            input_lock_sha256=digest_value(lock.model_dump(mode="json")),
            expected_source_run_id=source_run_id,
            expected_source_artifact_sha256=source_artifact_sha256,
            expected_source_workflow_commit=source_workflow_commit,
            expected_source_reservation_id=source_reservation_id,
        )
    )
    envelope = contract_safety_resume_budget_envelope(registration)
    return {
        "artifact_id": artifact_id,
        "artifact_size_bytes": artifact_size,
        "execution_total_upper_cny": envelope.total_upper_cny,
        "remaining_run_count": envelope.run_count,
        "source_row_count": len(source_bundle.rows),
        "source_workflow_run_id": source_run_id,
        "status": "ready",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a formal Pilot resume source")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--run-metadata", type=Path, required=True)
    parser.add_argument("--artifact-metadata", type=Path, required=True)
    parser.add_argument("--source-run-id", type=int, required=True)
    parser.add_argument("--source-artifact-sha256", required=True)
    parser.add_argument("--source-workflow-commit", required=True)
    parser.add_argument("--source-reservation-id", required=True)
    arguments = parser.parse_args(argv)
    report = verify_resume_inputs(
        arguments.root,
        arguments.source_root,
        arguments.run_metadata,
        arguments.artifact_metadata,
        source_run_id=arguments.source_run_id,
        source_artifact_sha256=arguments.source_artifact_sha256,
        source_workflow_commit=arguments.source_workflow_commit,
        source_reservation_id=arguments.source_reservation_id,
    )
    print(json.dumps(report, allow_nan=False, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
