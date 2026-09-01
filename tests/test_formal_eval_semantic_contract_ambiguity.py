from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from benchmarks.formal_eval.contracts import FormalManifestV2, SealedAgentTask
from benchmarks.formal_eval.manifest import load_document, verify_sealed_manifest
from benchmarks.formal_eval.pilot_stage import exact_two_sided_mcnemar_p_value
from benchmarks.formal_eval.semantic_contract_ambiguity import (
    DATASET_RELEASE,
    MINIMUM_UNOPPOSED_TREATMENT_WINS,
    build_semantic_contract_ambiguity_v1,
)
from joinlint.mcp_contracts import GetJoinPlanRequest, ValidateSQLRequest
from joinlint.runtime.cache import RuntimeCache
from joinlint.runtime.domain import EntityRef
from joinlint.runtime.service import RuntimeService


def test_ambiguity_bundle_is_deterministic_and_passes_frozen_design_gates(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "one"
    second_root = tmp_path / "two"
    first_root.mkdir()
    second_root.mkdir()
    first = first_root / DATASET_RELEASE
    second = second_root / DATASET_RELEASE

    first_report = build_semantic_contract_ambiguity_v1(first_root, first)
    second_report = build_semantic_contract_ambiguity_v1(second_root, second)

    assert first_report == second_report
    for name in ("agent-tasks.json", "manifest.json", "diagnostic-catalog.json"):
        assert (first / name).read_bytes() == (second / name).read_bytes()

    tasks = TypeAdapter(list[SealedAgentTask]).validate_json(
        (first / "agent-tasks.json").read_bytes()
    )
    manifest = load_document(first / "manifest.json", FormalManifestV2)
    catalog = json.loads((first / "diagnostic-catalog.json").read_bytes())
    verify_sealed_manifest(manifest, tasks)

    assert first_report["dataset_release"] == DATASET_RELEASE
    assert first_report["construction_rule"] == ("all_preconstructed_opaque_relationship_tasks_v1")
    assert first_report["outcome_based_task_selection"] is False
    assert len(tasks) == len(manifest.tasks) == len(catalog) == 20
    assert len({task.database_id for task in tasks}) == 4
    assert all(task.query_contract is not None for task in tasks)
    assert all("REFERENCES" not in task.schema_text for task in tasks)
    assert all(row["relationship_columns_opaque"] for row in catalog)
    assert all(row["type_compatible_candidate_graph_count"] == 2 for row in catalog)


def test_exact_test_boundary_is_fixed_before_model_outcomes() -> None:
    assert MINIMUM_UNOPPOSED_TREATMENT_WINS == 6
    assert exact_two_sided_mcnemar_p_value(treatment_wins=5, control_wins=0) == 0.0625
    assert exact_two_sided_mcnemar_p_value(treatment_wins=6, control_wins=0) == 0.03125


def test_every_ambiguity_task_has_a_joinlint_proof_for_only_the_gold_graph(
    tmp_path: Path,
) -> None:
    sealed_root = tmp_path / "sealed"
    sealed_root.mkdir()
    output = sealed_root / DATASET_RELEASE
    build_semantic_contract_ambiguity_v1(sealed_root, output)
    tasks = TypeAdapter(list[SealedAgentTask]).validate_json(
        (output / "agent-tasks.json").read_bytes()
    )
    catalog = {
        row["task_id"]: row for row in json.loads((output / "diagnostic-catalog.json").read_bytes())
    }

    for task in tasks:
        assert task.query_contract is not None
        service = RuntimeService(
            sealed_root,
            (task.database_path,),
            cache=RuntimeCache(tmp_path / "cache" / task.task_id),
        )
        refs = tuple(
            EntityRef(ref=entity, entity=entity) for entity in task.query_contract.required_entities
        )
        plan = service.get_join_plan(
            GetJoinPlanRequest(
                entity_refs=refs,
                start_ref=task.query_contract.row_grain_entity,
                expected_grain_ref=task.query_contract.row_grain_entity,
            )
        )

        assert plan.status == "ok", task.task_id
        assert plan.data is not None
        gold = service.validate_sql(
            ValidateSQLRequest(sql=task.gold_sql, plan_id=plan.data.proof.plan_id)
        )
        dangerous = service.validate_sql(
            ValidateSQLRequest(
                sql=catalog[task.task_id]["dangerous_sql"],
                plan_id=plan.data.proof.plan_id,
            )
        )

        assert gold.status == "ok", task.task_id
        assert dangerous.status == "findings", task.task_id
