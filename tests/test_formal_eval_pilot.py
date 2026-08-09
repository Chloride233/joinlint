from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from benchmarks.formal_eval import pilot_calibration, pilot_canary
from benchmarks.formal_eval.contracts import (
    AgentResultBundle,
    FormalManifestV2,
    FormalTask,
    SealedAgentTask,
)
from benchmarks.formal_eval.lineage import digest_value
from benchmarks.formal_eval.manifest import verify_input_lock
from benchmarks.formal_eval.pilot import (
    PILOT_DATASET_RELEASE,
    PILOT_ALLOCATION,
    PILOT_GROUND_TRUTH_EXCLUSIONS,
    _input_lock,
    budget_envelope,
    build_pilot_calibration_spec,
    build_pilot_run_plan,
    frozen_pilot_registration,
    model_batch_upper_costs,
    pilot_budget_checkpoint,
    pilot_budget_report,
)
from benchmarks.formal_eval.pilot_calibration import (
    CALIBRATION_BUDGET_CNY,
    CALIBRATION_TOKEN_ACCOUNTING_CEILINGS,
    CALIBRATION_TOKEN_LIMITS,
    CalibrationFailureReport,
    CalibrationResourceContract,
    InfrastructureAttestation,
    PilotCalibrationReport,
    attest_calibration_samples,
    build_calibration_commands,
    calibration_authorization_status,
    calibration_budget_envelope,
    calibration_readiness_status,
    submission_guard_pipeline_available,
    usage_breakdown,
    verify_calibration_budget_values,
)
from benchmarks.formal_eval.pilot_dispatch import (
    build_pilot_commands,
    model_usage_cost_cny,
    pilot_campaign_budget,
    require_sample_batch_health,
)
from benchmarks.formal_eval.pilot_canary import (
    CANARY_BUDGET_CNY,
    CANARY_HOST,
    CANARY_SANDBOX_TIMEOUT_SECONDS,
    CANARY_TOKEN_LIMIT,
    CANARY_TOKEN_LIMIT_TYPE,
    PilotCanaryReport,
    build_canary_command,
    canary_budget_envelope,
    require_canary_artifacts,
    verify_canary_attestation_values,
)


COMMIT = "a" * 40
LINEAGE_ID = "b" * 64


def test_pilot_v4_freezes_diagnostic_ground_truth_exclusions() -> None:
    assert PILOT_DATASET_RELEASE.endswith("pilot-v4")
    assert PILOT_ALLOCATION == {"citeseer": 9, "trains": 11}
    assert PILOT_GROUND_TRUTH_EXCLUSIONS == {
        "bird-train-citeseer-04142": (
            "question_requests_other_paper_but_gold_returns_source_paper_words"
        ),
        "bird-train-citeseer-04143": (
            "question_ambiguously_invokes_cites_table_but_gold_uses_content_only"
        ),
        "bird-train-citeseer-04150": (
            "question_requires_class_intersection_but_gold_uses_union"
        ),
        "bird-train-trains-00698": "question_requests_west_but_gold_filters_east",
    }


def test_pilot_budget_envelope_is_below_the_approved_hard_limit() -> None:
    registration = frozen_pilot_registration(COMMIT)

    envelope = budget_envelope(registration)

    assert envelope.run_count == 80
    assert registration.schema_version == 5
    assert registration.token_limit_per_run == 35_000
    assert registration.token_accounting_ceiling_per_run == 45_000
    assert envelope.model_cost_upper_cny == pytest.approx(14.4)
    assert envelope.modal_compute_upper_cny == pytest.approx(3.59584)
    assert envelope.modal_image_build_reserve_cny == 2.0
    assert envelope.total_upper_cny == pytest.approx(19.99584)
    assert envelope.total_upper_cny < registration.budget_cny == 20.0
    assert registration.modal_image_builder_version == "2025.06 Stable"
    assert {model.family for model in registration.models} == {"deepseek-v4"}
    assert {model.tier for model in registration.models} == {
        "high_capability",
        "cost_efficient",
    }
    assert model_batch_upper_costs(registration) == pytest.approx(
        (2.7, 2.7, 2.7, 2.7, 0.9, 0.9, 0.9, 0.9)
    )


def test_pilot_registration_keeps_the_previous_sandbox_contract_parseable() -> None:
    current = frozen_pilot_registration(COMMIT)
    legacy_payload = current.model_dump(mode="python")
    legacy_payload.update(schema_version=4, modal_sandbox_timeout_seconds=150)

    legacy = type(current).model_validate(legacy_payload)

    assert legacy.schema_version == 4
    assert legacy.modal_sandbox_timeout_seconds == 150
    with pytest.raises(ValueError, match="sandbox timeout"):
        type(current).model_validate(
            {**legacy_payload, "modal_sandbox_timeout_seconds": 170}
        )


@pytest.mark.parametrize("schema_version", [3, 4])
def test_legacy_pilot_registration_defaults_omitted_sandbox_timeout_to_150(
    schema_version: int,
) -> None:
    current = frozen_pilot_registration(COMMIT)
    legacy_payload = current.model_dump(mode="python")
    legacy_payload["schema_version"] = schema_version
    legacy_payload.pop("modal_sandbox_timeout_seconds")
    if schema_version == 3:
        legacy_payload["token_accounting_ceiling_per_run"] = None

    legacy = type(current).model_validate(legacy_payload)

    assert legacy.schema_version == schema_version
    assert legacy.modal_sandbox_timeout_seconds == 150


def test_current_pilot_registration_requires_explicit_schema_and_timeout() -> None:
    current = frozen_pilot_registration(COMMIT)
    current_payload = current.model_dump(mode="python")

    assert {"schema_version", "modal_sandbox_timeout_seconds"} <= current.model_fields_set
    with pytest.raises(ValueError, match="schema_version"):
        type(current).model_validate(
            {key: value for key, value in current_payload.items() if key != "schema_version"}
        )
    with pytest.raises(ValueError, match="explicitly declare.*sandbox timeout"):
        type(current).model_validate(
            {
                key: value
                for key, value in current_payload.items()
                if key != "modal_sandbox_timeout_seconds"
            }
        )


def test_pilot_canary_is_one_bounded_treatment_run(tmp_path: Path) -> None:
    registration = frozen_pilot_registration(COMMIT)

    envelope = canary_budget_envelope(registration)
    command = build_canary_command(
        inspect="/usr/bin/inspect",
        registration=registration,
        root=tmp_path / "sealed",
        log_dir=tmp_path / "logs",
        lineage_id=LINEAGE_ID,
    )

    assert CANARY_BUDGET_CNY == 2.25
    assert CANARY_SANDBOX_TIMEOUT_SECONDS == 170
    assert CANARY_TOKEN_LIMIT == 60_000
    assert CANARY_TOKEN_LIMIT_TYPE == "(input*0.5)+output"
    assert envelope.run_count == 1
    assert envelope.model_cost_upper_cny == pytest.approx(0.12)
    assert envelope.modal_compute_upper_cny == pytest.approx(0.044948)
    assert envelope.modal_image_build_reserve_cny == 2.0
    assert envelope.total_upper_cny == pytest.approx(2.164948)
    assert envelope.total_upper_cny < CANARY_BUDGET_CNY
    assert command[command.index("--limit") + 1] == "1"
    assert CANARY_HOST == "claude_code"
    assert "host=claude_code" in command
    assert "condition=treatment" in command
    assert "token_limit=60000" in command
    assert "token_limit=20000" not in command
    assert "token_limit_type=(input*0.5)+output" in command
    assert "sandbox_timeout=170" in command
    assert "sandbox_timeout=120" not in command
    assert registration.models[1].id in command


def test_pilot_canary_rejects_exhausted_campaign_before_inspect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration = frozen_pilot_registration(COMMIT)
    inspect_calls = 0
    monkeypatch.setattr(
        pilot_canary,
        "verify_pilot_inputs",
        lambda root: (
            registration,
            SimpleNamespace(),
            SimpleNamespace(lineage_id=LINEAGE_ID),
        ),
    )
    monkeypatch.setattr(pilot_canary.shutil, "which", lambda name: "/inspect")

    def unexpected_inspect(*args: object, **kwargs: object) -> None:
        nonlocal inspect_calls
        inspect_calls += 1

    monkeypatch.setattr(pilot_canary.subprocess, "run", unexpected_inspect)

    with pytest.raises(ValueError, match="cumulative campaign budget"):
        pilot_canary.run_canary(
            tmp_path / "sealed",
            tmp_path / "logs",
            tmp_path / "artifacts",
            workflow_run_id=123,
            campaign_budget_cny=10,
            campaign_spend_before_cny=8,
        )

    assert inspect_calls == 0


