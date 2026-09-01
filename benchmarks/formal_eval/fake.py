from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

from benchmarks.formal_eval.contracts import (
    AgentResultRow,
    AgentResultBundle,
    BlindReviewBundle,
    BlindReviewDecision,
    DeterministicEvidenceBundle,
    FormalManifestV2,
    FormalTask,
    FrozenModel,
    ModelPricingCNY,
    PerformanceMeasurement,
    PreregistrationV2,
    RelationshipResultRow,
    SQLValidationResultRow,
)
from benchmarks.formal_eval.gates import agent_product_gate, relationship_gate, sql_validation_gate
from benchmarks.formal_eval.manifest import validate_manifest
from benchmarks.formal_eval.lineage import digest_value
from benchmarks.formal_eval.report import ReportProvenance, build_report, write_report
from benchmarks.formal_eval.run_plan import RunPlanV2, build_run_plan, sample_id_for
from joinlint.contracts import canonical_json


FAKE_LINEAGE_ID = "1" * 64


def write_fake_evaluation(output_directory: Path) -> None:
    """Exercise the complete deterministic pipeline with non-evidentiary synthetic rows."""
    output_directory.mkdir(parents=True, exist_ok=True)
    preregistration = fake_preregistration()
    manifest = fake_manifest()
    evidence = fake_deterministic_evidence()
    agent_rows = fake_agent_rows()
    run_plan = build_run_plan(manifest, preregistration, FAKE_LINEAGE_ID)
    agent_bundle = AgentResultBundle(
        lineage_id=FAKE_LINEAGE_ID,
        run_plan_sha256=digest_value(run_plan.model_dump(mode="json")),
        rows=tuple(agent_rows),
    )
    review = fake_blind_review(agent_bundle, run_plan)

    readiness = validate_manifest(manifest, preregistration)
    if not readiness.ready:
        raise ValueError(f"fake manifest is not ready: {readiness.findings}")
    relationship = relationship_gate(
        evidence.relationship_rows,
        relationship_scope=preregistration.relationship_scope,
    )
    sql = sql_validation_gate(evidence.sql_validation_rows, evidence.performance)
    agent = agent_product_gate(
        agent_rows,
        review,
        run_plan=run_plan,
        agent_results_sha256=digest_value(agent_bundle.model_dump(mode="json")),
        thresholds=preregistration.thresholds,
        bootstrap_draws=1_000,
        seed=preregistration.seed,
    )
    report = build_report(
        preregistration.evaluation_id,
        ReportProvenance(
            lineage_id=FAKE_LINEAGE_ID,
            dataset_release=preregistration.dataset_release,
            joinlint_commit=preregistration.joinlint_commit,
            image_reference=preregistration.image_reference,
            image_digest=preregistration.image_digest,
            harness_version=preregistration.harness_version,
            inference_policy_version=preregistration.inference_policy_version,
            relationship_scope=preregistration.relationship_scope,
            preregistration_sha256="4" * 64,
            manifest_sha256="5" * 64,
            input_lock_sha256="6" * 64,
            deterministic_suite_sha256=evidence.deterministic_suite_sha256,
            run_plan_sha256=digest_value(run_plan.model_dump(mode="json")),
            model_identities=tuple(model.returned_id for model in preregistration.models),
            model_families=tuple(model.family for model in preregistration.models),
            model_comparison_scope="single_provider_two_tier",
            host_versions=dict(preregistration.host_versions),
        ),
        relationship,
        sql,
        agent,
        evidence_class="synthetic_non_evidentiary",
    )
    if not (
        report.deterministic_release_allowed
        and report.agent_product.publication_allowed
        and not report.public_quantitative_claim_allowed
    ):
        raise ValueError("fake pipeline must exercise the passing publication path")

    _write_json(output_directory / "preregistration.json", preregistration.model_dump(mode="json"))
    _write_json(output_directory / "manifest.json", manifest.model_dump(mode="json"))
    _write_json(output_directory / "readiness.json", readiness.model_dump(mode="json"))
    _write_json(output_directory / "deterministic-evidence.json", evidence.model_dump(mode="json"))
    _write_json(output_directory / "run-plan.json", run_plan.model_dump(mode="json"))
    _write_json(output_directory / "blind-review.json", review.model_dump(mode="json"))
    _write_json(output_directory / "agent-results.json", agent_bundle.model_dump(mode="json"))
    write_report(output_directory, report)


