from __future__ import annotations

from pathlib import Path
from typing import Literal

from benchmarks.formal_eval.contracts import RelationshipScope, StrictModel
from benchmarks.formal_eval.gates import AgentGateReport, RelationshipGateReport, SQLGateReport
from joinlint.contracts import canonical_json


class ReportProvenance(StrictModel):
    lineage_id: str
    dataset_release: str
    joinlint_commit: str
    image_reference: str
    image_digest: str
    harness_version: str
    inference_policy_version: str
    relationship_scope: RelationshipScope
    preregistration_sha256: str
    manifest_sha256: str
    input_lock_sha256: str
    deterministic_suite_sha256: str
    run_plan_sha256: str
    model_snapshots: tuple[str, str]
    model_families: tuple[str, str]
    host_versions: dict[str, str]


class FormalEvaluationReport(StrictModel):
    schema_version: Literal[2] = 2
    evaluation_id: str
    evidence_class: Literal["formal", "synthetic_non_evidentiary"]
    provenance: ReportProvenance
    relationship: RelationshipGateReport
    sql_validation: SQLGateReport
    agent_product: AgentGateReport
    deterministic_release_allowed: bool
    public_quantitative_claim_allowed: bool


def build_report(
    evaluation_id: str,
    provenance: ReportProvenance,
    relationship: RelationshipGateReport,
    sql_validation: SQLGateReport,
    agent_product: AgentGateReport,
    *,
    evidence_class: Literal["formal", "synthetic_non_evidentiary"] = "formal",
) -> FormalEvaluationReport:
    deterministic = relationship.passed and sql_validation.passed
    return FormalEvaluationReport(
        evaluation_id=evaluation_id,
        evidence_class=evidence_class,
        provenance=provenance,
        relationship=relationship,
        sql_validation=sql_validation,
        agent_product=agent_product,
        deterministic_release_allowed=deterministic,
        public_quantitative_claim_allowed=evidence_class == "formal"
        and deterministic
        and agent_product.publication_allowed,
    )


def write_report(output_directory: Path, report: FormalEvaluationReport) -> None:
    output_directory.mkdir(parents=True, exist_ok=True)
    (output_directory / "formal-evaluation.json").write_bytes(
        canonical_json(report.model_dump(mode="json"))
    )
    (output_directory / "formal-evaluation.md").write_text(
        render_markdown(report), encoding="utf-8"
    )


