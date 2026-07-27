from __future__ import annotations

from benchmarks.formal_eval.fake import FAKE_LINEAGE_ID, fake_manifest, fake_preregistration
from benchmarks.formal_eval.run_plan import build_run_plan, summarize_run_plan
from benchmarks.formal_eval.runtime_contract import check_tool_names


def test_run_plan_expands_the_frozen_two_by_two_matrix() -> None:
    plan = build_run_plan(fake_manifest(), fake_preregistration(), FAKE_LINEAGE_ID)

    assert summarize_run_plan(plan) == {
        "total": 1680,
        "confirmatory": 1440,
        "diagnostic": 240,
    }
    assert len({run.sample_id for run in plan.runs}) == 1680
    assert len(plan.blind_review_sample_ids) == 144


def test_runtime_contract_requires_only_the_completed_product_tools() -> None:
    assert check_tool_names({"get_join_plan", "validate_sql"}).ready
    assert not check_tool_names(
        {"get_data_model", "find_join_path", "validate_join"}
    ).ready
