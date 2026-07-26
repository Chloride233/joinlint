from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from statistics import fmean

from benchmarks.formal_eval.contracts import (
    AgentResultRow,
    BlindReviewBundle,
    PerformanceMeasurement,
    PublicationThresholds,
    RelationshipScope,
    RelationshipResultRow,
    SQLValidationResultRow,
    StrictModel,
)
from benchmarks.formal_eval.lineage import digest_value
from benchmarks.formal_eval.run_plan import RunPlanV2


class RelationshipGateReport(StrictModel):
    relationship_scope: RelationshipScope
    natural_database_count: int
    natural_domain_count: int
    declared_variant_pair_count: int
    annotated_absent_fk_database_count: int
    adversarial_candidate_count: int
    declared_eligible: int
    declared_extracted: int
    declared_recall: float
    curated_eligible: int
    curated_currently_supported: int
    adversarial_type_counts: dict[str, int]
    verified_auto_usable: int
    verified_correct: int
    verified_database_count: int
    verified_precision: float
    verified_wilson_lower: float
    verified_eligible_candidates: int
    verified_coverage: float
    verified_abstentions: int
    adversarial_false_promotions: int
    nonexact_promotions: int
    stale_promotions: int
    unsafe_auto_promotions: int
    general_precision_claim_allowed: bool
    passed: bool


class SQLGateReport(StrictModel):
    safe_count: int
    dangerous_count: int
    dangerous_accepted: int
    dangerous_not_blocked: int
    false_blocks: int
    edge_extraction_errors: int
    unsafe_repairs: int
    execution_count: int
    dangerous_acceptance_rate: float
    false_block_rate: float
    edge_extraction_accuracy: float
    repair_safety: float
    sql_shape_counts: dict[str, int]
    danger_type_counts: dict[str, int]
    performance: PerformanceMeasurement
    performance_passed: bool
    passed: bool


class Interval(StrictModel):
    lower: float
    upper: float


class EffectEstimate(StrictModel):
    point: float
    interval: Interval


class AgentGateReport(StrictModel):
    paired_unit_count: int
    database_count: int
    task_count: int
    model_count: int
    host_count: int
    completion_effect: EffectEstimate
    dangerous_submission_reduction: EffectEstimate
    execution_effect: EffectEstimate
    cell_completion_effects: dict[str, float]
    stratified_completion_effects: dict[str, float]
    treatment_rates: dict[str, float]
    funnel: dict[str, int]
    failure_counts: dict[str, int]
    database_completion_effects: dict[str, float]
    no_improvement_databases: tuple[str, ...]
    resource_usage: dict[str, dict[str, float | int]]
    blind_review_required: int
    blind_reviewed_runs: int
    blind_review_agreement: float
    publication_allowed: bool
    failed_publication_gates: tuple[str, ...]


class CanaryReport(StrictModel):
    row_count: int
    model_count: int
    host_count: int
    condition_counts: dict[str, int]
    infrastructure_failures: int
    passed: bool


def wilson_lower(successes: int, total: int, *, z: float = 1.959963984540054) -> float:
    if total <= 0 or not 0 <= successes <= total:
        raise ValueError("Wilson interval requires 0 <= successes <= total")
    proportion = successes / total
    denominator = 1.0 + z * z / total
    center = (proportion + z * z / (2.0 * total)) / denominator
    margin = (
        z
        * math.sqrt(proportion * (1.0 - proportion) / total + z * z / (4.0 * total * total))
        / denominator
    )
    return max(0.0, center - margin)


