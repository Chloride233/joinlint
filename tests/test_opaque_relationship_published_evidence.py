from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

import pytest

from benchmarks.formal_eval.pilot_stage import exact_two_sided_mcnemar_p_value
from benchmarks.formal_eval.public_artifacts import validate_public_artifacts
from joinlint.contracts import canonical_json


EVIDENCE_ROOT = (
    Path(__file__).parents[1]
    / "benchmarks"
    / "formal_eval"
    / "published"
    / "opaque-relationship-v1"
)
PILOT_STAGE_INVENTORY = (
    "stage.json",
    "stage-run-plan.json",
    "budget-checkpoint.json",
    "agent-results-bundle.json",
    "cleaning.json",
    "pilot-agent-results.json",
    "budget.json",
    "campaign-budget.json",
    "effect.json",
)


def _load_json(path: Path) -> object:
    return json.loads(path.read_bytes())


def test_opaque_relationship_result_is_hash_bound_and_recomputed() -> None:
    calibration_root = EVIDENCE_ROOT / "calibration"
    stage_root = EVIDENCE_ROOT / "pilot-stage"
    assert validate_public_artifacts(calibration_root, "pilot-calibration") == (
        "calibration.json",
    )
    assert validate_public_artifacts(stage_root, "pilot-stage") == PILOT_STAGE_INVENTORY

    result_lock = _load_json(EVIDENCE_ROOT / "result-lock.json")
    assert isinstance(result_lock, dict)
    assert result_lock["evidence_tier"] == "preregistered_bounded_synthetic_pilot"
    assert result_lock["claim_scope"] == "trusted_query_contract_opaque_relationship_stress"
    assert result_lock["excluded_claims"] == [
        "natural_sql_error_rates",
        "business_semantics",
        "undeclared_relationship_discovery",
        "pooling_or_rerun",
    ]
    assert result_lock["draft_release"]["draft"] is True
    assert result_lock["draft_release"]["immutable"] is False
    assert result_lock["draft_release"]["git_ref_exists"] is False
    for relative_path, expected_sha256 in result_lock["published_files"].items():
        assert hashlib.sha256((EVIDENCE_ROOT / relative_path).read_bytes()).hexdigest() == (
            expected_sha256
        )

    calibration = _load_json(calibration_root / "calibration.json")
    stage = _load_json(stage_root / "stage.json")
    effect = _load_json(stage_root / "effect.json")
    rows = _load_json(stage_root / "pilot-agent-results.json")
    budget = _load_json(stage_root / "budget.json")
    assert all(isinstance(value, dict) for value in (calibration, stage, effect, budget))
    assert isinstance(rows, list)

    assert calibration["status"] == "passed"
    assert calibration["readiness_status"] == "passed"
    assert calibration["workflow_run_id"] == 33248554896
    assert len(calibration["infrastructure"]["cells"]) == 8
    assert len(calibration["harness"]["cells"]) == 8
    assert len(calibration["scoring"]["cells"]) == 8
    assert len(calibration["resource"]["cells"]) == 8

    assert stage["joinlint_commit"] == result_lock["evaluated_commit"]
    assert stage["input_lock_sha256"] == result_lock["input_lock_sha256"]
    assert stage["workflow_run_id"] == 33249402590
    assert stage["stage_id"] == "semantic_join_contract_ambiguity_v1"
    assert stage["task_count"] == 20
    assert stage["run_count"] == 40
    assert len(rows) == 40

    assert effect == {
        "absolute_improvement": 0.7,
        "alpha": 0.05,
        "both_failure": 0,
        "both_success": 6,
        "control_successes": 6,
        "control_wins": 0,
        "exact_p_value": 0.0001220703125,
        "paired_unit_count": 20,
        "preregistration_sha256": (
            "9cd32c08a32790e4937fdbb49c32fc0f16efd0504c62a2bf180346397400da18"
        ),
        "primary_outcome": "join_correct_task_completion",
        "schema_version": 1,
        "significant_improvement": True,
        "stage_id": "semantic_join_contract_ambiguity_v1",
        "statistical_test": "exact_two_sided_mcnemar",
        "treatment_final_sql_validated": 20,
        "treatment_mcp_grounded": 20,
        "treatment_plan_called": 20,
        "treatment_protocol_compliant": 20,
        "treatment_successes": 20,
        "treatment_wins": 14,
    }

    control_failures = Counter(
        row["failure_code"]
        for row in rows
        if row["condition"] == "control" and not row["join_correct_task_completion"]
    )
    assert control_failures == {
        "SQL_PARSE_FAILED": 7,
        "WRONG_PLAN": 2,
        "MODEL_TIMEOUT": 3,
        "MODEL_LIMIT": 2,
    }

    pairs: dict[tuple[object, ...], dict[str, dict[str, object]]] = defaultdict(dict)
    for row in rows:
        pair_key = (row["host"], row["database_id"], row["task_id"], row["repetition"])
        pairs[pair_key][row["condition"]] = row
    resource_censored = {"MODEL_TIMEOUT", "MODEL_LIMIT"}
    sensitivity_treatment_wins = sum(
        pair["treatment"]["join_correct_task_completion"]
        and not (
            pair["control"]["join_correct_task_completion"]
            or pair["control"]["failure_code"] in resource_censored
        )
        for pair in pairs.values()
    )
    sensitivity_control_wins = sum(
        (
            pair["control"]["join_correct_task_completion"]
            or pair["control"]["failure_code"] in resource_censored
        )
        and not pair["treatment"]["join_correct_task_completion"]
        for pair in pairs.values()
    )
    assert sensitivity_treatment_wins == 9
    assert sensitivity_control_wins == 0
    assert exact_two_sided_mcnemar_p_value(
        treatment_wins=sensitivity_treatment_wins,
        control_wins=sensitivity_control_wins,
    ) == 0.00390625

    assert Decimal(str(calibration["actual_model_cost_cny"])) + Decimal(
        str(budget["actual_model_cost_cny"])
    ) == Decimal("0.45543432")
    assert Decimal(str(result_lock["calibration"]["settled_cost_cny"])) + Decimal(
        str(result_lock["formal_run"]["settled_cost_cny"])
    ) == Decimal("6.612940")
    assert Decimal(str(result_lock["campaign_ledger"]["final_cumulative_cny"])) + Decimal(
        str(result_lock["campaign_ledger"]["remaining_cny"])
    ) == Decimal("82.000000")


def test_public_validator_rejects_rows_relabelled_away_from_the_run_plan(
    tmp_path: Path,
) -> None:
    stage_root = tmp_path / "pilot-stage"
    shutil.copytree(EVIDENCE_ROOT / "pilot-stage", stage_root)
    bundle = _load_json(stage_root / "agent-results-bundle.json")
    rows = _load_json(stage_root / "pilot-agent-results.json")
    assert isinstance(bundle, dict)
    assert isinstance(rows, list)

    first = rows[0]
    pair_key = (first["host"], first["database_id"], first["task_id"], first["repetition"])
    for row in rows:
        row_key = (row["host"], row["database_id"], row["task_id"], row["repetition"])
        if row_key == pair_key:
            row["task_id"] = "tampered-task"
    bundle["rows"] = rows
    (stage_root / "agent-results-bundle.json").write_bytes(canonical_json(bundle))
    (stage_root / "pilot-agent-results.json").write_bytes(canonical_json(rows) + b"\n")

    with pytest.raises(ValueError, match="identities do not match"):
        validate_public_artifacts(stage_root, "pilot-stage")