def fake_preregistration() -> PreregistrationV2:
    return PreregistrationV2(
        evaluation_id="synthetic-pipeline-smoke",
        dataset_release="synthetic-v2",
        seed=20260725,
        models=(
            FrozenModel(
                id="fake/high-capability-v1",
                returned_id="fake/high-capability-v1",
                family="fake-series",
                tier="high_capability",
                thinking="disabled",
                pricing_cny=ModelPricingCNY(
                    observed_at=date(2026, 7, 26),
                    input_cache_hit_per_million_cny=0.025,
                    input_cache_miss_per_million_cny=3.0,
                    output_per_million_cny=6.0,
                ),
            ),
            FrozenModel(
                id="fake/cost-efficient-v1",
                returned_id="fake/cost-efficient-v1",
                family="fake-series",
                tier="cost_efficient",
                thinking="disabled",
                pricing_cny=ModelPricingCNY(
                    observed_at=date(2026, 7, 26),
                    input_cache_hit_per_million_cny=0.02,
                    input_cache_miss_per_million_cny=1.0,
                    output_per_million_cny=2.0,
                ),
            ),
        ),
        host_versions={"codex": "fake-codex-v1", "claude_code": "fake-claude-v1"},
        joinlint_commit="0" * 40,
        harness_version="fake-harness-v2",
        inference_policy_version="fake-policy-v2",
        relationship_scope="verified_inference",
        image_digest=f"sha256:{'0' * 64}",
        image_reference=f"ghcr.io/example/joinlint-formal@sha256:{'0' * 64}",
    )


def fake_manifest() -> FormalManifestV2:
    tasks: list[FormalTask] = []
    for database_index in range(12):
        for task_index in range(5):
            key = f"confirmatory:{database_index}:{task_index}"
            no_safe_path = database_index == 0 and task_index == 4
            tasks.append(
                FormalTask(
                    task_id=key,
                    database_id=f"natural-db-{database_index}",
                    database_variant_group=f"natural-group-{database_index}",
                    corpus="natural",
                    split="confirmatory",
                    domain=f"domain-{database_index % 6}",
                    source_type="sqlite",
                    database_scale=("small", "medium", "large")[database_index % 3],
                    ambiguity=("none", "low", "high")[task_index % 3],
                    fanout_type=(
                        "none",
                        "one_to_many",
                        "many_to_many",
                        "compound",
                    )[task_index % 4],
                    question_sha256=_digest(f"question:{key}"),
                    schema_sha256=_digest(f"schema:{database_index}"),
                    sql_shape_sha256=_digest(f"sql:{key}"),
                    schema_family_id=f"natural-schema-{database_index}",
                    question_template_id=f"natural-question-{database_index}-{task_index}",
                    sql_structure_id=f"natural-sql-{database_index}-{task_index}",
                    allowed_graphs=(
                        ()
                        if no_safe_path
                        else ((('orders.customer_id', 'customers.id'),),)
                    ),
                    oracle_has_safe_path=not no_safe_path,
                    join_depth=0 if no_safe_path else 1,
                )
            )
    for task_index in range(20):
        key = f"diagnostic:{task_index}"
        tasks.append(
            FormalTask(
                task_id=key,
                database_id=f"diagnostic-db-{task_index // 5}",
                database_variant_group=f"diagnostic-group-{task_index // 5}",
                corpus="semantic_join_failure",
                split="diagnostic",
                domain="diagnostic",
                source_type="sqlite",
                database_scale="small",
                ambiguity="high",
                fanout_type="compound",
                question_sha256=_digest(f"question:{key}"),
                schema_sha256=_digest(f"schema:{key}"),
                sql_shape_sha256=_digest(f"sql:{key}"),
                schema_family_id=f"diagnostic-schema-{task_index // 5}",
                question_template_id=f"diagnostic-question-{task_index}",
                sql_structure_id=f"diagnostic-sql-{task_index}",
                allowed_graphs=((('orders.customer_id', 'customers.id'),),),
                oracle_has_safe_path=True,
                join_depth=1,
            )
        )
    return FormalManifestV2(dataset_release="synthetic-v2", tasks=tuple(tasks))