def diagnostic_canary_gate(
    rows: Sequence[AgentResultRow], *, expected_count: int = 240
) -> CanaryReport:
    diagnostic = [row for row in rows if row.corpus == "semantic_join_failure"]
    conditions = Counter(row.condition for row in diagnostic)
    infrastructure_codes = {
        "MODEL_TIMEOUT",
        "MCP_TOOL_ERROR",
        "INFRASTRUCTURE_FAILURE",
    }
    infrastructure_failures = sum(
        not row.artifact_complete or row.failure_code in infrastructure_codes
        for row in diagnostic
    )
    expected_conditions = {
        "oracle_inline": expected_count // 3,
        "oracle_mcp": expected_count // 3,
        "no_harness": expected_count // 3,
    }
    model_count = len({row.model_id for row in diagnostic})
    host_count = len({row.host for row in diagnostic})
    passed = (
        len(diagnostic) == expected_count
        and model_count == 2
        and host_count == 2
        and dict(conditions) == expected_conditions
        and infrastructure_failures == 0
    )
    return CanaryReport(
        row_count=len(diagnostic),
        model_count=model_count,
        host_count=host_count,
        condition_counts=dict(sorted(conditions.items())),
        infrastructure_failures=infrastructure_failures,
        passed=passed,
    )


def relationship_gate(
    rows: Sequence[RelationshipResultRow],
    *,
    relationship_scope: RelationshipScope = "verified_inference",
    min_verified_edges: int = 250,
    min_verified_databases: int = 20,
    min_natural_databases: int = 24,
    min_natural_domains: int = 6,
    min_adversarial_candidates: int = 240,
) -> RelationshipGateReport:
    natural = [
        row for row in rows if row.corpus == "natural" and row.split == "confirmatory"
    ]
    adversarial = [
        row for row in rows if row.corpus == "adversarial" and row.split == "confirmatory"
    ]
    declared = [
        row
        for row in rows
        if row.corpus == "natural"
        and row.split == "confirmatory"
        and row.provenance == "declared"
        and row.eligible
        and row.expected_relation
    ]
    declared_extracted = sum(row.prediction_correct is True for row in declared)
    declared_recall = declared_extracted / len(declared) if declared else 0.0
    curated = [
        row
        for row in natural
        if row.provenance == "curated" and row.eligible and row.expected_relation
    ]
    curated_supported = sum(
        row.extracted and row.prediction_correct is True for row in curated
    )

    verified_candidates = [
        row
        for row in rows
        if row.corpus == "natural"
        and row.split == "confirmatory"
        and row.provenance == "verified"
        and row.eligible
    ]
    verified = [row for row in verified_candidates if row.auto_usable]
    verified_correct = sum(row.prediction_correct is True for row in verified)
    verified_precision = verified_correct / len(verified) if verified else 0.0
    verified_lower = wilson_lower(verified_correct, len(verified)) if verified else 0.0
    verified_databases = len({row.variant_group for row in verified})

    adversarial_false_promotions = sum(
        row.corpus == "adversarial"
        and row.auto_usable
        and row.prediction_correct is not True
        for row in rows
    )
    nonexact_promotions = sum(row.auto_usable and row.evidence_mode != "exact" for row in rows)
    stale_promotions = sum(row.auto_usable and row.stale for row in rows)
    unsafe_auto_promotions = sum(
        row.auto_usable and row.prediction_correct is not True for row in rows
    )
    enough_verified = (
        len(verified) >= min_verified_edges and verified_databases >= min_verified_databases
    )
    natural_database_count = len({row.variant_group for row in natural})
    natural_domain_count = len({row.domain for row in natural})
    declared_states: dict[str, set[str]] = defaultdict(set)
    for row in natural:
        if row.provenance == "declared" and row.fk_state == "visible":
            declared_states[row.variant_group].add("visible")
        if row.fk_state == "ablated":
            declared_states[row.variant_group].add("ablated")
    declared_variant_pair_count = sum(
        states >= {"visible", "ablated"} for states in declared_states.values()
    )
    annotated_absent_fk_database_count = len(
        {
            row.variant_group
            for row in natural
            if row.fk_state == "absent"
            and row.provenance in {"curated", "verified"}
            and row.reviewer_labels is not None
            and row.adjudicated_label is not None
        }
    )
    adversarial_type_counts = Counter(row.adversarial_type for row in adversarial)
    adversarial_balanced = bool(adversarial_type_counts) and (
        max(adversarial_type_counts.values()) - min(adversarial_type_counts.values()) <= 1
    )
    corpus_shape_passed = (
        natural_database_count >= min_natural_databases
        and natural_domain_count >= min_natural_domains
        and declared_variant_pair_count >= 12
        and annotated_absent_fk_database_count >= 12
        and len(adversarial) >= min_adversarial_candidates
        and set(adversarial_type_counts)
        == {
            "same_name_id",
            "integer_domain_collision",
            "small_domain_overlap",
            "multiple_parents",
            "non_unique_parent",
            "orphan",
            "drift",
        }
        and adversarial_balanced
    )
    precision_passed = verified_precision >= 0.98 and verified_lower >= 0.95
    if relationship_scope == "declared_only":
        declared_only_shape_passed = (
            natural_database_count >= 12
            and natural_domain_count >= 6
            and len(declared) >= 12
        )
        passed = (
            declared_only_shape_passed
            and declared_recall == 1.0
            and not any(row.auto_usable and row.provenance != "declared" for row in rows)
            and unsafe_auto_promotions == 0
            and nonexact_promotions == 0
            and stale_promotions == 0
        )
    else:
        passed = (
            bool(declared)
            and declared_recall == 1.0
            and corpus_shape_passed
            and enough_verified
            and precision_passed
            and adversarial_false_promotions == 0
            and unsafe_auto_promotions == 0
            and nonexact_promotions == 0
            and stale_promotions == 0
        )
    return RelationshipGateReport(
        relationship_scope=relationship_scope,
        natural_database_count=natural_database_count,
        natural_domain_count=natural_domain_count,
        declared_variant_pair_count=declared_variant_pair_count,
        annotated_absent_fk_database_count=annotated_absent_fk_database_count,
        adversarial_candidate_count=len(adversarial),
        declared_eligible=len(declared),
        declared_extracted=declared_extracted,
        declared_recall=declared_recall,
        curated_eligible=len(curated),
        curated_currently_supported=curated_supported,
        adversarial_type_counts=dict(
            sorted((str(key), value) for key, value in adversarial_type_counts.items())
        ),
        verified_auto_usable=len(verified),
        verified_correct=verified_correct,
        verified_database_count=verified_databases,
        verified_precision=verified_precision,
        verified_wilson_lower=verified_lower,
        verified_eligible_candidates=len(verified_candidates),
        verified_coverage=len(verified) / len(verified_candidates) if verified_candidates else 0.0,
        verified_abstentions=len(verified_candidates) - len(verified),
        adversarial_false_promotions=adversarial_false_promotions,
        nonexact_promotions=nonexact_promotions,
        stale_promotions=stale_promotions,
        unsafe_auto_promotions=unsafe_auto_promotions,
        general_precision_claim_allowed=(
            relationship_scope == "verified_inference"
            and enough_verified
            and precision_passed
        ),
        passed=passed,
    )