def test_pilot_calibration_freezes_highest_depth_tasks_from_distinct_databases() -> None:
    manifest = _manifest(4)
    depths = (1, 4, 2, 4)
    databases = ("db-a", "db-a", "db-b", "db-b")
    manifest = manifest.model_copy(
        update={
            "tasks": tuple(
                task.model_copy(update={"join_depth": depth, "database_id": database})
                for task, depth, database in zip(
                    manifest.tasks, depths, databases, strict=True
                )
            )
        }
    )

    specification = build_pilot_calibration_spec(manifest)

    assert specification.task_ids == ("task-1", "task-3")


def test_pilot_calibration_uses_exact_formal_resource_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import benchmarks.formal_eval.pilot_calibration as calibration

    registration = frozen_pilot_registration(COMMIT)
    manifest = _manifest(20)
    specification = build_pilot_calibration_spec(manifest)
    run_plan = SimpleNamespace(lineage_id=LINEAGE_ID)
    monkeypatch.setattr(
        calibration,
        "verify_pilot_inputs",
        lambda root: (registration, manifest, run_plan),
    )
    monkeypatch.setattr(
        calibration,
        "load_pilot_calibration_spec",
        lambda root, current_manifest: specification,
    )

    commands = build_calibration_commands(
        inspect="/usr/bin/inspect",
        registration=registration,
        root=tmp_path / "sealed",
        log_dir=tmp_path / "logs",
        lineage_id=LINEAGE_ID,
    )
    envelope = calibration_budget_envelope(registration)

    assert CALIBRATION_BUDGET_CNY == 4.0
    assert CALIBRATION_TOKEN_LIMITS == {
        host: registration.token_limit_per_run for host in registration.hosts
    }
    assert CALIBRATION_TOKEN_ACCOUNTING_CEILINGS == {
        host: registration.token_accounting_ceiling_per_run
        for host in registration.hosts
    }
    assert envelope.run_count == 8
    assert envelope.model_cost_upper_cny == pytest.approx(1.44)
    assert envelope.modal_compute_upper_cny == pytest.approx(0.359584)
    assert envelope.total_upper_cny == pytest.approx(3.799584)
    assert len(commands) == 4
    assert all("condition=treatment" in command for command in commands)
    for command in commands:
        host = next(
            value.removeprefix("host=") for value in command if value.startswith("host=")
        )
        assert f"token_limit={CALIBRATION_TOKEN_LIMITS[host]}" in command
    assert all("message_limit=20" in command for command in commands)
    assert all("time_limit=90" in command for command in commands)
    assert all("sandbox_timeout=170" in command for command in commands)
    assert all(not any(value.startswith("task_partition=") for value in command) for command in commands)
    expected_task_ids = f"task_ids={','.join(specification.task_ids)}"
    assert all(expected_task_ids in command for command in commands)