def fake_relationship_rows() -> list[RelationshipResultRow]:
    rows: list[RelationshipResultRow] = []
    for index in range(24):
        ablated = index % 2 == 1
        rows.append(
            RelationshipResultRow(
                case_id=f"declared-{index}",
                database_id=f"declared-db-{index // 2}-{index % 2}",
                variant_group=f"declared-group-{index // 2}",
                corpus="natural",
                source_type="sqlite",
                domain=f"domain-{index % 6}",
                split="confirmatory",
                provenance="verified" if ablated else "declared",
                eligible=True,
                expected_relation=True,
                extracted=True,
                auto_usable=True,
                prediction_correct=True,
                expected_edges=(("orders.customer_id", "customers.id"),),
                predicted_edges=(("orders.customer_id", "customers.id"),),
                direction_correct=True,
                evidence_mode="exact",
                stale=False,
                fk_state="ablated" if ablated else "visible",
                reviewer_labels=(True, True) if ablated else None,
                adjudicated_label=True if ablated else None,
            )
        )
    for index in range(250):
        rows.append(
            RelationshipResultRow(
                case_id=f"verified-{index}",
                database_id=f"verified-db-{index % 20}",
                variant_group=f"verified-group-{index % 20}",
                corpus="natural",
                source_type="sqlite" if index % 2 == 0 else "csv",
                domain=f"domain-{index % 6}",
                split="confirmatory",
                provenance="verified",
                eligible=True,
                expected_relation=True,
                extracted=True,
                auto_usable=True,
                prediction_correct=True,
                expected_edges=(("orders.customer_id", "customers.id"),),
                predicted_edges=(("orders.customer_id", "customers.id"),),
                direction_correct=True,
                evidence_mode="exact",
                stale=False,
                fk_state="absent",
                reviewer_labels=(True, True),
                adjudicated_label=True,
            )
        )
    for index in range(240):
        rows.append(
            RelationshipResultRow(
                case_id=f"adversarial-{index}",
                database_id=f"adversarial-db-{index % 24}",
                variant_group=f"adversarial-group-{index % 24}",
                corpus="adversarial",
                source_type="sqlite" if index % 2 == 0 else "csv",
                domain=f"trap-{index % 6}",
                split="confirmatory",
                provenance="verified",
                eligible=True,
                expected_relation=False,
                extracted=True,
                auto_usable=False,
                prediction_correct=False,
                expected_edges=(("orders.customer_id", "customers.id"),),
                predicted_edges=(("orders.id", "customers.id"),),
                direction_correct=False,
                evidence_mode="exact",
                stale=False,
                adversarial_type=(
                    "same_name_id",
                    "integer_domain_collision",
                    "small_domain_overlap",
                    "multiple_parents",
                    "non_unique_parent",
                    "orphan",
                    "drift",
                )[index % 7],
                reviewer_labels=(False, False),
                adjudicated_label=False,
            )
        )
    return rows


def fake_sql_rows() -> list[SQLValidationResultRow]:
    shapes = (
        "alias",
        "cte",
        "subquery",
        "where_equality",
        "using",
        "self_join",
        "composite",
        "wrong_edge",
        "grain_change",
        "many_to_many",
        "compound_fanout",
        "ddl_dml",
        "multi_statement",
    )
    danger_types = (
        "unsupported_edge",
        "missing_required_edge",
        "uniqueness",
        "orphan",
        "cardinality",
        "grain",
        "many_to_many",
        "compound_fanout",
        "unsafe_statement",
        "multi_statement",
    )
    rows = [
        SQLValidationResultRow(
            case_id=f"safe-{index}",
            sql_class="safe",
            supported_shape=True,
            sql_shape=shapes[index % len(shapes)],
            danger_type=None,
            decision="pass",
            extracted_edges_correct=True,
            repair_uses_only_auto_usable=True,
            execution_count=0,
        )
        for index in range(200)
    ]
    rows.extend(
        SQLValidationResultRow(
            case_id=f"dangerous-{index}",
            sql_class="dangerous",
            supported_shape=True,
            sql_shape=shapes[index % len(shapes)],
            danger_type=danger_types[index % len(danger_types)],
            decision="block",
            extracted_edges_correct=True,
            repair_uses_only_auto_usable=True,
            execution_count=0,
        )
        for index in range(200)
    )
    return rows