def sql_validation_gate(
    rows: Sequence[SQLValidationResultRow],
    performance: PerformanceMeasurement,
    *,
    min_safe: int = 200,
    min_dangerous: int = 200,
) -> SQLGateReport:
    safe = [row for row in rows if row.sql_class == "safe" and row.supported_shape]
    dangerous = [row for row in rows if row.sql_class == "dangerous"]
    dangerous_accepted = sum(row.decision == "pass" for row in dangerous)
    dangerous_not_blocked = sum(row.decision != "block" for row in dangerous)
    false_blocks = sum(row.decision != "pass" for row in safe)
    extraction_errors = sum(
        row.supported_shape and not row.extracted_edges_correct for row in rows
    )
    unsafe_repairs = sum(not row.repair_uses_only_auto_usable for row in rows)
    execution_count = sum(row.execution_count for row in rows)
    supported = [row for row in rows if row.supported_shape]
    shape_counts = Counter(row.sql_shape for row in rows)
    danger_counts = Counter(row.danger_type for row in dangerous)
    required_shapes = {
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
    }
    required_danger_types = {
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
    }
    performance_passed = (
        performance.cached_plan_p95_ms <= 500.0
        and performance.cached_validation_p95_ms <= 500.0
        and performance.metadata_plan_p95_ms <= 1_000.0
        and performance.million_row_seconds <= 60.0
        and performance.million_row_peak_rss_bytes < 1_073_741_824
    )
    passed = (
        len(safe) >= min_safe
        and len(dangerous) >= min_dangerous
        and dangerous_not_blocked == 0
        and false_blocks == 0
        and extraction_errors == 0
        and unsafe_repairs == 0
        and execution_count == 0
        and performance_passed
        and set(shape_counts) == required_shapes
        and set(danger_counts) == required_danger_types
    )
    return SQLGateReport(
        safe_count=len(safe),
        dangerous_count=len(dangerous),
        dangerous_accepted=dangerous_accepted,
        dangerous_not_blocked=dangerous_not_blocked,
        false_blocks=false_blocks,
        edge_extraction_errors=extraction_errors,
        unsafe_repairs=unsafe_repairs,
        execution_count=execution_count,
        dangerous_acceptance_rate=(
            dangerous_not_blocked / len(dangerous) if dangerous else 0.0
        ),
        false_block_rate=false_blocks / len(safe) if safe else 0.0,
        edge_extraction_accuracy=(
            1.0 - extraction_errors / len(supported) if supported else 0.0
        ),
        repair_safety=1.0 - unsafe_repairs / len(rows) if rows else 0.0,
        sql_shape_counts=dict(sorted(shape_counts.items())),
        danger_type_counts=dict(sorted((str(key), value) for key, value in danger_counts.items())),
        performance=performance,
        performance_passed=performance_passed,
        passed=passed,
    )