def test_pilot_calibration_separates_resource_readiness_from_product_outcome() -> None:
    from benchmarks.formal_eval.lifecycle import (
        allow_scoring,
        complete_evaluation,
        fail_evaluation,
        infrastructure_prepared,
        LifecycleFailureReason,
        new_lifecycle,
        readiness_passed,
        start_evaluation,
    )

    registration = frozen_pilot_registration(COMMIT)
    task_ids = ("task-a", "task-b")
    lifecycle = new_lifecycle("codex", "0.144.1")
    lifecycle = infrastructure_prepared(
        lifecycle,
        duration_seconds=1,
        host_binary_sha256="c" * 64,
    )
    lifecycle = readiness_passed(lifecycle, duration_seconds=1)
    lifecycle = start_evaluation(lifecycle)
    lifecycle = complete_evaluation(lifecycle, duration_seconds=1)
    lifecycle = allow_scoring(lifecycle)
    trace = {
        "plan_called": True,
        "plan_usable": True,
        "complete_entity_planning": True,
        "final_sql_validated": True,
        "validation_passed": True,
        "mcp_grounded": True,
        "protocol_compliant": True,
        "protocol_violation": None,
        "tool_error": False,
    }
    samples = []
    for model in registration.models:
        for host in registration.hosts:
            for task_id in task_ids:
                host_lifecycle = lifecycle.model_copy(
                    update={"host": host, "agent_version": registration.host_versions[host]}
                )
                samples.append(
                    SimpleNamespace(
                        metadata={"host": host, "task_id": task_id},
                        model_usage={
                            model.returned_id: SimpleNamespace(
                                input_tokens=984,
                                input_tokens_cache_read=43_008,
                                input_tokens_cache_write=None,
                                output_tokens=482,
                            )
                        },
                        store={
                            "joinlint.formal_eval.lifecycle.v1": host_lifecycle.model_dump(
                                mode="json"
                            )
                        },
                        scores={
                            "formal_join_scorer": SimpleNamespace(
                                metadata={
                                    "failure_code": None,
                                    "trace": trace,
                                    "submission_guard_contract_version": 1,
                                    "submission_guard_decision": (
                                        "accepted_validated_sql"
                                    ),
                                }
                            ),
                            "formal_execution_scorer": SimpleNamespace(
                                metadata={"error_code": None}
                            ),
                        },
                    )
                )

    infrastructure, resource, harness, scoring = (
        attest_calibration_samples(
            samples,
            registration=registration,
            task_ids=task_ids,
        )
    )

    assert infrastructure.status == "passed"
    assert resource.status == "passed"
    assert harness.status == "passed"
    assert scoring.status == "passed"
    assert len(infrastructure.cells) == len(resource.cells) == 8
    assert len(harness.cells) == len(scoring.cells) == 8
    assert all(cell.protocol_compliant is True for cell in harness.cells)
    assert all(cell.protocol_violation is None for cell in harness.cells)
    assert submission_guard_pipeline_available(harness) is True

    tool_error_payload = harness.model_dump(mode="python")
    tool_error_payload["status"] = "failed"
    tool_error_payload["cells"][0]["tool_error"] = True
    tool_error_payload["cells"][0][
        "submission_guard_decision"
    ] = "accepted_abstention"
    tool_error_harness = type(harness).model_validate(tool_error_payload)
    assert submission_guard_pipeline_available(tool_error_harness) is False
    assert calibration_authorization_status(
        infrastructure=infrastructure,
        resource=resource,
        scoring=scoring,
        submission_guard_available=submission_guard_pipeline_available(
            tool_error_harness
        ),
        within_budget=True,
    ) == "failed"

    assert {summary.observed_cache_read_floor_tokens for summary in resource.hosts} == {
        43_008
    }
    assert {summary.target_headroom_tokens for summary in resource.hosts} == {5_000}
    assert calibration_readiness_status(
        infrastructure=infrastructure,
        resource=resource,
        scoring=scoring,
        within_budget=True,
    ) == "passed"

    actual_model_cost = sum(
        cell.usage.calculated_cost_cny
        for cell in resource.cells
        if cell.usage is not None
    )
    envelope = calibration_budget_envelope(registration)
    total_cost_upper = (
        actual_model_cost
        + envelope.modal_compute_upper_cny
        + envelope.modal_image_build_reserve_cny
    )
    current_report = PilotCalibrationReport(
        schema_version=6,
        status="passed",
        readiness_status="passed",
        calibration_task_ids=task_ids,
        resource_contract=CalibrationResourceContract(
            token_limit_by_host=CALIBRATION_TOKEN_LIMITS,
            token_accounting_ceiling_by_host=CALIBRATION_TOKEN_ACCOUNTING_CEILINGS,
        ),
        infrastructure=infrastructure,
        resource=resource,
        harness=harness,
        scoring=scoring,
        actual_model_cost_cny=actual_model_cost,
        modal_compute_upper_cny=envelope.modal_compute_upper_cny,
        modal_image_build_reserve_cny=envelope.modal_image_build_reserve_cny,
        total_cost_upper_cny=total_cost_upper,
        campaign_spend_before_cny=8.0,
        campaign_budget_cny=20.0,
        campaign_total_upper_cny=8.0 + total_cost_upper,
        workflow_run_id=123,
        joinlint_commit=COMMIT,
        input_lock_sha256="c" * 64,
        dependency_versions={},
    )
    verify_calibration_budget_values(current_report, registration)

    tampered = current_report.model_dump()
    tampered["actual_model_cost_cny"] += 0.01
    tampered["total_cost_upper_cny"] += 0.01
    tampered["campaign_total_upper_cny"] += 0.01
    with pytest.raises(ValueError, match="actual model cost"):
        PilotCalibrationReport.model_validate(tampered)

    samples[0].scores["formal_join_scorer"].metadata[
        "submission_guard_decision"
    ] = "rejected_unvalidated_sql"
    _, _, rejected_harness, _ = attest_calibration_samples(
        samples,
        registration=registration,
        task_ids=task_ids,
    )
    assert submission_guard_pipeline_available(rejected_harness) is True
    samples[0].scores["formal_join_scorer"].metadata[
        "submission_guard_decision"
    ] = "not_observed"
    _, _, missing_guard, _ = attest_calibration_samples(
        samples,
        registration=registration,
        task_ids=task_ids,
    )
    assert submission_guard_pipeline_available(missing_guard) is False
    samples[0].scores["formal_join_scorer"].metadata[
        "submission_guard_decision"
    ] = "accepted_validated_sql"

    original_lifecycle = samples[0].store["joinlint.formal_eval.lifecycle.v1"]
    sample_host = samples[0].metadata["host"]
    ledger_failed = new_lifecycle(
        sample_host,
        registration.host_versions[sample_host],
    )
    ledger_failed = infrastructure_prepared(
        ledger_failed,
        duration_seconds=1,
        host_binary_sha256="c" * 64,
    )
    ledger_failed = readiness_passed(ledger_failed, duration_seconds=1)
    ledger_failed = start_evaluation(ledger_failed)
    ledger_failed = fail_evaluation(
        ledger_failed,
        reason=LifecycleFailureReason.VALIDATION_LEDGER_WRITE_FAILED,
        duration_seconds=1,
    )
    samples[0].store[
        "joinlint.formal_eval.lifecycle.v1"
    ] = ledger_failed.model_dump(mode="json")
    failed_infrastructure, still_accounted, _, still_emitted = attest_calibration_samples(
        samples,
        registration=registration,
        task_ids=task_ids,
    )
    assert failed_infrastructure.status == "failed"
    assert calibration_authorization_status(
        infrastructure=failed_infrastructure,
        resource=still_accounted,
        scoring=still_emitted,
        within_budget=True,
    ) == "failed"
    with pytest.raises(RuntimeError, match="infrastructure failure"):
        require_sample_batch_health([samples[0]], expected_sample_count=1)
    samples[0].store["joinlint.formal_eval.lifecycle.v1"] = original_lifecycle

    marker_unavailable = new_lifecycle(
        sample_host,
        registration.host_versions[sample_host],
    )
    marker_unavailable = infrastructure_prepared(
        marker_unavailable,
        duration_seconds=1,
        host_binary_sha256="c" * 64,
    )
    marker_unavailable = readiness_passed(marker_unavailable, duration_seconds=1)
    marker_unavailable = start_evaluation(marker_unavailable)
    marker_unavailable = fail_evaluation(
        marker_unavailable,
        reason=LifecycleFailureReason.VALIDATION_FAILURE_MARKER_UNAVAILABLE,
        duration_seconds=1,
    )
    samples[0].store[
        "joinlint.formal_eval.lifecycle.v1"
    ] = marker_unavailable.model_dump(mode="json")
    marker_infrastructure, marker_resource, marker_harness, marker_scoring = (
        attest_calibration_samples(
            samples,
            registration=registration,
            task_ids=task_ids,
        )
    )
    assert marker_infrastructure.status == "failed"
    assert calibration_authorization_status(
        infrastructure=marker_infrastructure,
        resource=marker_resource,
        scoring=marker_scoring,
        submission_guard_available=submission_guard_pipeline_available(marker_harness),
        within_budget=True,
    ) == "failed"
    with pytest.raises(RuntimeError, match="infrastructure failure"):
        require_sample_batch_health([samples[0]], expected_sample_count=1)
    samples[0].store["joinlint.formal_eval.lifecycle.v1"] = original_lifecycle

    report_values = {
        "calibration_task_ids": task_ids,
        "resource_contract": CalibrationResourceContract(
            token_limit_by_host=CALIBRATION_TOKEN_LIMITS
        ),
        "infrastructure": infrastructure,
        "harness": harness,
        "scoring": scoring,
        "actual_model_cost_cny": 0.1,
        "modal_compute_upper_cny": 0.2,
        "modal_image_build_reserve_cny": 2.0,
        "total_cost_upper_cny": 2.3,
        "campaign_spend_before_cny": 8.0,
        "campaign_budget_cny": 20.0,
        "campaign_total_upper_cny": 10.3,
        "workflow_run_id": 123,
        "joinlint_commit": COMMIT,
        "input_lock_sha256": "c" * 64,
        "dependency_versions": {},
    }
    legacy_report = PilotCalibrationReport(
        schema_version=1,
        status="passed",
        **report_values,
    )
    assert legacy_report.readiness_status is None

    trace["plan_called"] = False
    _, still_sufficient, failed_harness, still_available = attest_calibration_samples(
        samples,
        registration=registration,
        task_ids=task_ids,
    )
    assert failed_harness.status == "failed"
    assert still_sufficient.status == "passed"
    assert still_available.status == "passed"
    separated_report = PilotCalibrationReport(
        schema_version=3,
        status="passed",
        readiness_status="passed",
        resource=still_sufficient,
        **{**report_values, "harness": failed_harness},
    )
    assert separated_report.status == "passed"
    assert separated_report.readiness_status == "passed"
    trace["plan_called"] = True

    trace["protocol_compliant"] = False
    _, _, failed_protocol, _ = attest_calibration_samples(
        samples,
        registration=registration,
        task_ids=task_ids,
    )
    assert failed_protocol.status == "failed"
    trace["protocol_compliant"] = True

    samples[0].scores["formal_join_scorer"].metadata["failure_code"] = "SQL_PARSE_FAILED"
    _, _, _, failed_scoring = attest_calibration_samples(
        samples,
        registration=registration,
        task_ids=task_ids,
    )
    assert failed_scoring.status == "failed"
    assert calibration_readiness_status(
        infrastructure=infrastructure,
        resource=resource,
        scoring=failed_scoring,
        within_budget=True,
    ) == "passed"

    limited = new_lifecycle("codex", registration.host_versions["codex"])
    limited = infrastructure_prepared(
        limited,
        duration_seconds=1,
        host_binary_sha256="c" * 64,
    )
    limited = readiness_passed(limited, duration_seconds=1)
    limited = start_evaluation(limited)
    limited = fail_evaluation(
        limited,
        reason=LifecycleFailureReason.MODEL_LIMIT,
        detail="weighted token limit reached",
        duration_seconds=1,
    )
    samples[0].store["joinlint.formal_eval.lifecycle.v1"] = limited.model_dump(mode="json")
    limited_model = next(iter(samples[0].model_usage))
    samples[0].model_usage[limited_model] = SimpleNamespace(
        input_tokens=2_775,
        input_tokens_cache_read=68_352,
        input_tokens_cache_write=None,
        output_tokens=1_354,
    )
    limited_infrastructure, limited_resource, limited_harness, limited_scoring = (
        attest_calibration_samples(
            samples,
            registration=registration,
            task_ids=task_ids,
        )
    )
    assert limited_resource.status == "failed"
    limited_cell = next(cell for cell in limited_resource.cells if cell.model_limit_reached)
    assert limited_cell.resource_sufficient is False
    assert limited_cell.usage is not None
    assert limited_cell.observed_weighted_tokens == 36_917.5
    assert limited_cell.observed_weighted_tokens > limited_cell.configured_token_limit
    assert (
        limited_cell.observed_weighted_tokens
        < CALIBRATION_TOKEN_ACCOUNTING_CEILINGS[limited_cell.host]
    )
    assert next(
        summary for summary in limited_resource.hosts if summary.host == limited_cell.host
    ).limit_censored
    assert limited_scoring.status == "failed"
    assert calibration_authorization_status(
        infrastructure=limited_infrastructure,
        resource=limited_resource,
        scoring=limited_scoring,
        within_budget=True,
    ) == "passed"
    censored_report = PilotCalibrationReport(
        schema_version=4,
        status="passed",
        readiness_status="passed",
        resource=limited_resource,
        **{
            **report_values,
            "resource_contract": CalibrationResourceContract(
                token_limit_by_host=CALIBRATION_TOKEN_LIMITS,
                token_accounting_ceiling_by_host=CALIBRATION_TOKEN_ACCOUNTING_CEILINGS,
            ),
            "infrastructure": limited_infrastructure,
            "harness": limited_harness,
            "scoring": limited_scoring,
        },
    )
    assert censored_report.status == "passed"

    with pytest.raises(ValueError, match="status is inconsistent"):
        InfrastructureAttestation(status="passed", cells=())


