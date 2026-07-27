from __future__ import annotations

from benchmarks.formal_eval.fake import FAKE_LINEAGE_ID, fake_agent_rows, fake_manifest
from benchmarks.formal_eval.fake import fake_preregistration
from benchmarks.formal_eval.power import power_plan_from_pilot, validate_power_readiness
from benchmarks.formal_eval.run_plan import build_run_plan


def test_power_plan_preserves_the_preregistered_floor() -> None:
    plan = power_plan_from_pilot(fake_agent_rows(), lineage_id=FAKE_LINEAGE_ID)

    assert plan.pilot_database_count == 12
    assert plan.required_database_count == 12
    assert plan.required_task_count == 60
    assert plan.minimum_formal_run_count == 1440


def test_power_plan_blocks_an_underpowered_manifest() -> None:
    plan = power_plan_from_pilot(
        fake_agent_rows(),
        lineage_id=FAKE_LINEAGE_ID,
        minimum_databases=20,
    )
    manifest = fake_manifest()
    run_plan = build_run_plan(manifest, fake_preregistration(), FAKE_LINEAGE_ID)

    readiness = validate_power_readiness(plan, manifest, run_plan)

    assert not readiness.ready
    assert readiness.required_database_count == 20
    assert readiness.actual_database_count == 12
