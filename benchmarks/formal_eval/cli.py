from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, TypeAdapter

from benchmarks.formal_eval.contracts import (
    AgentResultRow,
    AgentResultBundle,
    BlindReviewBundle,
    DeterministicEvidenceBundle,
    FormalManifestV2,
    InputLockV2,
    PreregistrationV2,
    SealedAgentTask,
)
from benchmarks.formal_eval.fake import write_fake_evaluation
from benchmarks.formal_eval.inspect_smoke import run_inspect_smoke
from benchmarks.formal_eval.export import export_agent_rows
from benchmarks.formal_eval.gates import (
    agent_product_gate,
    diagnostic_canary_gate,
    relationship_gate,
    sql_validation_gate,
)
from benchmarks.formal_eval.manifest import (
    load_document,
    require_locked_inputs,
    validate_manifest,
    verify_input_lock,
    verify_sealed_manifest,
)
from benchmarks.formal_eval.lineage import EvaluationLineage, build_lineage, digest_value, verify_lineage
from benchmarks.formal_eval.power import PowerPlan, power_plan_from_pilot, validate_power_readiness
from benchmarks.formal_eval.report import ReportProvenance, build_report, write_report
from benchmarks.formal_eval.run_plan import RunPlanV2, build_run_plan, summarize_run_plan
from benchmarks.formal_eval.runtime_contract import check_runtime_contract
from joinlint.contracts import canonical_json