def test_calibration_batch_failure_preserves_count_when_cost_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration = frozen_pilot_registration(COMMIT)
    log_dir = tmp_path / "logs"
    output = tmp_path / "artifacts"
    monkeypatch.setattr(
        pilot_calibration,
        "verify_pilot_inputs",
        lambda root: (
            registration,
            SimpleNamespace(),
            SimpleNamespace(lineage_id=LINEAGE_ID),
        ),
    )
    monkeypatch.setattr(
        pilot_calibration,
        "load_pilot_calibration_spec",
        lambda root, manifest: SimpleNamespace(task_ids=("task-a", "task-b")),
    )
    monkeypatch.setattr(pilot_calibration.shutil, "which", lambda name: "/inspect")
    monkeypatch.setattr(
        pilot_calibration,
        "build_calibration_commands",
        lambda **kwargs: (["/inspect", "--log-dir", str(log_dir / "batch")],),
    )
    monkeypatch.setattr(pilot_calibration.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        pilot_calibration,
        "require_batch_health",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("pilot batch contains an infrastructure failure")
        ),
    )
    monkeypatch.setattr(
        pilot_calibration,
        "observed_model_cost_cny",
        lambda *args: (_ for _ in ()).throw(RuntimeError("usage log unavailable")),
    )
    monkeypatch.setattr(pilot_calibration, "_observed_sample_count", lambda path: 6)
    monkeypatch.setattr(pilot_calibration, "_input_lock_sha256", lambda root: "c" * 64)

    with pytest.raises(RuntimeError, match="infrastructure failure"):
        pilot_calibration.run_calibration(
            tmp_path / "sealed",
            log_dir,
            output,
            workflow_run_id=123,
            campaign_budget_cny=75,
            campaign_spend_before_cny=64,
        )

    report = CalibrationFailureReport.model_validate_json(
        (output / "calibration-failure.json").read_bytes()
    )
    assert report.failure_code == "INFRASTRUCTURE_FAILURE"
    assert report.observed_sample_count == 6
    assert report.actual_model_cost_cny == 0
    assert report.joinlint_commit == COMMIT
    assert report.input_lock_sha256 == "c" * 64
    assert "SELECT" not in (output / "calibration-failure.json").read_text()


def test_calibration_rejects_a_nonempty_output_directory(tmp_path: Path) -> None:
    output = tmp_path / "artifacts"
    output.mkdir()
    stale_success = output / "calibration.json"
    stale_success.write_text('{"status":"passed"}', encoding="utf-8")

    with pytest.raises(ValueError, match="output directory must be empty"):
        pilot_calibration.run_calibration(
            tmp_path / "sealed",
            tmp_path / "logs",
            output,
            workflow_run_id=123,
            campaign_budget_cny=75,
            campaign_spend_before_cny=64,
        )

    assert stale_success.read_text(encoding="utf-8") == '{"status":"passed"}'
    assert not (output / "calibration-failure.json").exists()


def test_calibration_archive_write_failure_does_not_mask_the_run_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration = frozen_pilot_registration(COMMIT)
    log_dir = tmp_path / "logs"
    output = tmp_path / "artifacts"
    monkeypatch.setattr(
        pilot_calibration,
        "verify_pilot_inputs",
        lambda root: (
            registration,
            SimpleNamespace(),
            SimpleNamespace(lineage_id=LINEAGE_ID),
        ),
    )
    monkeypatch.setattr(
        pilot_calibration,
        "load_pilot_calibration_spec",
        lambda root, manifest: SimpleNamespace(task_ids=("task-a", "task-b")),
    )
    monkeypatch.setattr(pilot_calibration.shutil, "which", lambda name: "/inspect")
    monkeypatch.setattr(
        pilot_calibration,
        "build_calibration_commands",
        lambda **kwargs: (["/inspect", "--log-dir", str(log_dir / "batch")],),
    )
    monkeypatch.setattr(pilot_calibration.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        pilot_calibration,
        "require_batch_health",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("original batch failure")
        ),
    )
    monkeypatch.setattr(pilot_calibration, "observed_model_cost_cny", lambda *args: 0)
    monkeypatch.setattr(pilot_calibration, "_observed_sample_count", lambda path: 0)
    monkeypatch.setattr(pilot_calibration, "_input_lock_sha256", lambda root: "c" * 64)
    original_write = pilot_calibration._write_calibration_failure
    writes = 0

    def fail_final_write(path: Path, report: CalibrationFailureReport) -> None:
        nonlocal writes
        writes += 1
        if writes == 3:
            raise OSError("failure archive unavailable")
        original_write(path, report)

    monkeypatch.setattr(pilot_calibration, "_write_calibration_failure", fail_final_write)

    with pytest.raises(RuntimeError, match="original batch failure"):
        pilot_calibration.run_calibration(
            tmp_path / "sealed",
            log_dir,
            output,
            workflow_run_id=123,
            campaign_budget_cny=75,
            campaign_spend_before_cny=64,
        )

    assert writes == 3


def test_calibration_post_run_failure_updates_the_sanitized_failure_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration = frozen_pilot_registration(COMMIT)
    log_dir = tmp_path / "logs"
    output = tmp_path / "artifacts"
    monkeypatch.setattr(
        pilot_calibration,
        "verify_pilot_inputs",
        lambda root: (
            registration,
            SimpleNamespace(),
            SimpleNamespace(lineage_id=LINEAGE_ID),
        ),
    )
    monkeypatch.setattr(
        pilot_calibration,
        "load_pilot_calibration_spec",
        lambda root, manifest: SimpleNamespace(task_ids=("task-a", "task-b")),
    )
    monkeypatch.setattr(pilot_calibration.shutil, "which", lambda name: "/inspect")
    monkeypatch.setattr(
        pilot_calibration,
        "build_calibration_commands",
        lambda **kwargs: (["/inspect", "--log-dir", str(log_dir / "batch")],),
    )
    monkeypatch.setattr(
        pilot_calibration.subprocess,
        "run",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        pilot_calibration,
        "require_batch_health",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(pilot_calibration, "observed_model_cost_cny", lambda *args: 0.12)
    monkeypatch.setattr(pilot_calibration, "_observed_sample_count", lambda path: 8)
    monkeypatch.setattr(pilot_calibration, "_input_lock_sha256", lambda root: "c" * 64)
    monkeypatch.setattr(
        pilot_calibration,
        "verify_calibration_logs",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("sensitive post-run detail: SELECT secret_value")
        ),
    )

    with pytest.raises(RuntimeError, match="sensitive post-run detail"):
        pilot_calibration.run_calibration(
            tmp_path / "sealed",
            log_dir,
            output,
            workflow_run_id=123,
            campaign_budget_cny=75,
            campaign_spend_before_cny=64,
        )

    failure_path = output / "calibration-failure.json"
    report = CalibrationFailureReport.model_validate_json(failure_path.read_bytes())
    assert report.failure_code == "CALIBRATION_FAILED"
    assert report.observed_sample_count == 8
    assert report.actual_model_cost_cny == pytest.approx(0.12)
    assert "sensitive post-run detail" not in failure_path.read_text()
    assert "SELECT" not in failure_path.read_text()