def fake_performance() -> PerformanceMeasurement:
    return PerformanceMeasurement(
        cached_plan_p95_ms=100,
        cached_validation_p95_ms=100,
        metadata_plan_p95_ms=250,
        million_row_seconds=5,
        million_row_peak_rss_bytes=256 * 1024 * 1024,
    )


def fake_deterministic_evidence() -> DeterministicEvidenceBundle:
    return DeterministicEvidenceBundle(
        lineage_id=FAKE_LINEAGE_ID,
        deterministic_suite_sha256="3" * 64,
        producer="joinlint-formal-deterministic-v2",
        relationship_rows=tuple(fake_relationship_rows()),
        sql_validation_rows=tuple(fake_sql_rows()),
        performance=fake_performance(),
    )


def fake_blind_review(
    agent_bundle: AgentResultBundle,
    run_plan: RunPlanV2,
) -> BlindReviewBundle:
    sample_ids = run_plan.blind_review_sample_ids
    rows = {row.sample_id: row for row in agent_bundle.rows}
    return BlindReviewBundle(
        lineage_id=agent_bundle.lineage_id,
        formal_results_sha256=digest_value(agent_bundle.model_dump(mode="json")),
        run_plan_sha256=agent_bundle.run_plan_sha256,
        review_protocol_version="fake-arm-blind-v1",
        arm_labels_removed=True,
        decisions=tuple(
            BlindReviewDecision(
                sample_id=sample_id,
                output_sha256=rows[sample_id].output_sha256,
                reviewer_id_sha256="2" * 64,
                automated_join_correct=rows[sample_id].join_correct_task_completion,
                human_join_correct=rows[sample_id].join_correct_task_completion,
            )
            for sample_id in sample_ids
        ),
    )


def fake_agent_artifacts() -> tuple[RunPlanV2, AgentResultBundle, BlindReviewBundle]:
    run_plan = build_run_plan(fake_manifest(), fake_preregistration(), FAKE_LINEAGE_ID)
    bundle = AgentResultBundle(
        lineage_id=FAKE_LINEAGE_ID,
        run_plan_sha256=digest_value(run_plan.model_dump(mode="json")),
        rows=tuple(fake_agent_rows()),
    )
    return run_plan, bundle, fake_blind_review(bundle, run_plan)


def fake_agent_rows() -> list[AgentResultRow]:
    rows: list[AgentResultRow] = []
    models = ("fake/high-capability-v1", "fake/cost-efficient-v1")
    hosts = ("codex", "claude_code")
    for model_id in models:
        for host in hosts:
            for database_index in range(12):
                for task_index in range(5):
                    for repetition in range(3):
                        no_safe_path = database_index == 0 and task_index == 4
                        control_success = task_index < 3 and not no_safe_path
                        rows.append(
                            _agent_row(
                                model_id=model_id,
                                host=host,
                                database_index=database_index,
                                task_index=task_index,
                                repetition=repetition,
                                condition="control",
                                success=control_success,
                                no_safe_path=no_safe_path,
                            )
                        )
                        rows.append(
                            _agent_row(
                                model_id=model_id,
                                host=host,
                                database_index=database_index,
                                task_index=task_index,
                                repetition=repetition,
                                condition="treatment",
                                success=True,
                                no_safe_path=no_safe_path,
                            )
                        )
    for model_id in models:
        for host in hosts:
            for task_index in range(20):
                for condition in ("oracle_inline", "oracle_mcp", "no_harness"):
                    mcp = condition != "oracle_inline"
                    diagnostic_sample_id = sample_id_for(
                        task_id=f"diagnostic:{task_index}",
                        database_id=f"diagnostic-db-{task_index // 5}",
                        corpus="semantic_join_failure",
                        condition=condition,
                        model_id=model_id,
                        host=host,
                        repetition=0,
                    )
                    rows.append(
                        AgentResultRow(
                            sample_id=diagnostic_sample_id,
                            task_id=f"diagnostic:{task_index}",
                            database_id=f"diagnostic-db-{task_index // 5}",
                            corpus="semantic_join_failure",
                            condition=condition,  # type: ignore[arg-type]
                            model_id=model_id,
                            host=host,  # type: ignore[arg-type]
                            domain="diagnostic",
                            source_type="sqlite",
                            database_scale="small",
                            join_depth=1,
                            ambiguity="high",
                            fanout_type="compound",
                            repetition=0,
                            output_sha256=_digest(f"output:{diagnostic_sample_id}"),
                            artifact_complete=True,
                            oracle_has_safe_path=True,
                            submitted_sql=True,
                            safe_abstention=False,
                            join_graph_correct=True,
                            evaluator_validation_passed=True,
                            join_correct_task_completion=True,
                            dangerous_sql_submitted=False,
                            execution_correct=True,
                            plan_called=True if mcp else None,
                            plan_usable=True if mcp else None,
                            complete_entity_planning=True if mcp else None,
                            final_sql_validated=True if mcp else None,
                            mcp_grounded=True if mcp else None,
                            protocol_compliant=True if mcp else None,
                            protocol_violation=None,
                            blocking_applicable=False if mcp else None,
                            blocking_compliant=None,
                            bypassed=False if mcp else None,
                            tool_error=False if mcp else None,
                            total_time_seconds=1.0,
                            input_tokens=100,
                            input_cache_read_tokens=25,
                            input_cache_write_tokens=0,
                            output_tokens=50,
                            calculated_cost_cny=0.0001,
                            provider_reported_cost_usd=None,
                        )
                    )
    return rows