ModelT = TypeVar("ModelT", bound=BaseModel)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "fake-model":
        write_fake_evaluation(arguments.output)
        return 0
    if arguments.command == "inspect-smoke":
        run_inspect_smoke(arguments.project, arguments.output)
        return 0
    if arguments.command == "preflight":
        from benchmarks.formal_eval.deterministic import DeterministicSuite

        preregistration = load_document(arguments.preregistration, PreregistrationV2)
        manifest = load_document(arguments.manifest, FormalManifestV2)
        lock = load_document(arguments.input_lock, InputLockV2)
        sealed_tasks = _load_rows(arguments.sealed_tasks, SealedAgentTask)
        deterministic_suite = load_document(arguments.deterministic_suite, DeterministicSuite)
        if deterministic_suite.relationship_scope != preregistration.relationship_scope:
            raise ValueError("deterministic suite relationship scope does not match preregistration")
        verify_sealed_manifest(manifest, sealed_tasks)
        if arguments.current_commit != preregistration.joinlint_commit:
            raise ValueError("checked-out JoinLint commit does not match preregistration")
        verify_input_lock(lock, arguments.inputs_root)
        require_locked_inputs(
            lock,
            arguments.inputs_root,
            [
                arguments.preregistration,
                arguments.manifest,
                arguments.sealed_tasks,
                arguments.deterministic_suite,
                *arguments.required_input,
                *(Path(task.database_path) for task in sealed_tasks),
                *(Path(case.project_path) for case in deterministic_suite.relationship_cases),
                *(Path(case.project_path) for case in deterministic_suite.sql_validation_cases),
                Path(deterministic_suite.performance.project_path),
                Path(deterministic_suite.performance.million_row_project_path),
            ],
        )
        readiness = validate_manifest(manifest, preregistration)
        lineage = build_lineage(preregistration, manifest, lock)
        _write_canonical(arguments.output, readiness.model_dump(mode="json"))
        _write_canonical(arguments.lineage_output, lineage.model_dump(mode="json"))
        return 0 if readiness.ready else 2
    if arguments.command == "score":
        from benchmarks.formal_eval.deterministic import DeterministicSuite

        preregistration = load_document(arguments.preregistration, PreregistrationV2)
        manifest = load_document(arguments.manifest, FormalManifestV2)
        input_lock = load_document(arguments.input_lock, InputLockV2)
        lineage = build_lineage(preregistration, manifest, input_lock)
        evidence = load_document(arguments.deterministic_evidence, DeterministicEvidenceBundle)
        verify_lineage(lineage, evidence.lineage_id)
        deterministic_suite = load_document(arguments.deterministic_suite, DeterministicSuite)
        if deterministic_suite.relationship_scope != preregistration.relationship_scope:
            raise ValueError("deterministic suite relationship scope does not match preregistration")
        if evidence.deterministic_suite_sha256 != digest_value(
            deterministic_suite.model_dump(mode="json")
        ):
            raise ValueError("deterministic evidence does not match the frozen raw suite")
        run_plan = load_document(arguments.run_plan, RunPlanV2)
        verify_lineage(lineage, run_plan.lineage_id)
        agent_bundle = load_document(arguments.agent_results, AgentResultBundle)
        verify_lineage(lineage, agent_bundle.lineage_id)
        run_plan_sha256 = digest_value(run_plan.model_dump(mode="json"))
        if agent_bundle.run_plan_sha256 != run_plan_sha256:
            raise ValueError("agent results are not bound to the frozen run plan")
        agent_results_sha256 = digest_value(agent_bundle.model_dump(mode="json"))
        blind_review = (
            load_document(arguments.blind_review, BlindReviewBundle)
            if arguments.blind_review is not None
            else BlindReviewBundle(
                lineage_id=lineage.lineage_id,
                formal_results_sha256=agent_results_sha256,
                run_plan_sha256=run_plan_sha256,
                review_protocol_version="pending",
                arm_labels_removed=True,
                decisions=(),
            )
        )
        verify_lineage(lineage, blind_review.lineage_id)
        report = build_report(
            preregistration.evaluation_id,
            ReportProvenance(
                lineage_id=lineage.lineage_id,
                dataset_release=preregistration.dataset_release,
                joinlint_commit=preregistration.joinlint_commit,
                image_reference=preregistration.image_reference,
                image_digest=preregistration.image_digest,
                harness_version=preregistration.harness_version,
                inference_policy_version=preregistration.inference_policy_version,
                relationship_scope=preregistration.relationship_scope,
                preregistration_sha256=lineage.preregistration_sha256,
                manifest_sha256=lineage.manifest_sha256,
                input_lock_sha256=lineage.input_lock_sha256,
                deterministic_suite_sha256=evidence.deterministic_suite_sha256,
                run_plan_sha256=run_plan_sha256,
                model_snapshots=tuple(model.returned_id for model in preregistration.models),
                model_families=tuple(model.family for model in preregistration.models),
                host_versions=dict(preregistration.host_versions),
            ),
            relationship_gate(
                evidence.relationship_rows,
                relationship_scope=preregistration.relationship_scope,
            ),
            sql_validation_gate(evidence.sql_validation_rows, evidence.performance),
            agent_product_gate(
                agent_bundle.rows,
                blind_review,
                run_plan=run_plan,
                agent_results_sha256=agent_results_sha256,
                thresholds=preregistration.thresholds,
                bootstrap_draws=preregistration.bootstrap_draws,
                seed=preregistration.seed,
            ),
        )
        write_report(arguments.output, report)
        if arguments.require == "deterministic" and not report.deterministic_release_allowed:
            return 2
        if arguments.require == "public" and not report.public_quantitative_claim_allowed:
            return 2
        return 0
    if arguments.command == "power":
        rows = _load_rows(arguments.pilot_agent_results, AgentResultRow)
        lineage = load_document(arguments.lineage, EvaluationLineage)
        plan = power_plan_from_pilot(rows, lineage_id=lineage.lineage_id)
        _write_canonical(arguments.output, plan.model_dump(mode="json"))
        return 0
    if arguments.command == "run-plan":
        preregistration = load_document(arguments.preregistration, PreregistrationV2)
        manifest = load_document(arguments.manifest, FormalManifestV2)
        lineage = load_document(arguments.lineage, EvaluationLineage)
        plan = build_run_plan(manifest, preregistration, lineage.lineage_id)
        _write_canonical(arguments.output, plan.model_dump(mode="json"))
        if arguments.summary is not None:
            _write_canonical(arguments.summary, summarize_run_plan(plan))
        return 0
    if arguments.command == "power-check":
        plan = load_document(arguments.power_plan, PowerPlan)
        manifest = load_document(arguments.manifest, FormalManifestV2)
        run_plan = load_document(arguments.run_plan, RunPlanV2)
        readiness = validate_power_readiness(plan, manifest, run_plan)
        _write_canonical(arguments.output, readiness.model_dump(mode="json"))
        return 0 if readiness.ready else 2
    if arguments.command == "canary-check":
        bundle = load_document(arguments.agent_results, AgentResultBundle)
        report = diagnostic_canary_gate(bundle.rows)
        _write_canonical(arguments.output, report.model_dump(mode="json"))
        return 0 if report.passed else 2
    if arguments.command == "runtime-check":
        report = check_runtime_contract(arguments.project)
        _write_canonical(arguments.output, report.model_dump(mode="json"))
        return 0 if report.ready else 2
    if arguments.command == "export-logs":
        preregistration = load_document(arguments.preregistration, PreregistrationV2)
        lineage = load_document(arguments.lineage, EvaluationLineage)
        run_plan = load_document(arguments.run_plan, RunPlanV2)
        export_agent_rows(
            arguments.log_dir,
            arguments.output,
            expected_model_ids={model.returned_id for model in preregistration.models},
            lineage_id=lineage.lineage_id,
            run_plan=run_plan,
            phase=arguments.phase,
            summary_output=arguments.summary,
        )
        return 0
    parser.error("unknown command")
    return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="JoinLint formal evaluation v2")
    commands = parser.add_subparsers(dest="command", required=True)

    fake = commands.add_parser("fake-model", help="run the non-evidentiary pipeline smoke")
    fake.add_argument("--output", type=Path, required=True)

    inspect_smoke = commands.add_parser(
        "inspect-smoke",
        help="run a non-evidentiary Inspect mock against the current two-tool MCP",
    )
    inspect_smoke.add_argument("--project", type=Path, required=True)
    inspect_smoke.add_argument("--output", type=Path, required=True)

    preflight = commands.add_parser("preflight", help="verify frozen formal inputs")
    preflight.add_argument("--preregistration", type=Path, required=True)
    preflight.add_argument("--manifest", type=Path, required=True)
    preflight.add_argument("--input-lock", type=Path, required=True)
    preflight.add_argument("--inputs-root", type=Path, required=True)
    preflight.add_argument("--sealed-tasks", type=Path, required=True)
    preflight.add_argument("--deterministic-suite", type=Path, required=True)
    preflight.add_argument("--required-input", type=Path, action="append", default=[])
    preflight.add_argument("--current-commit", required=True)
    preflight.add_argument("--output", type=Path, required=True)
    preflight.add_argument("--lineage-output", type=Path, required=True)

    score = commands.add_parser("score", help="score frozen, sanitized result rows")
    score.add_argument("--preregistration", type=Path, required=True)
    score.add_argument("--manifest", type=Path, required=True)
    score.add_argument("--input-lock", type=Path, required=True)
    score.add_argument("--deterministic-evidence", type=Path, required=True)
    score.add_argument("--deterministic-suite", type=Path, required=True)
    score.add_argument("--run-plan", type=Path, required=True)
    score.add_argument("--blind-review", type=Path)
    score.add_argument("--agent-results", type=Path, required=True)
    score.add_argument("--output", type=Path, required=True)
    score.add_argument("--require", choices=("none", "deterministic", "public"), default="none")

    power = commands.add_parser("power", help="derive the minimum formal size from a pilot")
    power.add_argument("--pilot-agent-results", type=Path, required=True)
    power.add_argument("--lineage", type=Path, required=True)
    power.add_argument("--output", type=Path, required=True)

    run_plan = commands.add_parser("run-plan", help="expand frozen tasks into remote runs")
    run_plan.add_argument("--preregistration", type=Path, required=True)
    run_plan.add_argument("--manifest", type=Path, required=True)
    run_plan.add_argument("--lineage", type=Path, required=True)
    run_plan.add_argument("--output", type=Path, required=True)
    run_plan.add_argument("--summary", type=Path)

    power_check = commands.add_parser("power-check", help="enforce pilot-derived sample size")
    power_check.add_argument("--power-plan", type=Path, required=True)
    power_check.add_argument("--manifest", type=Path, required=True)
    power_check.add_argument("--run-plan", type=Path, required=True)
    power_check.add_argument("--output", type=Path, required=True)

    canary = commands.add_parser("canary-check", help="gate diagnostic infrastructure")
    canary.add_argument("--agent-results", type=Path, required=True)
    canary.add_argument("--output", type=Path, required=True)

    runtime = commands.add_parser("runtime-check", help="require the completed two-tool MCP")
    runtime.add_argument("--project", type=Path, required=True)
    runtime.add_argument("--output", type=Path, required=True)

    export = commands.add_parser("export-logs", help="sanitize Inspect logs into v2 rows")
    export.add_argument("--log-dir", type=Path, required=True)
    export.add_argument("--preregistration", type=Path, required=True)
    export.add_argument("--lineage", type=Path, required=True)
    export.add_argument("--run-plan", type=Path, required=True)
    export.add_argument(
        "--phase", choices=("all", "confirmatory", "diagnostic"), default="all"
    )
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--summary", type=Path, required=True)
    return parser


def _load_rows(path: Path, model: type[ModelT]) -> list[ModelT]:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"result input is not one regular file: {path.name}")
    try:
        return TypeAdapter(list[model]).validate_json(path.read_bytes())  # type: ignore[valid-type]
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"invalid result rows: {path.name}") from error


def _write_canonical(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(value))


if __name__ == "__main__":
    raise SystemExit(main())