def test_pilot_canary_requires_model_usage_and_both_scorers() -> None:
    expected_model = "openai-api/deepseek/deepseek-v4-pro"
    sample = SimpleNamespace(
        error=None,
        scores={
            "formal_join_scorer": SimpleNamespace(metadata={"scoring_eligible": True}),
            "formal_execution_scorer": SimpleNamespace(metadata={"scoring_eligible": True}),
        },
        model_usage={expected_model: object()},
    )

    model_id, scorers = require_canary_artifacts(
        log_model_id=expected_model,
        samples=[sample],
        expected_model_id=expected_model,
    )

    assert model_id == expected_model
    assert scorers == ("formal_execution_scorer", "formal_join_scorer")

    with pytest.raises(RuntimeError, match="provider model identity"):
        require_canary_artifacts(
            log_model_id=expected_model,
            samples=[SimpleNamespace(**{**vars(sample), "model_usage": {}})],
            expected_model_id=expected_model,
        )

    ineligible = SimpleNamespace(
        **{
            **vars(sample),
            "scores": {
                "formal_join_scorer": SimpleNamespace(
                    metadata={"scoring_eligible": False}
                ),
                "formal_execution_scorer": SimpleNamespace(metadata={}),
            },
        }
    )
    with pytest.raises(RuntimeError, match="lifecycle was not scoring eligible"):
        require_canary_artifacts(
            log_model_id=expected_model,
            samples=[ineligible],
            expected_model_id=expected_model,
        )


def test_observed_cost_treats_cache_read_as_additional_input_usage(
) -> None:
    registration = frozen_pilot_registration(COMMIT)
    usage = SimpleNamespace(
        input_tokens=984,
        input_tokens_cache_read=43_008,
        input_tokens_cache_write=None,
        output_tokens=482,
    )
    cost = model_usage_cost_cny(usage, registration.models[0].pricing_cny)
    breakdown = usage_breakdown(usage, registration.models[0].pricing_cny)

    assert cost == pytest.approx(0.0069192)
    assert breakdown.uncached_input_tokens == 984
    assert breakdown.cache_read_input_tokens == 43_008
    assert breakdown.cache_write_input_tokens == 0
    assert breakdown.context_input_tokens == 43_992
    assert breakdown.inspect_weighted_tokens == 22_478
    assert breakdown.calculated_cost_cny == pytest.approx(cost)
    assert breakdown.cache_read_ratio == pytest.approx(43_008 / 43_992)


def test_pilot_canary_attestation_binds_run_commit_input_and_dependencies() -> None:
    dependencies = {
        "anthropic": "0.120.2",
        "inspect-ai": "0.3.249",
        "inspect-sandboxes": "0.4.0",
        "inspect-swe": "0.2.66",
        "modal": "1.5.3",
    }
    report = PilotCanaryReport(
        model_id="openai-api/deepseek/deepseek-v4-pro",
        scorer_artifacts=("formal_execution_scorer", "formal_join_scorer"),
        actual_model_cost_cny=0.01,
        modal_compute_upper_cny=0.031728,
        modal_image_build_reserve_cny=2.0,
        total_cost_upper_cny=2.041728,
        workflow_run_id=123,
        joinlint_commit=COMMIT,
        input_lock_sha256="c" * 64,
        dependency_versions=dependencies,
    )
    run_metadata = {
        "id": 123,
        "run_attempt": 1,
        "name": "formal-pilot-canary",
        "path": ".github/workflows/formal-pilot-canary.yml",
        "event": "workflow_dispatch",
        "conclusion": "success",
        "head_sha": COMMIT,
        "head_repository": {"full_name": "Chloride233/joinlint"},
    }

    verify_canary_attestation_values(
        report,
        expected_run_id=123,
        current_commit=COMMIT,
        input_lock_sha256="c" * 64,
        dependency_versions=dependencies,
        run_metadata=run_metadata,
        repository="Chloride233/joinlint",
    )

    with pytest.raises(ValueError, match="commit"):
        verify_canary_attestation_values(
            report,
            expected_run_id=123,
            current_commit="d" * 40,
            input_lock_sha256="c" * 64,
            dependency_versions=dependencies,
            run_metadata=run_metadata,
            repository="Chloride233/joinlint",
        )

    for invalid_attempt in (None, 2, "1", True):
        invalid_metadata = {**run_metadata, "run_attempt": invalid_attempt}
        with pytest.raises(ValueError, match="run attempt"):
            verify_canary_attestation_values(
                report,
                expected_run_id=123,
                current_commit=COMMIT,
                input_lock_sha256="c" * 64,
                dependency_versions=dependencies,
                run_metadata=invalid_metadata,
                repository="Chloride233/joinlint",
            )


def test_pilot_calibration_attestation_rejects_rerun_attempts() -> None:
    report = SimpleNamespace(
        status="passed",
        readiness_status="passed",
        workflow_run_id=123,
        joinlint_commit=COMMIT,
        input_lock_sha256="c" * 64,
        dependency_versions={},
    )
    run_metadata = {
        "id": 123,
        "run_attempt": 1,
        "name": "formal-pilot-canary",
        "path": ".github/workflows/formal-pilot-canary.yml",
        "event": "workflow_dispatch",
        "conclusion": "success",
        "head_sha": COMMIT,
        "head_repository": {"full_name": "Chloride233/joinlint"},
    }

    pilot_calibration.verify_calibration_attestation_values(
        report,
        expected_run_id=123,
        current_commit=COMMIT,
        input_lock_sha256="c" * 64,
        dependency_versions={},
        run_metadata=run_metadata,
        repository="Chloride233/joinlint",
    )

    for invalid_attempt in (None, 2, "1", True):
        invalid_metadata = {**run_metadata, "run_attempt": invalid_attempt}
        with pytest.raises(ValueError, match="run attempt"):
            pilot_calibration.verify_calibration_attestation_values(
                report,
                expected_run_id=123,
                current_commit=COMMIT,
                input_lock_sha256="c" * 64,
                dependency_versions={},
                run_metadata=invalid_metadata,
                repository="Chloride233/joinlint",
            )


def test_pilot_budget_checkpoint_stops_before_an_unsafe_next_batch() -> None:
    registration = frozen_pilot_registration(COMMIT)

    safe = pilot_budget_checkpoint(
        registration,
        completed_batches=1,
        actual_model_cost_cny=2.1,
    )
    unsafe = pilot_budget_checkpoint(
        registration,
        completed_batches=1,
        actual_model_cost_cny=6.0,
    )

    assert safe.safe_to_continue is True
    assert safe.projected_total_upper_cny == pytest.approx(19.39584)
    assert unsafe.safe_to_continue is False
    assert unsafe.projected_total_upper_cny == pytest.approx(23.29584)


def test_pilot_campaign_budget_includes_prior_investigation_spend() -> None:
    safe = pilot_campaign_budget(
        campaign_budget_cny=30,
        campaign_spend_before_cny=8,
        pilot_cost_upper_cny=20,
    )
    unsafe = pilot_campaign_budget(
        campaign_budget_cny=27,
        campaign_spend_before_cny=8,
        pilot_cost_upper_cny=20,
    )

    assert safe.campaign_total_upper_cny == 28
    assert safe.passed is True
    assert unsafe.passed is False


@pytest.mark.parametrize("campaign_budget_cny", [float("inf"), float("nan")])
def test_pilot_campaign_budget_rejects_non_finite_ceiling(
    campaign_budget_cny: float,
) -> None:
    with pytest.raises(ValueError):
        pilot_campaign_budget(
            campaign_budget_cny=campaign_budget_cny,
            campaign_spend_before_cny=8,
            pilot_cost_upper_cny=20,
        )


def test_pilot_campaign_preflight_uses_frozen_cost_envelope() -> None:
    registration = frozen_pilot_registration(COMMIT)
    frozen_upper = budget_envelope(registration).total_upper_cny

    report = pilot_campaign_budget(
        campaign_budget_cny=40,
        campaign_spend_before_cny=10.56,
        pilot_cost_upper_cny=frozen_upper,
    )

    assert frozen_upper == pytest.approx(19.99584)
    assert frozen_upper < registration.budget_cny
    assert report.campaign_total_upper_cny == pytest.approx(30.55584)
    assert report.passed is True