Pair = tuple[AgentResultRow, AgentResultRow]


def agent_product_gate(
    rows: Sequence[AgentResultRow],
    blind_review: BlindReviewBundle,
    *,
    run_plan: RunPlanV2,
    agent_results_sha256: str,
    thresholds: PublicationThresholds | None = None,
    bootstrap_draws: int = 10_000,
    seed: int = 20260725,
    min_databases: int = 12,
    min_tasks: int = 60,
    min_repetitions: int = 3,
) -> AgentGateReport:
    thresholds = thresholds or PublicationThresholds()
    formal_rows = [
        row
        for row in rows
        if row.corpus == "natural" and row.condition in {"control", "treatment"}
    ]
    pairs = _paired_units(formal_rows)
    if not pairs:
        raise ValueError("formal agent gate requires paired control and treatment rows")
    database_count = len({control.database_id for control, _ in pairs})
    task_count = len({control.task_id for control, _ in pairs})
    model_count = len({control.model_id for control, _ in pairs})
    host_count = len({control.host for control, _ in pairs})
    expected_ids = {run.sample_id for run in run_plan.runs if run.confirmatory}
    actual_ids = {row.sample_id for row in formal_rows}
    sample_shape_passed = (
        actual_ids == expected_ids
        and len(actual_ids) == len(formal_rows)
        and database_count >= min_databases
        and task_count >= min_tasks
        and model_count == 2
        and host_count == 2
        and run_plan.lineage_id == blind_review.lineage_id
    )

    completion_point = _equal_cell_effect(pairs, _completion_effect)
    dangerous_point = _equal_cell_effect(pairs, _dangerous_reduction)
    execution_point = _equal_cell_effect(pairs, _execution_effect)
    bootstrap = _cluster_bootstrap(pairs, draws=bootstrap_draws, seed=seed)
    completion_interval = _percentile_interval(bootstrap["completion"])
    dangerous_interval = _percentile_interval(bootstrap["dangerous"])
    execution_interval = _percentile_interval(bootstrap["execution"])
    cell_effects = _cell_effects(pairs, _completion_effect)
    database_effects = _group_effects(
        pairs,
        key=lambda pair: pair[0].database_id,
        metric=_completion_effect,
    )
    strata: dict[str, float] = {}
    dimensions = {
        "source_type": lambda pair: pair[0].source_type,
        "domain": lambda pair: pair[0].domain,
        "database_scale": lambda pair: pair[0].database_scale,
        "join_depth": lambda pair: str(pair[0].join_depth),
        "ambiguity": lambda pair: pair[0].ambiguity,
        "fanout_type": lambda pair: pair[0].fanout_type,
    }
    for dimension, key in dimensions.items():
        strata.update(
            {
                f"{dimension}={value}": effect
                for value, effect in _group_effects(
                    pairs, key=key, metric=_completion_effect
                ).items()
            }
        )

    treatment = [treatment for _, treatment in pairs]
    behavior_treatment = treatment
    blocking = [row for row in behavior_treatment if row.blocking_applicable]
    treatment_rates = {
        "complete_entity_planning": _bool_rate(
            behavior_treatment, lambda row: row.complete_entity_planning
        ),
        "final_sql_validation": _bool_rate(
            behavior_treatment, lambda row: row.final_sql_validated
        ),
        "mcp_grounding": _bool_rate(behavior_treatment, lambda row: row.mcp_grounded),
        "blocking_compliance": _bool_rate(blocking, lambda row: row.blocking_compliant),
        "bypass": _bool_rate(behavior_treatment, lambda row: row.bypassed),
        "tool_error": _bool_rate(behavior_treatment, lambda row: row.tool_error),
    }
    failed: list[str] = []
    if not sample_shape_passed:
        failed.append("SAMPLE_SHAPE")
    if completion_point < thresholds.completion_improvement_min:
        failed.append("COMPLETION_EFFECT")
    if completion_interval.lower <= 0.0:
        failed.append("COMPLETION_INTERVAL")
    if dangerous_point < 0.0:
        failed.append("DANGEROUS_SUBMISSION")
    if execution_interval.lower < thresholds.execution_noninferiority_margin:
        failed.append("EXECUTION_NONINFERIORITY")
    if any(value < thresholds.cell_harm_floor for value in cell_effects.values()):
        failed.append("CELL_HARM")
    run_plan_sha256 = digest_value(run_plan.model_dump(mode="json"))
    decisions = {decision.sample_id: decision for decision in blind_review.decisions}
    formal_by_id = {row.sample_id: row for row in formal_rows}
    expected_review_ids = set(run_plan.blind_review_sample_ids)
    blind_review_required = len(expected_review_ids)
    review_binding_valid = (
        blind_review.formal_results_sha256 == agent_results_sha256
        and blind_review.run_plan_sha256 == run_plan_sha256
        and set(decisions) == expected_review_ids
        and set(decisions) <= set(formal_by_id)
        and all(
            decision.automated_join_correct
            == formal_by_id[sample_id].join_correct_task_completion
            and decision.output_sha256 == formal_by_id[sample_id].output_sha256
            for sample_id, decision in decisions.items()
        )
    )
    blind_review_agreement = (
        fmean(
            float(decision.automated_join_correct == decision.human_join_correct)
            for decision in decisions.values()
        )
        if decisions
        else 0.0
    )
    if not review_binding_valid:
        failed.append("BLIND_REVIEW_SAMPLE")
    if blind_review_agreement < 0.95:
        failed.append("BLIND_REVIEW_AGREEMENT")
    behavior_checks = {
        "PLANNING_RATE": treatment_rates["complete_entity_planning"]
        >= thresholds.complete_entity_planning_min,
        "VALIDATION_RATE": treatment_rates["final_sql_validation"]
        >= thresholds.final_sql_validation_min,
        "GROUNDING_RATE": treatment_rates["mcp_grounding"] >= thresholds.mcp_grounding_min,
        "BLOCKING_COMPLIANCE": treatment_rates["blocking_compliance"]
        >= thresholds.blocking_compliance_min,
        "BYPASS_RATE": treatment_rates["bypass"] <= thresholds.bypass_max,
        "TOOL_ERROR_RATE": treatment_rates["tool_error"] <= thresholds.tool_error_max,
    }
    failed.extend(name for name, passed in behavior_checks.items() if not passed)

    funnel = {
        "eligible": len(treatment),
        "plan_called": sum(row.plan_called is True for row in treatment),
        "plan_usable": sum(row.plan_usable is True for row in treatment),
        "sql_grounded": sum(row.mcp_grounded is True for row in treatment),
        "validation_called": sum(row.final_sql_validated is True for row in treatment),
        "validation_passed": sum(row.evaluator_validation_passed for row in treatment),
        "join_correct": sum(row.join_graph_correct for row in treatment),
        "execution_correct": sum(row.execution_correct is True for row in treatment),
    }
    failure_counts = dict(
        sorted(
            Counter(
                f"{row.condition}:{row.failure_code}"
                for row in formal_rows
                if row.failure_code is not None
            ).items()
        )
    )
    return AgentGateReport(
        paired_unit_count=len(pairs),
        database_count=database_count,
        task_count=task_count,
        model_count=model_count,
        host_count=host_count,
        completion_effect=EffectEstimate(point=completion_point, interval=completion_interval),
        dangerous_submission_reduction=EffectEstimate(
            point=dangerous_point, interval=dangerous_interval
        ),
        execution_effect=EffectEstimate(point=execution_point, interval=execution_interval),
        cell_completion_effects=cell_effects,
        stratified_completion_effects=dict(sorted(strata.items())),
        treatment_rates=treatment_rates,
        funnel=funnel,
        failure_counts=failure_counts,
        database_completion_effects=database_effects,
        no_improvement_databases=tuple(
            database_id
            for database_id, value in database_effects.items()
            if value <= 0.0
        ),
        resource_usage={
            condition: _resource_summary(
                [row for row in formal_rows if row.condition == condition]
            )
            for condition in ("control", "treatment")
        },
        blind_review_required=blind_review_required,
        blind_reviewed_runs=len(decisions),
        blind_review_agreement=blind_review_agreement,
        publication_allowed=not failed,
        failed_publication_gates=tuple(failed),
    )


