from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks.formal_eval import pilot_calibration
from benchmarks.formal_eval.pilot import frozen_pilot_registration
from benchmarks.formal_eval.pilot_calibration import (
    CALIBRATION_TOKEN_ACCOUNTING_CEILINGS,
    CALIBRATION_TOKEN_LIMITS,
    CalibrationFailureReport,
    HarnessAttestation,
    InfrastructureAttestation,
    PilotCalibrationReport,
    ResourceAttestation,
    ScoringAttestation,
)


COMMIT = "a" * 40
LINEAGE_ID = "b" * 64


def _calibration_payload(schema_version: int) -> dict[str, object]:
    resource_contract: dict[str, object] = {
        "token_limit_by_host": CALIBRATION_TOKEN_LIMITS,
    }
    if schema_version >= 4:
        resource_contract["token_accounting_ceiling_by_host"] = (
            CALIBRATION_TOKEN_ACCOUNTING_CEILINGS
        )
    payload: dict[str, object] = {
        "schema_version": schema_version,
        "status": "failed",
        "calibration_task_ids": ("task-a", "task-b"),
        "resource_contract": resource_contract,
        "infrastructure": {"status": "failed", "cells": ()},
        "harness": {"status": "failed", "cells": ()},
        "scoring": {"status": "failed", "cells": ()},
        "actual_model_cost_cny": 0.0,
        "modal_compute_upper_cny": 0.2,
        "modal_image_build_reserve_cny": 2.0,
        "total_cost_upper_cny": 2.2,
        "campaign_spend_before_cny": 0.0,
        "campaign_budget_cny": 10.0,
        "campaign_total_upper_cny": 2.2,
        "workflow_run_id": 123,
        "joinlint_commit": COMMIT,
        "input_lock_sha256": "c" * 64,
        "dependency_versions": {},
    }
    if schema_version >= 2:
        payload.update(
            readiness_status="failed",
            resource={"status": "failed", "cells": (), "hosts": ()},
        )
    return payload


@pytest.mark.parametrize("schema_version", range(1, 7))
def test_legacy_calibration_omitted_sandbox_timeout_means_150(
    schema_version: int,
) -> None:
    report = PilotCalibrationReport.model_validate(
        _calibration_payload(schema_version)
    )

    assert report.resource_contract.sandbox_timeout_seconds == 150


@pytest.mark.parametrize("schema_version", range(1, 7))
def test_legacy_calibration_rejects_explicit_current_sandbox_timeout(
    schema_version: int,
) -> None:
    payload = _calibration_payload(schema_version)
    resource_contract = payload["resource_contract"]
    assert isinstance(resource_contract, dict)
    resource_contract["sandbox_timeout_seconds"] = 170

    with pytest.raises(ValueError, match="sandbox timeout"):
        PilotCalibrationReport.model_validate(payload)


@pytest.mark.parametrize(
    ("remove_schema", "remove_timeout", "message"),
    (
        (True, False, "explicit schema version"),
        (False, True, "explicit sandbox timeout"),
    ),
)
def test_current_calibration_loader_requires_explicit_raw_contract_fields(
    tmp_path: Path,
    remove_schema: bool,
    remove_timeout: bool,
    message: str,
) -> None:
    payload = _calibration_payload(7)
    resource_contract = payload["resource_contract"]
    assert isinstance(resource_contract, dict)
    resource_contract["sandbox_timeout_seconds"] = 170
    if remove_schema:
        payload.pop("schema_version")
    if remove_timeout:
        resource_contract.pop("sandbox_timeout_seconds")
    attestation = tmp_path / "calibration.json"
    attestation.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        pilot_calibration._load_current_calibration_attestation(attestation)


def test_current_calibration_loader_rejects_success_and_failure_siblings(
    tmp_path: Path,
) -> None:
    payload = _calibration_payload(7)
    resource_contract = payload["resource_contract"]
    assert isinstance(resource_contract, dict)
    resource_contract["sandbox_timeout_seconds"] = 170
    attestation = tmp_path / "calibration.json"
    attestation.write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "calibration-failure.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="success and failure artifacts coexist"):
        pilot_calibration._load_current_calibration_attestation(attestation)


def test_current_calibration_loader_accepts_explicit_raw_contract(
    tmp_path: Path,
) -> None:
    payload = _calibration_payload(7)
    resource_contract = payload["resource_contract"]
    assert isinstance(resource_contract, dict)
    resource_contract["sandbox_timeout_seconds"] = 170
    attestation = tmp_path / "calibration.json"
    attestation.write_text(json.dumps(payload), encoding="utf-8")

    report = pilot_calibration._load_current_calibration_attestation(attestation)

    assert report.schema_version == 7
    assert report.resource_contract.sandbox_timeout_seconds == 170


