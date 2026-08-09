from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
import yaml

from benchmarks.formal_eval.cli import main as formal_eval_main
from benchmarks.formal_eval.manifest import ManifestReadiness
from benchmarks.formal_eval.pilot import PilotBudgetCheckpoint
from benchmarks.formal_eval.pilot_canary import PilotCanaryReport
from benchmarks.formal_eval.public_artifacts import (
    _validate_json_artifact,
    validate_public_artifacts,
)
from joinlint.contracts import canonical_json


VERIFIER_COMMIT = "0389c4bd34597f06676d4c560f5e7683fd2b20c9"


def _canary_report(**updates: object) -> PilotCanaryReport:
    payload: dict[str, object] = {
        "model_id": "openai-api/deepseek/deepseek-v4-pro",
        "scorer_artifacts": (
            "formal_execution_scorer",
            "formal_join_scorer",
        ),
        "actual_model_cost_cny": 0.01,
        "modal_compute_upper_cny": 0.031728,
        "modal_image_build_reserve_cny": 2.0,
        "total_cost_upper_cny": 2.041728,
        "workflow_run_id": 123,
        "joinlint_commit": "a" * 40,
        "input_lock_sha256": "b" * 64,
        "dependency_versions": {"inspect-ai": "0.3.249"},
    }
    payload.update(updates)
    return PilotCanaryReport.model_validate(payload)


def _write_model(path: Path, model: PilotCanaryReport) -> None:
    path.write_bytes(canonical_json(model.model_dump(mode="json")) + b"\n")


