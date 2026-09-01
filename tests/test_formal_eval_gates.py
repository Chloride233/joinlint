from __future__ import annotations

import pytest

from benchmarks.formal_eval.contracts import (
    AgentResultRow,
    RelationshipResultRow,
    SQLValidationResultRow,
)
from benchmarks.formal_eval.fake import (
    fake_agent_artifacts,
    fake_agent_rows,
    fake_performance,
    fake_preregistration,
    fake_relationship_rows,
    fake_sql_rows,
)
from benchmarks.formal_eval.gates import (
    agent_product_gate,
    diagnostic_canary_gate,
    holm_adjust,
    relationship_gate,
    sql_validation_gate,
    wilson_lower,
)


def test_fake_rows_pass_all_formal_gates() -> None:
    preregistration = fake_preregistration()

    relationship = relationship_gate(fake_relationship_rows())
    sql = sql_validation_gate(fake_sql_rows(), fake_performance())
    rows = fake_agent_rows()
    run_plan, agent_bundle, review = fake_agent_artifacts()
    agent = agent_product_gate(
        rows,
        review,
        run_plan=run_plan,
        agent_results_sha256=review.formal_results_sha256,
        thresholds=preregistration.thresholds,
        bootstrap_draws=100,
        seed=preregistration.seed,
    )

    assert relationship.passed
    assert len(rows) == 1680
    assert diagnostic_canary_gate(rows).passed
    assert relationship.verified_wilson_lower > 0.95
    assert sql.passed
    assert agent.publication_allowed
    assert agent.completion_effect.point == pytest.approx(0.4)
    assert agent.funnel["validation_called"] == 708
    assert agent.reliability.repetitions_required == 3
    assert agent.reliability.strict_completion_rates == pytest.approx(
        {"control": 0.6, "treatment": 1.0}
    )
    assert agent.reliability.strict_completion_cells == {
        "control": 144,
        "treatment": 240,
    }
    assert agent.reliability.total_cells == {"control": 240, "treatment": 240}
    assert agent.reliability.answerability_by_condition[
        "control"
    ].safe_abstention_rate == 0.0
    assert agent.reliability.answerability_by_condition[
        "control"
    ].unsafe_submission_rate == 1.0
    assert agent.reliability.answerability_by_condition[
        "treatment"
    ].safe_abstention_rate == 1.0
    assert agent.reliability.answerability_by_condition[
        "treatment"
    ].unsafe_submission_rate == 0.0


def test_strict_reliability_requires_every_repetition_to_succeed() -> None:
    preregistration = fake_preregistration()
    rows = fake_agent_rows()
    run_plan, _, review = fake_agent_artifacts()
    reviewed_ids = {decision.sample_id for decision in review.decisions}
    index = next(
        index
        for index, row in enumerate(rows)
        if row.corpus == "natural"
        and row.condition == "treatment"
        and row.oracle_has_safe_path
        and row.sample_id not in reviewed_ids
    )
    payload = rows[index].model_dump(mode="python")
    payload.update(
        join_graph_correct=False,
        evaluator_validation_passed=False,
        join_correct_task_completion=False,
        dangerous_sql_submitted=True,
        execution_correct=False,
        failure_code="WRONG_PLAN",
    )
    rows[index] = AgentResultRow.model_validate(payload)

    agent = agent_product_gate(
        rows,
        review,
        run_plan=run_plan,
        agent_results_sha256=review.formal_results_sha256,
        thresholds=preregistration.thresholds,
        bootstrap_draws=100,
        seed=preregistration.seed,
    )

    assert agent.reliability.strict_completion_cells["treatment"] == 239
    assert agent.reliability.strict_completion_rates["treatment"] == pytest.approx(
        239 / 240
    )