def holm_adjust(p_values: Sequence[float]) -> list[float]:
    if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in p_values):
        raise ValueError("p-values must be finite values within [0, 1]")
    ordered = sorted(enumerate(p_values), key=lambda item: item[1])
    adjusted = [0.0] * len(p_values)
    running = 0.0
    total = len(p_values)
    for rank, (index, value) in enumerate(ordered):
        running = max(running, min(1.0, (total - rank) * value))
        adjusted[index] = running
    return adjusted


def _paired_units(rows: Sequence[AgentResultRow]) -> list[Pair]:
    grouped: dict[tuple[str, str, str, str, int], dict[str, AgentResultRow]] = defaultdict(dict)
    for row in rows:
        key = (row.model_id, row.host, row.database_id, row.task_id, row.repetition)
        if row.condition in grouped[key]:
            raise ValueError("duplicate formal result row")
        grouped[key][row.condition] = row
    pairs: list[Pair] = []
    for conditions in grouped.values():
        if set(conditions) != {"control", "treatment"}:
            raise ValueError("every formal unit requires one control and one treatment row")
        pairs.append((conditions["control"], conditions["treatment"]))
    return pairs


def _completion_effect(pair: Pair) -> float:
    control, treatment = pair
    return float(treatment.join_correct_task_completion) - float(
        control.join_correct_task_completion
    )