@pytest.mark.parametrize(
    ("needle", "replacement"),
    (
        ('"schema_version": 7', '"schema_version": 6, "schema_version": 7'),
        (
            '"sandbox_timeout_seconds": 170',
            '"sandbox_timeout_seconds": 150, "sandbox_timeout_seconds": 170',
        ),
    ),
)
def test_current_calibration_loader_rejects_duplicate_json_keys(
    tmp_path: Path,
    needle: str,
    replacement: str,
) -> None:
    payload = _calibration_payload(7)
    resource_contract = payload["resource_contract"]
    assert isinstance(resource_contract, dict)
    resource_contract["sandbox_timeout_seconds"] = 170
    raw = json.dumps(payload)
    assert needle in raw
    attestation = tmp_path / "calibration.json"
    attestation.write_text(raw.replace(needle, replacement, 1), encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate JSON key"):
        pilot_calibration._load_current_calibration_attestation(attestation)


def _stub_calibration_run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    log_dir: Path,
) -> None:
    registration = frozen_pilot_registration(COMMIT)
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
    monkeypatch.setattr(pilot_calibration, "_input_lock_sha256", lambda root: "c" * 64)


def test_calibration_nan_cost_does_not_mask_original_error_and_keeps_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_dir = tmp_path / "logs"
    output = tmp_path / "artifacts"
    _stub_calibration_run(monkeypatch, log_dir=log_dir)
    monkeypatch.setattr(
        pilot_calibration,
        "require_batch_health",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("original batch failure")
        ),
    )
    monkeypatch.setattr(
        pilot_calibration,
        "observed_model_cost_cny",
        lambda *args: float("nan"),
    )
    monkeypatch.setattr(pilot_calibration, "_observed_sample_count", lambda path: 6)

    with pytest.raises(RuntimeError, match="original batch failure"):
        pilot_calibration.run_calibration(
            tmp_path / "sealed",
            log_dir,
            output,
            workflow_run_id=123,
            campaign_budget_cny=75,
            campaign_spend_before_cny=64,
        )

    failure = CalibrationFailureReport.model_validate_json(
        (output / "calibration-failure.json").read_bytes()
    )
    assert failure.observed_sample_count == 6
    assert failure.actual_model_cost_cny == 0


def test_calibration_publishes_success_atomically_before_clearing_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_dir = tmp_path / "logs"
    output = tmp_path / "artifacts"
    _stub_calibration_run(monkeypatch, log_dir=log_dir)
    monkeypatch.setattr(pilot_calibration, "require_batch_health", lambda *args, **kwargs: None)
    monkeypatch.setattr(pilot_calibration, "observed_model_cost_cny", lambda *args: 0.0)
    monkeypatch.setattr(pilot_calibration, "_observed_sample_count", lambda path: 8)
    monkeypatch.setattr(pilot_calibration, "dependency_versions", lambda: {})
    monkeypatch.setattr(
        pilot_calibration,
        "verify_calibration_logs",
        lambda *args, **kwargs: (
            InfrastructureAttestation(status="failed", cells=()),
            ResourceAttestation(status="failed", cells=(), hosts=()),
            HarnessAttestation(status="failed", cells=()),
            ScoringAttestation(status="failed", cells=()),
        ),
    )
    original_write = pilot_calibration._write_calibration_artifact

    def observe_write(path: Path, payload: bytes) -> None:
        if path.name == "calibration.json":
            assert (output / "calibration-failure.json").is_file()
        original_write(path, payload)

    monkeypatch.setattr(pilot_calibration, "_write_calibration_artifact", observe_write)

    pilot_calibration.run_calibration(
        tmp_path / "sealed",
        log_dir,
        output,
        workflow_run_id=123,
        campaign_budget_cny=75,
        campaign_spend_before_cny=64,
    )

    assert (output / "calibration.json").is_file()
    assert not (output / "calibration-failure.json").exists()


@pytest.mark.parametrize(
    ("log_parts", "output_parts"),
    (
        (("shared",), ("shared",)),
        (("shared", "raw"), ("shared",)),
        (("shared",), ("shared", "sanitized")),
    ),
)
def test_calibration_rejects_overlapping_log_and_output_directories(
    tmp_path: Path,
    log_parts: tuple[str, ...],
    output_parts: tuple[str, ...],
) -> None:
    log_dir = tmp_path.joinpath(*log_parts)
    output = tmp_path.joinpath(*output_parts)

    with pytest.raises(ValueError, match="must be disjoint"):
        pilot_calibration.run_calibration(
            tmp_path / "sealed",
            log_dir,
            output,
            workflow_run_id=123,
            campaign_budget_cny=75,
            campaign_spend_before_cny=64,
        )