def test_relationship_gate_separately_blocks_adversarial_false_promotion() -> None:
    rows = fake_relationship_rows()
    index = next(index for index, row in enumerate(rows) if row.corpus == "adversarial")
    payload = rows[index].model_dump(mode="python")
    payload["auto_usable"] = True
    rows[index] = RelationshipResultRow.model_validate(payload)

    report = relationship_gate(rows)

    assert report.adversarial_false_promotions == 1
    assert not report.passed


def test_relationship_gate_requires_every_adversarial_trap_type() -> None:
    rows = fake_relationship_rows()
    rows = [
        RelationshipResultRow.model_validate(
            {
                **row.model_dump(mode="python"),
                "adversarial_type": "same_name_id",
            }
        )
        if row.adversarial_type == "drift"
        else row
        for row in rows
    ]

    assert not relationship_gate(rows).passed


def test_relationship_gate_requires_balanced_adversarial_traps() -> None:
    rows = fake_relationship_rows()
    changed = 0
    skewed: list[RelationshipResultRow] = []
    for row in rows:
        if row.adversarial_type == "drift" and changed < 10:
            skewed.append(
                RelationshipResultRow.model_validate(
                    {**row.model_dump(mode="python"), "adversarial_type": "same_name_id"}
                )
            )
            changed += 1
        else:
            skewed.append(row)

    report = relationship_gate(skewed)

    assert set(report.adversarial_type_counts) == {
        "same_name_id",
        "integer_domain_collision",
        "small_domain_overlap",
        "multiple_parents",
        "non_unique_parent",
        "orphan",
        "drift",
    }
    assert not report.passed


def test_relationship_gate_requires_visible_and_ablated_declared_pairs() -> None:
    rows = [
        RelationshipResultRow.model_validate(
            {**row.model_dump(mode="python"), "fk_state": "visible"}
        )
        if row.corpus == "natural" and row.fk_state == "ablated"
        else row
        for row in fake_relationship_rows()
    ]

    report = relationship_gate(rows)

    assert report.declared_variant_pair_count == 0
    assert not report.passed


def test_relationship_gate_requires_twelve_annotated_absent_fk_databases() -> None:
    rows = [
        RelationshipResultRow.model_validate(
            {**row.model_dump(mode="python"), "fk_state": "ablated"}
        )
        if row.corpus == "natural" and row.provenance == "verified"
        else row
        for row in fake_relationship_rows()
    ]

    report = relationship_gate(rows)

    assert report.annotated_absent_fk_database_count == 0
    assert not report.passed


def test_declared_only_relationship_gate_does_not_require_stage2_inference() -> None:
    rows = [
        row
        for row in fake_relationship_rows()
        if row.corpus == "natural"
        and row.provenance == "declared"
        and row.fk_state == "visible"
    ]
    rows = [row.model_copy(update={"domain": f"domain-{index % 6}"}) for index, row in enumerate(rows)]

    report = relationship_gate(rows, relationship_scope="declared_only")

    assert report.passed
    assert report.declared_recall == 1.0
    assert report.verified_auto_usable == 0
    assert report.general_precision_claim_allowed is False


def test_declared_only_relationship_gate_rejects_non_declared_auto_use() -> None:
    rows = [
        row
        for row in fake_relationship_rows()
        if row.corpus == "natural"
        and row.provenance == "declared"
        and row.fk_state == "visible"
    ]
    rows = [row.model_copy(update={"domain": f"domain-{index % 6}"}) for index, row in enumerate(rows)]
    rows[0] = rows[0].model_copy(update={"provenance": "verified"})

    report = relationship_gate(rows, relationship_scope="declared_only")

    assert not report.passed


def test_sql_gate_distinguishes_dangerous_acceptance_from_false_block() -> None:
    rows = fake_sql_rows()
    dangerous_index = next(
        index for index, row in enumerate(rows) if row.sql_class == "dangerous"
    )
    dangerous = rows[dangerous_index].model_dump(mode="python")
    dangerous["decision"] = "pass"
    rows[dangerous_index] = SQLValidationResultRow.model_validate(dangerous)

    report = sql_validation_gate(rows, fake_performance())

    assert report.dangerous_accepted == 1
    assert report.dangerous_not_blocked == 1
    assert not report.passed