def _dangerous_reduction(pair: Pair) -> float:
    control, treatment = pair
    return float(control.dangerous_sql_submitted) - float(treatment.dangerous_sql_submitted)


def _execution_effect(pair: Pair) -> float:
    control, treatment = pair
    return float(treatment.execution_correct is True) - float(control.execution_correct is True)


def _cell(pair: Pair) -> str:
    control, _ = pair
    return f"{control.model_id}|{control.host}"


def _cell_effects(pairs: Sequence[Pair], metric: Callable[[Pair], float]) -> dict[str, float]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for pair in pairs:
        grouped[_cell(pair)].append(metric(pair))
    return {name: fmean(values) for name, values in sorted(grouped.items())}


def _group_effects(
    pairs: Sequence[Pair],
    *,
    key: Callable[[Pair], str],
    metric: Callable[[Pair], float],
) -> dict[str, float]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for pair in pairs:
        grouped[key(pair)].append(metric(pair))
    return {name: fmean(values) for name, values in sorted(grouped.items())}


def _equal_cell_effect(pairs: Sequence[Pair], metric: Callable[[Pair], float]) -> float:
    return fmean(_cell_effects(pairs, metric).values())


def _cluster_bootstrap(pairs: Sequence[Pair], *, draws: int, seed: int) -> dict[str, list[float]]:
    if draws <= 0:
        raise ValueError("bootstrap draws must be positive")
    hierarchy: dict[str, dict[str, dict[int, dict[str, Pair]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(dict))
    )
    expected_cells = {_cell(pair) for pair in pairs}
    for pair in pairs:
        control, _ = pair
        cell = _cell(pair)
        leaf = hierarchy[control.database_id][control.task_id][control.repetition]
        if cell in leaf:
            raise ValueError("duplicate cell in bootstrap hierarchy")
        leaf[cell] = pair
    if any(
        set(cells) != expected_cells
        for tasks in hierarchy.values()
        for repetitions in tasks.values()
        for cells in repetitions.values()
    ):
        raise ValueError("bootstrap hierarchy requires every model-host cell")
    rng = random.Random(seed)
    samples = {"completion": [], "dangerous": [], "execution": []}
    metrics = {
        "completion": _completion_effect,
        "dangerous": _dangerous_reduction,
        "execution": _execution_effect,
    }
    for _ in range(draws):
        drawn: list[Pair] = []
        database_ids = sorted(hierarchy)
        for database_id in rng.choices(database_ids, k=len(database_ids)):
            tasks = hierarchy[database_id]
            task_ids = sorted(tasks)
            for task_id in rng.choices(task_ids, k=len(task_ids)):
                repetitions = tasks[task_id]
                repetition_ids = sorted(repetitions)
                for repetition in rng.choices(repetition_ids, k=len(repetition_ids)):
                    drawn.extend(
                        repetitions[repetition][cell]
                        for cell in sorted(expected_cells)
                    )
        for name, metric in metrics.items():
            samples[name].append(_equal_cell_effect(drawn, metric))
    return samples


def _percentile_interval(values: Sequence[float]) -> Interval:
    if not values:
        raise ValueError("interval requires at least one value")
    ordered = sorted(values)
    return Interval(
        lower=_nearest_rank(ordered, 0.025),
        upper=_nearest_rank(ordered, 0.975),
    )


def _nearest_rank(values: Sequence[float], probability: float) -> float:
    index = max(0, min(len(values) - 1, math.ceil(probability * len(values)) - 1))
    return values[index]


def _bool_rate(
    rows: Iterable[AgentResultRow], getter: Callable[[AgentResultRow], bool | None]
) -> float:
    values = [value for row in rows if (value := getter(row)) is not None]
    return fmean(float(value) for value in values) if values else 0.0


def _resource_summary(rows: Sequence[AgentResultRow]) -> dict[str, float | int]:
    times = sorted(row.total_time_seconds for row in rows)
    return {
        "run_count": len(rows),
        "input_tokens": sum(row.input_tokens for row in rows),
        "output_tokens": sum(row.output_tokens for row in rows),
        "cost_usd": sum(row.cost_usd for row in rows),
        "latency_mean_seconds": fmean(times) if times else 0.0,
        "latency_p95_seconds": _nearest_rank(times, 0.95) if times else 0.0,
    }