def render_markdown(report: FormalEvaluationReport) -> str:
    relationship = report.relationship
    sql = report.sql_validation
    agent = report.agent_product
    provenance = report.provenance
    status = (
        "SYNTHETIC_NON_EVIDENTIARY"
        if report.evidence_class == "synthetic_non_evidentiary"
        else "PUBLIC_QUANTITATIVE_CLAIM_ALLOWED"
        if report.public_quantitative_claim_allowed
        else "OBSERVED_RESULTS_ONLY"
    )
    failure_lines = (
        "\n".join(f"- `{finding}`" for finding in agent.failed_publication_gates)
        or "- None"
    )
    cell_lines = "\n".join(
        f"- `{name}`: {value:.4f}" for name, value in agent.cell_completion_effects.items()
    )
    strata_lines = "\n".join(
        f"- `{name}`: {value:.4f}"
        for name, value in agent.stratified_completion_effects.items()
    )
    funnel_lines = "\n".join(f"- {name}: {value}" for name, value in agent.funnel.items())
    agent_failure_lines = (
        "\n".join(f"- `{name}`: {value}" for name, value in agent.failure_counts.items())
        or "- None"
    )
    no_improvement_lines = (
        "\n".join(f"- `{name}`" for name in agent.no_improvement_databases)
        or "- None"
    )
    resource_lines = "\n".join(
        f"- `{condition}`: {values}"
        for condition, values in agent.resource_usage.items()
    )
    return f"""# JoinLint Formal Evaluation

> Join correctness is not proof of metric meaning or complete answer correctness.

## Decision

- Evaluation: `{report.evaluation_id}`
- Evidence class: `{report.evidence_class}`
- Lineage: `{provenance.lineage_id}`
- Dataset release: `{provenance.dataset_release}`
- JoinLint commit: `{provenance.joinlint_commit}`
- Image: `{provenance.image_reference}`
- Harness: `{provenance.harness_version}`
- Inference policy: `{provenance.inference_policy_version}`
- Relationship scope: `{provenance.relationship_scope}`
- Models: `{provenance.model_snapshots}`
- Model families: `{provenance.model_families}`
- Host versions: `{provenance.host_versions}`
- Preregistration SHA-256: `{provenance.preregistration_sha256}`
- Manifest SHA-256: `{provenance.manifest_sha256}`
- Input lock SHA-256: `{provenance.input_lock_sha256}`
- Deterministic suite SHA-256: `{provenance.deterministic_suite_sha256}`
- Run plan SHA-256: `{provenance.run_plan_sha256}`
- Status: `{status}`
- Deterministic release allowed: `{str(report.deterministic_release_allowed).lower()}`
- Public quantitative claim allowed: `{str(report.public_quantitative_claim_allowed).lower()}`

## Relationship evidence

- Scope: {relationship.relationship_scope}
- Declared extraction recall: {relationship.declared_recall:.4f}
- Curated currently supported: {relationship.curated_currently_supported}/{relationship.curated_eligible}
- Natural databases: {relationship.natural_database_count}
- Natural domains: {relationship.natural_domain_count}
- Declared visible/ablated pairs: {relationship.declared_variant_pair_count}
- Annotated absent-FK databases: {relationship.annotated_absent_fk_database_count}
- Adversarial candidates: {relationship.adversarial_candidate_count}
- Adversarial trap counts: {relationship.adversarial_type_counts}
- Verified selective precision: {relationship.verified_precision:.4f}
- Verified Wilson lower bound: {relationship.verified_wilson_lower:.4f}
- Verified coverage: {relationship.verified_coverage:.4f}
- Verified abstentions: {relationship.verified_abstentions}
- Verified auto-usable edges: {relationship.verified_auto_usable}
- Verified databases: {relationship.verified_database_count}
- Adversarial false promotions: {relationship.adversarial_false_promotions}
- Non-exact promotions: {relationship.nonexact_promotions}
- Stale promotions: {relationship.stale_promotions}
- Unsafe auto-promotions: {relationship.unsafe_auto_promotions}

## SQL validation

- Safe controls: {sql.safe_count}
- Dangerous cases: {sql.dangerous_count}
- Dangerous accepted: {sql.dangerous_accepted}
- Dangerous acceptance rate: {sql.dangerous_acceptance_rate:.4f}
- Dangerous not blocked: {sql.dangerous_not_blocked}
- False blocks: {sql.false_blocks}
- False block rate: {sql.false_block_rate:.4f}
- Edge extraction errors: {sql.edge_extraction_errors}
- Edge extraction accuracy: {sql.edge_extraction_accuracy:.4f}
- Unsafe repairs: {sql.unsafe_repairs}
- Repair safety: {sql.repair_safety:.4f}
- SQL shape counts: {sql.sql_shape_counts}
- Dangerous-type counts: {sql.danger_type_counts}
- Runtime-attested SQL executions during validation: {sql.execution_count}
- Cached plan p95: {sql.performance.cached_plan_p95_ms:.2f} ms
- Cached validation p95: {sql.performance.cached_validation_p95_ms:.2f} ms
- Metadata-only plan p95: {sql.performance.metadata_plan_p95_ms:.2f} ms
- Million-row validation: {sql.performance.million_row_seconds:.2f} s
- Million-row peak RSS: {sql.performance.million_row_peak_rss_bytes} bytes

## Agent product effect

- Paired units: {agent.paired_unit_count}
- Databases: {agent.database_count}
- Tasks: {agent.task_count}
- Completion effect: {agent.completion_effect.point:.4f}
- Completion 95% interval: [{agent.completion_effect.interval.lower:.4f}, {agent.completion_effect.interval.upper:.4f}]
- Dangerous submission reduction: {agent.dangerous_submission_reduction.point:.4f}
- Execution effect: {agent.execution_effect.point:.4f}
- Blind review: {agent.blind_reviewed_runs}/{agent.blind_review_required} required, agreement {agent.blind_review_agreement:.4f}

### Model and host cells

{cell_lines}

### Preregistered strata

{strata_lines}

### Treatment funnel

{funnel_lines}

### Failure counts

{agent_failure_lines}

### Databases without improvement

{no_improvement_lines}

### Tokens, latency, and cost

{resource_lines}

### Failed publication gates

{failure_lines}
"""