def test_pilot_run_plan_is_twenty_tasks_and_80_balanced_crossover_runs() -> None:
    registration = frozen_pilot_registration(COMMIT)
    plan = build_pilot_run_plan(_manifest(20), registration, LINEAGE_ID)

    assert len({run.task_id for run in plan.runs}) == 20
    assert len(plan.runs) == 80
    assert {run.repetition for run in plan.runs} == {0}
    assert {run.condition for run in plan.runs} == {"control", "treatment"}
    assert {run.host for run in plan.runs} == {"codex", "claude_code"}
    assert {run.model_id for run in plan.runs} == {
        model.returned_id for model in registration.models
    }
    assert {
        sum(run.task_id == task_id for run in plan.runs)
        for task_id in {run.task_id for run in plan.runs}
    } == {4}
    assert not any(run.confirmatory for run in plan.runs)


def test_pilot_input_lock_detects_tampering(tmp_path: Path) -> None:
    locked = tmp_path / "manifest.json"
    locked.write_text("frozen", encoding="utf-8")
    lock = _input_lock(tmp_path)
    verify_input_lock(lock, tmp_path)

    locked.write_text("tampered", encoding="utf-8")

    with pytest.raises(ValueError, match="locked input hash mismatch"):
        verify_input_lock(lock, tmp_path)


def test_pilot_dispatch_has_eight_non_retrying_bounded_batches(tmp_path: Path) -> None:
    registration = frozen_pilot_registration(COMMIT)

    commands = build_pilot_commands(
        inspect="/usr/bin/inspect",
        registration=registration,
        root=tmp_path / "sealed",
        log_dir=tmp_path / "logs",
        lineage_id=LINEAGE_ID,
    )

    assert len(commands) == 8
    assert {command[command.index("--epochs") + 1] for command in commands} == {"1"}
    assert {command[command.index("--max-retries") + 1] for command in commands} == {"0"}
    assert all("--time-limit" not in command for command in commands)
    assert {command[command.index("--max-sandboxes") + 1] for command in commands} == {"2"}
    assert all("token_limit=35000" in command for command in commands)
    assert all("token_limit_type=(input*0.5)+output" in command for command in commands)
    assert all("message_limit=20" in command for command in commands)
    assert all("time_limit=90" in command for command in commands)
    assert all("sandbox_timeout=170" in command for command in commands)
    assert {
        next(value for value in command if value.startswith("task_partition="))
        for command in commands
    } == {"task_partition=even", "task_partition=odd"}
    assert all("cpu=0.5" in command for command in commands)
    assert all("memory_mib=2048" in command for command in commands)
    assert all(
        f"sealed_tasks={(tmp_path / 'sealed' / 'agent-tasks.json').resolve()}" in command
        for command in commands
    )
    assert all(
        f"manifest={(tmp_path / 'sealed' / 'manifest.json').resolve()}" in command
        for command in commands
    )
    assert all(
        f"dockerfile={Path('Dockerfile.formal-pilot').resolve()}" in command
        for command in commands
    )


def test_pilot_dispatch_stops_on_systemic_batch_failure() -> None:
    failed_samples = [
        SimpleNamespace(error=RuntimeError("sandbox failed"), scores={}, model_usage={})
        for _ in range(10)
    ]

    with pytest.raises(RuntimeError, match="systemic infrastructure failure"):
        require_sample_batch_health(failed_samples, expected_sample_count=10)

    one_scored_sample = SimpleNamespace(error=None, scores={"join": 1}, model_usage={})
    require_sample_batch_health(
        failed_samples[:-1] + [one_scored_sample],
        expected_sample_count=10,
    )


def test_pilot_dispatch_stops_on_any_lifecycle_infrastructure_failure() -> None:
    healthy = SimpleNamespace(error=None, scores={"join": 1}, model_usage={}, store={})
    failed = SimpleNamespace(
        error=None,
        scores={"join": 0},
        model_usage={},
        store={
            "joinlint.formal_eval.lifecycle.v1": {"infrastructure_status": "failed"}
        },
    )

    with pytest.raises(RuntimeError, match="contains an infrastructure failure"):
        require_sample_batch_health([healthy] * 9 + [failed], expected_sample_count=10)


def test_pilot_budget_report_fails_when_observed_total_exceeds_ceiling() -> None:
    registration = frozen_pilot_registration(COMMIT)
    plan = build_pilot_run_plan(_manifest(20), registration, LINEAGE_ID)
    cost_per_row = 15.0 / len(plan.runs)
    results = AgentResultBundle.model_construct(
        lineage_id=LINEAGE_ID,
        run_plan_sha256=digest_value(plan.model_dump(mode="json")),
        rows=tuple(
            SimpleNamespace(sample_id=run.sample_id, calculated_cost_cny=cost_per_row)
            for run in plan.runs
        ),
    )

    report = pilot_budget_report(registration, plan, results)

    assert report.run_count == 80
    assert report.total_cost_upper_cny > report.approved_budget_cny
    assert report.passed is False


def test_pilot_task_resolves_database_relative_to_sealed_root(tmp_path: Path) -> None:
    pytest.importorskip("inspect_ai")
    from benchmarks.formal_eval.inspect_task import _samples

    sealed_root = tmp_path / "sealed"
    database = sealed_root / "databases" / "case.sqlite"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"SQLite fixture")
    task = _sealed_task()
    manifest = _manifest(1, task=task)

    samples = _samples(
        manifest,
        {task.task_id: task},
        "control",
        "codex",
        LINEAGE_ID,
        sealed_root=sealed_root,
    )

    assert samples[0].files == {"/workspace/data/database.sqlite": str(database.resolve())}
    assert samples[0].metadata["database_path"] == str(database.resolve())


def test_pilot_task_uses_modal_compose_build_and_resource_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("inspect_ai")
    pytest.importorskip("inspect_sandboxes")
    from benchmarks.formal_eval.inspect_task import formal_pilot_eval

    sealed_root = tmp_path / "sealed"
    database = sealed_root / "databases" / "case.sqlite"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"SQLite fixture")
    task = _sealed_task()
    manifest = _manifest(1, task=task)
    tasks_path = sealed_root / "agent-tasks.json"
    manifest_path = sealed_root / "manifest.json"
    tasks_path.write_text(
        "[" + task.model_dump_json(by_alias=True) + "]",
        encoding="utf-8",
    )
    manifest_path.write_text(manifest.model_dump_json(), encoding="utf-8")

    inspect_task = formal_pilot_eval.__wrapped__(
        sealed_tasks=str(tasks_path),
        manifest=str(manifest_path),
        host="codex",
        condition="control",
        agent_version="0.144.1",
        dockerfile="Dockerfile.formal-pilot",
        lineage_id=LINEAGE_ID,
        task_partition="even",
    )

    sandbox = inspect_task.sandbox
    service = sandbox.config.services["default"]
    assert sandbox.type == "modal"
    assert service.build.context == "."
    assert service.build.dockerfile == "Dockerfile.formal-pilot"
    assert service.cpus == 0.5
    assert service.mem_limit == "2048m"
    assert inspect_task.token_limit == 35_000
    assert inspect_task.token_limit_type == "(input*0.5)+output"
    assert inspect_task.message_limit == 20
    assert inspect_task.time_limit is None
    assert sandbox.config.extensions == {"x-modal": {"timeout": 170}}

    from inspect_sandboxes._util.naming import make_sandbox_name

    sandbox_name = make_sandbox_name(
        inspect_task.name,
        {"__sample_id__": "x" * 100},
    )
    assert inspect_task.name == "jl"
    assert len(sandbox_name) < 64

    captured: dict[str, str] = {}

    def fake_image(dockerfile: str, *, context_dir: str) -> str:
        captured.update(dockerfile=dockerfile, context_dir=context_dir)
        return "test-image"

    from inspect_sandboxes.modal import _compose

    monkeypatch.setattr(_compose.modal.Image, "from_dockerfile", fake_image)
    params = _compose.convert_compose_to_modal_params(sandbox.config, None)
    assert Path(captured["dockerfile"]).resolve() == Path("Dockerfile.formal-pilot").resolve()
    assert Path(captured["context_dir"]).resolve() == Path.cwd()
    assert params.kwargs["image"] == "test-image"
    assert params.kwargs["cpu"] == 0.5
    assert params.kwargs["memory"] == 2048
    assert params.kwargs["timeout"] == 170


def test_pilot_task_requires_platform_timeout_margin() -> None:
    pytest.importorskip("inspect_ai")
    from benchmarks.formal_eval.inspect_task import formal_pilot_eval

    with pytest.raises(ValueError, match="resource limits"):
        formal_pilot_eval.__wrapped__(
            sealed_tasks="sealed-tasks.json",
            manifest="manifest.json",
            host="codex",
            condition="control",
            agent_version="0.144.1",
            dockerfile="Dockerfile.formal-pilot",
            lineage_id=LINEAGE_ID,
            task_partition="even",
            time_limit=90,
            sandbox_timeout=150,
        )