def test_dangerous_acceptance_rate_counts_every_nonblocking_outcome() -> None:
    rows = fake_sql_rows()
    dangerous_index = next(
        index for index, row in enumerate(rows) if row.sql_class == "dangerous"
    )
    payload = rows[dangerous_index].model_dump(mode="python")
    payload["decision"] = "inconclusive"
    rows[dangerous_index] = SQLValidationResultRow.model_validate(payload)

    report = sql_validation_gate(rows, fake_performance())

    assert report.dangerous_accepted == 0
    assert report.dangerous_not_blocked == 1
    assert report.dangerous_acceptance_rate == pytest.approx(1 / 200)
    assert not report.passed


def test_sql_gate_requires_every_preregistered_shape() -> None:
    rows = [
        SQLValidationResultRow.model_validate(
            {
                **row.model_dump(mode="python"),
                "sql_shape": "alias",
                "danger_type": "unsupported_edge"
                if row.sql_class == "dangerous"
                else None,
            }
        )
        if row.sql_shape == "cte"
        else row
        for row in fake_sql_rows()
    ]

    assert not sql_validation_gate(rows, fake_performance()).passed


def test_sql_gate_requires_every_danger_taxonomy_category() -> None:
    rows = [
        SQLValidationResultRow.model_validate(
            {
                **row.model_dump(mode="python"),
                "danger_type": "unsupported_edge",
            }
        )
        if row.danger_type == "orphan"
        else row
        for row in fake_sql_rows()
    ]

    assert not sql_validation_gate(rows, fake_performance()).passed


def test_agent_gate_rejects_missing_pairs() -> None:
    rows = fake_agent_rows()
    rows.pop(0)
    run_plan, _, review = fake_agent_artifacts()

    with pytest.raises(ValueError, match="one control and one treatment"):
        agent_product_gate(
            rows,
            review,
            run_plan=run_plan,
            agent_results_sha256=review.formal_results_sha256,
            bootstrap_draws=10,
        )


def test_agent_gate_fails_closed_without_the_blind_review_floor() -> None:
    run_plan, bundle, review = fake_agent_artifacts()
    insufficient = review.model_copy(update={"decisions": review.decisions[:80]})
    report = agent_product_gate(
        bundle.rows,
        insufficient,
        run_plan=run_plan,
        agent_results_sha256=review.formal_results_sha256,
        bootstrap_draws=10,
    )

    assert not report.publication_allowed
    assert "BLIND_REVIEW_SAMPLE" in report.failed_publication_gates


def test_agent_gate_rejects_review_from_another_result_bundle() -> None:
    run_plan, bundle, review = fake_agent_artifacts()
    wrong_result_review = review.model_copy(update={"formal_results_sha256": "f" * 64})

    report = agent_product_gate(
        bundle.rows,
        wrong_result_review,
        run_plan=run_plan,
        agent_results_sha256=review.formal_results_sha256,
        bootstrap_draws=10,
    )

    assert not report.publication_allowed
    assert "BLIND_REVIEW_SAMPLE" in report.failed_publication_gates


def test_agent_gate_requires_every_model_host_task_cell() -> None:
    run_plan, bundle, review = fake_agent_artifacts()
    rows = tuple(
        row
        for row in bundle.rows
        if not (
            row.corpus == "natural"
            and row.model_id == "fake/cost-efficient-v1"
            and row.host == "claude_code"
            and row.task_id == "confirmatory:0:0"
        )
    )

    with pytest.raises(ValueError, match="every model-host cell"):
        agent_product_gate(
            rows,
            review,
            run_plan=run_plan,
            agent_results_sha256=review.formal_results_sha256,
            bootstrap_draws=10,
        )


def test_wilson_and_holm_are_deterministic() -> None:
    assert wilson_lower(250, 250) > 0.98
    assert holm_adjust([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