def _agent_row(
    *,
    model_id: str,
    host: str,
    database_index: int,
    task_index: int,
    repetition: int,
    condition: str,
    success: bool,
    no_safe_path: bool = False,
) -> AgentResultRow:
    mcp = condition == "treatment"
    safe_abstention = mcp and no_safe_path
    submitted_sql = not safe_abstention
    completion = success or safe_abstention
    return AgentResultRow(
        sample_id=sample_id_for(
            task_id=f"confirmatory:{database_index}:{task_index}",
            database_id=f"natural-db-{database_index}",
            corpus="natural",
            condition=condition,
            model_id=model_id,
            host=host,
            repetition=repetition,
        ),
        task_id=f"confirmatory:{database_index}:{task_index}",
        database_id=f"natural-db-{database_index}",
        corpus="natural",
        condition=condition,  # type: ignore[arg-type]
        model_id=model_id,
        host=host,  # type: ignore[arg-type]
        domain=f"domain-{database_index % 6}",
        source_type="sqlite",
        database_scale=("small", "medium", "large")[database_index % 3],  # type: ignore[arg-type]
        join_depth=0 if no_safe_path else 1,
        ambiguity=("none", "low", "high")[task_index % 3],  # type: ignore[arg-type]
        fanout_type=("none", "one_to_many", "many_to_many", "compound")[
            task_index % 4
        ],  # type: ignore[arg-type]
        repetition=repetition,
        output_sha256=_digest(
            f"output:{model_id}:{host}:{database_index}:{task_index}:{repetition}:{condition}"
        ),
        artifact_complete=True,
        oracle_has_safe_path=not no_safe_path,
        submitted_sql=submitted_sql,
        safe_abstention=safe_abstention,
        join_graph_correct=success and not safe_abstention,
        evaluator_validation_passed=success and not safe_abstention,
        join_correct_task_completion=completion,
        dangerous_sql_submitted=submitted_sql and not success,
        execution_correct=None if safe_abstention else success,
        plan_called=True if mcp else None,
        plan_usable=not safe_abstention if mcp else None,
        complete_entity_planning=True if mcp else None,
        final_sql_validated=not safe_abstention if mcp else None,
        mcp_grounded=not safe_abstention if mcp else None,
        protocol_compliant=True if mcp else None,
        protocol_violation=None,
        blocking_applicable=safe_abstention if mcp else None,
        blocking_compliant=True if safe_abstention else None,
        bypassed=False if mcp else None,
        tool_error=False if mcp else None,
        total_time_seconds=1.0,
        input_tokens=100,
        input_cache_read_tokens=25,
        input_cache_write_tokens=0,
        output_tokens=50,
        calculated_cost_cny=0.0001,
        provider_reported_cost_usd=None,
        failure_code=None if completion else "WRONG_PLAN",
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(canonical_json(value))