def test_public_canary_accepts_only_the_strict_canonical_schema(tmp_path: Path) -> None:
    _write_model(tmp_path / "canary.json", _canary_report())

    assert validate_public_artifacts(tmp_path, "pilot-canary") == ("canary.json",)

    (tmp_path / "canary.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        validate_public_artifacts(tmp_path, "pilot-canary")


def test_public_artifact_prefix_profiles_accept_typed_failure_evidence(
    tmp_path: Path,
) -> None:
    formal = tmp_path / "formal"
    formal.mkdir()
    readiness = ManifestReadiness(
        confirmatory_task_count=0,
        confirmatory_database_count=0,
        diagnostic_task_count=0,
        model_count=2,
        host_count=2,
        duplicate_pair_count=0,
        near_duplicate_pair_count=0,
        cross_split_variant_count=0,
        preregistration_matches_manifest=True,
        ready=False,
        findings=("INSUFFICIENT_CONFIRMATORY_TASKS",),
    )
    (formal / "readiness.json").write_bytes(
        canonical_json(readiness.model_dump(mode="json"))
    )
    assert validate_public_artifacts(formal, "formal-evaluation") == (
        "readiness.json",
    )

    pilot = tmp_path / "pilot"
    pilot.mkdir()
    checkpoint = PilotBudgetCheckpoint(
        approved_budget_cny=20,
        completed_batches=0,
        total_batches=8,
        actual_model_cost_cny=0,
        remaining_model_cost_upper_cny=17.99584,
        modal_compute_upper_cny=0,
        modal_image_build_reserve_cny=2,
        projected_total_upper_cny=19.99584,
        safe_to_continue=True,
    )
    (pilot / "budget-checkpoint.json").write_bytes(
        canonical_json(checkpoint.model_dump(mode="json")) + b"\n"
    )
    assert validate_public_artifacts(pilot, "pilot") == (
        "budget-checkpoint.json",
    )


def test_public_report_markdown_must_rebuild_from_its_json(tmp_path: Path) -> None:
    generated = tmp_path / "generated"
    public = tmp_path / "public"
    assert formal_eval_main(["fake-model", "--output", str(generated)]) == 0
    public.mkdir()
    for name in ("formal-evaluation.json", "formal-evaluation.md"):
        shutil.copyfile(generated / name, public / name)

    assert validate_public_artifacts(public, "formal-report") == (
        "formal-evaluation.json",
        "formal-evaluation.md",
    )

    markdown = public / "formal-evaluation.md"
    markdown.write_text(markdown.read_text(encoding="utf-8") + "private suffix\n")
    with pytest.raises(ValueError, match="Markdown"):
        validate_public_artifacts(public, "formal-report")


def test_public_artifacts_reject_unknown_symlink_and_special_paths(
    tmp_path: Path,
) -> None:
    _write_model(tmp_path / "canary.json", _canary_report())
    (tmp_path / "leak.sql").write_text("SELECT customer_email FROM customers")
    with pytest.raises(ValueError, match="unexpected public artifact path"):
        validate_public_artifacts(tmp_path, "pilot-canary")

    (tmp_path / "leak.sql").unlink()
    (tmp_path / "canary.json").unlink()
    outside = tmp_path.parent / "private-question.txt"
    outside.write_text("How many customers placed orders?", encoding="utf-8")
    (tmp_path / "canary.json").symlink_to(outside)
    with pytest.raises(ValueError, match="symbolic links"):
        validate_public_artifacts(tmp_path, "pilot-canary")

    (tmp_path / "canary.json").unlink()
    os.mkfifo(tmp_path / "canary.json")
    with pytest.raises(ValueError, match="regular files"):
        validate_public_artifacts(tmp_path, "pilot-canary")


@pytest.mark.parametrize(
    "updates",
    (
        {"model_id": "How many customers placed orders?"},
        {"dependency_versions": {"inspect-ai": "sk-testsecretvalue012345678901"}},
    ),
)
def test_public_artifacts_reject_private_free_text_and_secret_shapes(
    tmp_path: Path,
    updates: dict[str, object],
) -> None:
    _write_model(tmp_path / "canary.json", _canary_report(**updates))

    with pytest.raises(ValueError, match="private content"):
        validate_public_artifacts(tmp_path, "pilot-canary")


def test_public_artifacts_reject_duplicate_keys_before_model_validation(
    tmp_path: Path,
) -> None:
    payload = json.loads(
        canonical_json(_canary_report().model_dump(mode="json")).decode("utf-8")
    )
    prefix = json.dumps(payload, separators=(",", ":"))[:-1]
    (tmp_path / "canary.json").write_text(
        f'{prefix},"workflow_run_id":456}}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate keys"):
        validate_public_artifacts(tmp_path, "pilot-canary")


@pytest.mark.parametrize(
    ("relative_path", "trailing_lf"),
    (
        ("readiness.json", False),
        ("lineage.json", False),
        ("runtime-contract.json", False),
        ("run-plan.json", False),
        ("run-plan-summary.json", False),
        ("power-plan.json", False),
        ("power-readiness.json", False),
        ("deterministic-evidence.json", False),
        ("diagnostic-results.json", False),
        ("diagnostic-cleaning.json", False),
        ("diagnostic-canary.json", False),
        ("agent-results.json", False),
        ("cleaning.json", False),
        ("report/formal-evaluation.json", False),
        ("formal-evaluation.json", False),
        ("modal-readiness.json", True),
        ("canary.json", True),
        ("calibration.json", True),
        ("calibration-failure.json", True),
        ("budget-checkpoint.json", True),
        ("agent-results-bundle.json", False),
        ("pilot-agent-results.json", True),
        ("budget.json", True),
        ("campaign-budget.json", True),
    ),
)
def test_every_public_json_path_requires_its_strict_schema(
    relative_path: str,
    trailing_lf: bool,
) -> None:
    raw = b"{}\n" if trailing_lf else b"{}"

    with pytest.raises(ValueError, match="schema"):
        _validate_json_artifact(relative_path, raw)


def test_public_artifacts_reject_noncanonical_and_non_utf8_json(
    tmp_path: Path,
) -> None:
    payload = _canary_report().model_dump(mode="json")
    (tmp_path / "canary.json").write_text(
        json.dumps(payload) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="canonical"):
        validate_public_artifacts(tmp_path, "pilot-canary")

    (tmp_path / "canary.json").write_bytes(b"\xff\n")
    with pytest.raises(ValueError, match="UTF-8"):
        validate_public_artifacts(tmp_path, "pilot-canary")


def test_task_identifier_does_not_look_like_an_api_key(tmp_path: Path) -> None:
    _write_model(tmp_path / "canary.json", _canary_report(model_id="task-a"))

    assert validate_public_artifacts(tmp_path, "pilot-canary") == ("canary.json",)


def test_calibration_public_artifact_terminal_states_are_mutually_exclusive(
    tmp_path: Path,
) -> None:
    (tmp_path / "calibration.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "calibration-failure.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="terminal inventory"):
        validate_public_artifacts(tmp_path, "pilot-calibration")


@pytest.mark.parametrize(
    (
        "workflow_path",
        "job_name",
        "scan_name",
        "profile",
        "artifact_root",
    ),
    (
        (
            ".github/workflows/formal-evaluation.yml",
            "formal",
            "Scan sanitized formal evidence",
            "formal-evaluation",
            "formal-artifacts",
        ),
        (
            ".github/workflows/formal-pilot-canary.yml",
            "readiness",
            "Scan sanitized readiness artifacts",
            "modal-readiness",
            "modal-readiness-artifacts",
        ),
        (
            ".github/workflows/formal-pilot-canary.yml",
            "canary",
            "Scan sanitized canary artifacts",
            "pilot-canary",
            "pilot-canary-artifacts",
        ),
        (
            ".github/workflows/formal-pilot-canary.yml",
            "canary",
            "Scan sanitized calibration artifacts",
            "pilot-calibration",
            "pilot-calibration-artifacts",
        ),
        (
            ".github/workflows/formal-pilot.yml",
            "pilot",
            "Scan sanitized pilot artifacts",
            "pilot",
            "pilot-artifacts",
        ),
        (
            ".github/workflows/formal-evaluation-report.yml",
            "report",
            "Scan final public artifacts",
            "formal-report",
            "final-report",
        ),
    ),
)
def test_evidence_scans_run_only_from_the_pinned_verifier_checkout(
    workflow_path: str,
    job_name: str,
    scan_name: str,
    profile: str,
    artifact_root: str,
) -> None:
    workflow = yaml.load(
        Path(workflow_path).read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    steps = workflow["jobs"][job_name]["steps"]
    checkout = next(
        step
        for step in steps
        if step.get("name") == "Checkout pinned public artifact verifier"
    )
    assert checkout["id"] == "public_artifact_verifier"
    assert checkout["if"] == "always()"
    assert checkout["uses"] == "actions/checkout@v4"
    assert checkout["with"] == {
        "ref": VERIFIER_COMMIT,
        "path": ".workflow-verifier",
        "persist-credentials": "false",
    }

    scan = next(step for step in steps if step.get("name") == scan_name)
    assert "steps.public_artifact_verifier.outcome == 'success'" in scan["if"]
    assert scan["working-directory"] == ".workflow-verifier"
    assert scan["env"] == {
        "PYTHONPATH": (
            "${{ github.workspace }}/.workflow-verifier/src:"
            "${{ github.workspace }}/.workflow-verifier"
        )
    }
    assert scan["run"] == (
        "python -m benchmarks.formal_eval.public_artifacts "
        f"--profile {profile} --root \"$GITHUB_WORKSPACE/{artifact_root}\""
    )