def test_pilot_workflow_requires_exact_approval_and_scopes_paid_secrets() -> None:
    workflow_path = Path(".github/workflows/formal-pilot.yml")
    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    text = workflow_path.read_text(encoding="utf-8")
    job = workflow["jobs"]["pilot"]

    assert job["environment"] == "formal-evaluation"
    assert job["runs-on"] == "ubuntu-latest"
    assert workflow["permissions"]["contents"] == "write"
    assert "inputs.confirm_paid != true || inputs.budget_cny != '20'" in text
    assert workflow["on"]["workflow_dispatch"]["inputs"]["pilot_commit"]["default"] == (
        "05da3fb4b2fa8536caef7a28cd9994b8b84a98c9"
    )
    assert workflow["on"]["workflow_dispatch"]["inputs"]["calibration_run_id"]["required"] == (
        "true"
    )
    assert workflow["permissions"]["actions"] == "read"
    assert job["steps"][0]["name"] == (
        "Block paid evaluation pending atomic campaign reservation"
    )
    exact_gate = next(
        step
        for step in job["steps"]
        if step.get("name") == "Require the exact approved paid pilot"
    )
    assert "github.run_attempt != 1" in exact_gate["if"]
    assert "ref: ${{ inputs.pilot_commit }}" in text
    assert "--current-commit \"${{ steps.pilot_commit.outputs.sha }}\"" in text
    assert "pilot_volume" not in text
    assert "modal volume get" not in text
    assert "gh release download \"$PILOT_RELEASE_TAG\"" in text
    assert "tar -xzf \"pilot-release/$PILOT_RELEASE_ASSET\"" in text
    assert "${{ inputs." not in "\n".join(
        step.get("run", "") for step in job["steps"]
    )
    restore_step = next(
        step for step in job["steps"] if step.get("name") == "Restore private frozen pilot inputs"
    )
    assert set(restore_step["env"]) == {
        "GH_TOKEN",
        "PILOT_RELEASE_TAG",
        "PILOT_RELEASE_ASSET",
    }
    run_step = next(
        step for step in job["steps"] if step.get("name") == "Run the approved 20-task pilot"
    )
    assert set(run_step["env"]) == {
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "CAMPAIGN_BUDGET_CNY",
        "CAMPAIGN_SPEND_BEFORE_CNY",
    }
    assert "--campaign-budget-cny \"$CAMPAIGN_BUDGET_CNY\"" in run_step["run"]
    assert "--campaign-spend-before-cny \"$CAMPAIGN_SPEND_BEFORE_CNY\"" in run_step["run"]
    assert "OPENAI_API_KEY" not in text
    calibration_restore = next(
        step
        for step in job["steps"]
        if step.get("name") == "Restore successful calibration attestation"
    )
    assert set(calibration_restore["env"]) == {"GH_TOKEN", "CALIBRATION_RUN_ID"}
    assert "gh run download" in calibration_restore["run"]
    step_names = [step.get("name") for step in job["steps"]]
    assert step_names.index("Run no-model lifecycle gate") < step_names.index(
        "Restore private frozen pilot inputs"
    )
    campaign_preflight = next(
        step
        for step in job["steps"]
        if step.get("name") == "Require cumulative campaign budget for full pilot"
    )
    checkout_index = next(
        index for index, step in enumerate(job["steps"]) if step.get("uses") == "actions/checkout@v4"
    )
    assert job["steps"][checkout_index]["with"]["persist-credentials"] == "false"
    assert step_names.index(campaign_preflight["name"]) < checkout_index
    assert "benchmarks.formal_eval" not in campaign_preflight["run"]
    assert campaign_preflight["env"]["CAMPAIGN_COST_UPPER_CNY"] == "20"
    assert step_names.index("Verify calibration attestation binding") < step_names.index(
        "Run the approved 20-task pilot"
    )
    sanitized_scan = next(
        step for step in job["steps"] if step.get("name") == "Scan sanitized pilot artifacts"
    )
    sanitized_upload = next(
        step
        for step in job["steps"]
        if (step.get("with") or {}).get("name") == "joinlint-formal-pilot-sanitized"
    )
    assert sanitized_scan["id"] == "scan_pilot_sanitized"
    assert "steps.scan_pilot_sanitized.outcome == 'success'" in sanitized_upload["if"]
    assert sanitized_upload["with"]["if-no-files-found"] == "error"


def test_pilot_canary_workflow_has_an_independent_spend_gate() -> None:
    workflow_path = Path(".github/workflows/formal-pilot-canary.yml")
    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    text = workflow_path.read_text(encoding="utf-8")
    job = workflow["jobs"]["canary"]

    assert job["environment"] == "formal-evaluation"
    assert workflow["permissions"]["contents"] == "write"
    assert "inputs.confirm_paid != true" in text
    assert "inputs.calibration != true && inputs.budget_cny != '2.25'" in text
    assert job["steps"][0]["name"] == (
        "Block paid evaluation pending atomic campaign reservation"
    )
    exact_gate = next(
        step
        for step in job["steps"]
        if step.get("name") == "Require the exact approved paid diagnostic"
    )
    assert "github.run_attempt != 1" in exact_gate["if"]
    step_names = [step.get("name") for step in job["steps"]]
    assert step_names.index("Run no-model lifecycle gate") < step_names.index(
        "Run one-task canary"
    )
    assert "--limit" not in text
    assert "benchmarks.formal_eval.pilot_canary" in text
    assert '--workflow-run-id "${{ github.run_id }}"' in text
    run_step = next(
        step for step in job["steps"] if step.get("name") == "Run one-task canary"
    )
    assert set(run_step["env"]) == {
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "CAMPAIGN_BUDGET_CNY",
        "CAMPAIGN_SPEND_BEFORE_CNY",
    }
    assert '--campaign-budget-cny "$CAMPAIGN_BUDGET_CNY"' in run_step["run"]
    assert '--campaign-spend-before-cny "$CAMPAIGN_SPEND_BEFORE_CNY"' in run_step["run"]
    preflight = next(
        step
        for step in job["steps"]
        if step.get("name") == "Require cumulative campaign budget for canary"
    )
    assert step_names.index(preflight["name"]) < step_names.index(run_step["name"])
    checkout_index = next(
        index for index, step in enumerate(job["steps"]) if step.get("uses") == "actions/checkout@v4"
    )
    assert job["steps"][checkout_index]["with"]["persist-credentials"] == "false"
    assert step_names.index(preflight["name"]) < checkout_index
    assert "benchmarks.formal_eval" not in preflight["run"]
    assert preflight["env"]["CAMPAIGN_COST_UPPER_CNY"] == "2.25"
    sanitized_scan = next(
        step for step in job["steps"] if step.get("name") == "Scan sanitized canary artifacts"
    )
    sanitized_upload = next(
        step
        for step in job["steps"]
        if (step.get("with") or {}).get("name")
        == "joinlint-formal-pilot-canary-sanitized"
    )
    assert sanitized_scan["id"] == "scan_canary_sanitized"
    assert "steps.scan_canary_sanitized.outcome == 'success'" in sanitized_upload["if"]
    assert sanitized_upload["with"]["if-no-files-found"] == "error"


def test_pilot_calibration_workflow_has_four_cell_and_campaign_budget_gates() -> None:
    workflow_path = Path(".github/workflows/formal-pilot-canary.yml")
    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    text = workflow_path.read_text(encoding="utf-8")
    job = workflow["jobs"]["canary"]

    assert job["environment"] == "formal-evaluation"
    assert workflow["on"]["workflow_dispatch"]["inputs"]["calibration"]["default"] == (
        "false"
    )
    assert "inputs.calibration == true && inputs.budget_cny != '4'" in text
    step_names = [step.get("name") for step in job["steps"]]
    preflight = next(
        step
        for step in job["steps"]
        if step.get("name") == "Require cumulative campaign budget for calibration"
    )
    run_step = next(
        step for step in job["steps"] if step.get("name") == "Run sealed four-cell calibration"
    )
    assert step_names.index(preflight["name"]) < step_names.index(run_step["name"])
    checkout_index = next(
        index for index, step in enumerate(job["steps"]) if step.get("uses") == "actions/checkout@v4"
    )
    assert step_names.index(preflight["name"]) < checkout_index
    assert "benchmarks.formal_eval" not in preflight["run"]
    assert preflight["env"]["CAMPAIGN_COST_UPPER_CNY"] == "4"
    assert run_step["if"] == "inputs.calibration == true"
    assert set(run_step["env"]) == {
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "CAMPAIGN_BUDGET_CNY",
        "CAMPAIGN_SPEND_BEFORE_CNY",
    }
    assert "--campaign-budget-cny \"$CAMPAIGN_BUDGET_CNY\"" in run_step["run"]
    assert "--campaign-spend-before-cny \"$CAMPAIGN_SPEND_BEFORE_CNY\"" in run_step["run"]
    sanitized_upload = next(
        step
        for step in job["steps"]
        if (step.get("with") or {}).get("name")
        == "joinlint-formal-pilot-calibration-sanitized"
    )
    assert sanitized_upload["with"]["if-no-files-found"] == "error"
    sanitized_scan = next(
        step
        for step in job["steps"]
        if step.get("name") == "Scan sanitized calibration artifacts"
    )
    assert sanitized_scan["id"] == "scan_calibration_sanitized"
    assert "steps.scan_calibration_sanitized.outcome == 'success'" in sanitized_upload["if"]


@pytest.mark.parametrize(
    ("workflow_path", "raw_log_directories"),
    [
        (
            ".github/workflows/formal-pilot-canary.yml",
            (
                "modal-readiness-logs",
                "pilot-canary-logs",
                "pilot-calibration-logs",
            ),
        ),
        (".github/workflows/formal-pilot.yml", ("pilot-logs",)),
        (".github/workflows/formal-evaluation.yml", ("formal-logs",)),
    ],
)
def test_public_formal_workflows_keep_raw_inspect_logs_runner_ephemeral(
    workflow_path: str,
    raw_log_directories: tuple[str, ...],
) -> None:
    workflow = yaml.load(Path(workflow_path).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    uploads = [
        step
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if step.get("uses") == "actions/upload-artifact@v4"
    ]
    uploaded_paths = "\n".join(
        str((step.get("with") or {}).get("path", "")) for step in uploads
    )
    uploaded_names = "\n".join(
        str((step.get("with") or {}).get("name", "")) for step in uploads
    )

    assert not any(path in uploaded_paths for path in raw_log_directories)
    assert "private-inspect-logs" not in uploaded_names
    assert "private-logs" not in uploaded_names
    assert all("outcome == 'success'" in step.get("if", "") for step in uploads)
    scans = [
        step
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if str(step.get("name", "")).startswith("Scan sanitized")
        and step.get("id") != "scan_formal_smoke_sanitized"
    ]
    assert scans
    assert all(
        "benchmarks.formal_eval.public_artifacts" in step["run"] for step in scans
    )
    assert all(
        "steps.public_artifact_verifier.outcome == 'success'" in step["if"]
        for step in scans
    )


@pytest.mark.parametrize(
    ("workflow_path", "job_name", "step_name", "upper"),
    (
        (
            ".github/workflows/formal-pilot-canary.yml",
            "readiness",
            "Require cumulative campaign budget for readiness",
            "2.10",
        ),
        (
            ".github/workflows/formal-pilot-canary.yml",
            "canary",
            "Require cumulative campaign budget for canary",
            "2.25",
        ),
        (
            ".github/workflows/formal-pilot-canary.yml",
            "canary",
            "Require cumulative campaign budget for calibration",
            "4",
        ),
        (
            ".github/workflows/formal-pilot.yml",
            "pilot",
            "Require cumulative campaign budget for full pilot",
            "20",
        ),
    ),
)
@pytest.mark.parametrize(
    "invalid",
    ["", " ", "+1", "-1", "nan", "inf", "-inf", "1e309", "1.0000001"],
)
def test_workflow_campaign_preflight_rejects_noncanonical_amounts(
    workflow_path: str,
    job_name: str,
    step_name: str,
    upper: str,
    invalid: str,
) -> None:
    workflow = yaml.load(
        Path(workflow_path).read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    step = next(
        candidate
        for candidate in workflow["jobs"][job_name]["steps"]
        if candidate.get("name") == step_name
    )
    environment = {
        **os.environ,
        "CAMPAIGN_BUDGET_CNY": "75",
        "CAMPAIGN_SPEND_BEFORE_CNY": invalid,
        "CAMPAIGN_COST_UPPER_CNY": upper,
    }

    result = subprocess.run(
        ["bash", "-c", step["run"]],
        check=False,
        capture_output=True,
        env=environment,
    )

    assert result.returncode != 0


@pytest.mark.parametrize(
    ("workflow_path", "job_name", "step_name", "upper", "spend"),
    (
        (
            ".github/workflows/formal-pilot-canary.yml",
            "readiness",
            "Require cumulative campaign budget for readiness",
            "2.10",
            "72.90",
        ),
        (
            ".github/workflows/formal-pilot-canary.yml",
            "canary",
            "Require cumulative campaign budget for canary",
            "2.25",
            "72.75",
        ),
        (
            ".github/workflows/formal-pilot-canary.yml",
            "canary",
            "Require cumulative campaign budget for calibration",
            "4",
            "71",
        ),
        (
            ".github/workflows/formal-pilot.yml",
            "pilot",
            "Require cumulative campaign budget for full pilot",
            "20",
            "55",
        ),
    ),
)
def test_workflow_campaign_preflight_enforces_the_exact_decimal_boundary(
    workflow_path: str,
    job_name: str,
    step_name: str,
    upper: str,
    spend: str,
) -> None:
    workflow = yaml.load(
        Path(workflow_path).read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    step = next(
        candidate
        for candidate in workflow["jobs"][job_name]["steps"]
        if candidate.get("name") == step_name
    )
    exact_environment = {
        **os.environ,
        "CAMPAIGN_BUDGET_CNY": "75",
        "CAMPAIGN_SPEND_BEFORE_CNY": spend,
        "CAMPAIGN_COST_UPPER_CNY": upper,
    }

    exact = subprocess.run(
        ["bash", "-c", step["run"]],
        check=False,
        capture_output=True,
        env=exact_environment,
    )
    over = subprocess.run(
        ["bash", "-c", step["run"]],
        check=False,
        capture_output=True,
        env={
            **exact_environment,
            "CAMPAIGN_SPEND_BEFORE_CNY": f"{float(spend) + 0.000001:.6f}",
        },
    )

    assert exact.returncode == 0
    assert over.returncode != 0


def _manifest(count: int, *, task: SealedAgentTask | None = None) -> FormalManifestV2:
    tasks = []
    for index in range(count):
        current = task if task is not None else _sealed_task(str(index))
        tasks.append(
            FormalTask(
                task_id=current.task_id,
                database_id=current.database_id,
                database_variant_group=f"variant-{index}",
                corpus="natural",
                split="confirmatory",
                domain="test",
                source_type="sqlite",
                database_scale="small",
                ambiguity="none",
                fanout_type="none",
                question_sha256=_sha256(current.question),
                schema_sha256=_sha256(current.schema_text),
                sql_shape_sha256=_sha256(current.sql_shape),
                schema_family_id=f"schema-{index}",
                question_template_id=f"question-{index}",
                sql_structure_id=f"sql-{index}",
                allowed_graphs=current.allowed_graphs,
                oracle_has_safe_path=True,
                join_depth=1,
            )
        )
    return FormalManifestV2(dataset_release="pilot-test", tasks=tuple(tasks))


def _sealed_task(suffix: str = "0") -> SealedAgentTask:
    return SealedAgentTask(
        task_id=f"task-{suffix}",
        database_id=f"database-{suffix}",
        question="List each child and parent.",
        schema_text="child(parent_id), parent(id)",
        schema={"child": {"parent_id": "INTEGER"}, "parent": {"id": "INTEGER"}},
        sql_shape="select-join",
        gold_sql="SELECT * FROM child JOIN parent ON child.parent_id = parent.id",
        database_path="databases/case.sqlite",
        expected_entities=("child", "parent"),
        allowed_graphs=((('child.parent_id', 'parent.id'),),),
        oracle_has_safe_path=True,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
