from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from benchmarks.formal_eval.contracts import (
    AgentResultBundle,
    AgentResultRow,
    DeterministicEvidenceBundle,
    StrictModel,
)
from benchmarks.formal_eval.export import ExportSummary
from benchmarks.formal_eval.gates import CanaryReport, diagnostic_canary_gate
from benchmarks.formal_eval.lineage import EvaluationLineage, digest_value
from benchmarks.formal_eval.manifest import ManifestReadiness
from benchmarks.formal_eval.modal_readiness import ModalReadinessReport
from benchmarks.formal_eval.pilot import PilotBudgetCheckpoint, PilotBudgetReport
from benchmarks.formal_eval.pilot_calibration import (
    CalibrationFailureReport,
    PilotCalibrationReport,
)
from benchmarks.formal_eval.pilot_canary import PilotCanaryReport
from benchmarks.formal_eval.pilot_dispatch import (
    PilotCampaignBudget,
    pilot_campaign_budget,
)
from benchmarks.formal_eval.pilot_stage import (
    PilotStageEffect,
    PilotStagePreregistration,
    stage_effect_report,
)
from benchmarks.formal_eval.power import PowerPlan, PowerReadiness
from benchmarks.formal_eval.report import FormalEvaluationReport, render_markdown
from benchmarks.formal_eval.run_plan import RunPlanV2, summarize_run_plan
from benchmarks.formal_eval.runtime_contract import RuntimeContractReport
from joinlint.contracts import canonical_json


class RunPlanSummary(StrictModel):
    total: int = Field(ge=0)
    confirmatory: int = Field(ge=0)
    diagnostic: int = Field(ge=0)

    @model_validator(mode="after")
    def require_complete_partition(self) -> RunPlanSummary:
        if self.total != self.confirmatory + self.diagnostic:
            raise ValueError("run-plan summary counts are inconsistent")
        return self


@dataclass(frozen=True)
class JsonArtifactSpec:
    adapter: TypeAdapter[Any]
    trailing_lf: bool


PUBLIC_ARTIFACT_FILES: dict[str, tuple[str, ...]] = {
    "formal-evaluation": (
        "readiness.json",
        "lineage.json",
        "runtime-contract.json",
        "run-plan.json",
        "run-plan-summary.json",
        "power-plan.json",
        "power-readiness.json",
        "deterministic-evidence.json",
        "diagnostic-results.json",
        "diagnostic-cleaning.json",
        "diagnostic-canary.json",
        "agent-results.json",
        "cleaning.json",
        "report/formal-evaluation.json",
        "report/formal-evaluation.md",
    ),
    "formal-report": (
        "formal-evaluation.json",
        "formal-evaluation.md",
    ),
    "modal-readiness": ("modal-readiness.json",),
    "pilot-canary": ("canary.json",),
    "pilot-calibration": (
        "calibration.json",
        "calibration-failure.json",
    ),
    "pilot": (
        "budget-checkpoint.json",
        "agent-results-bundle.json",
        "cleaning.json",
        "pilot-agent-results.json",
        "budget.json",
        "campaign-budget.json",
    ),
    "pilot-stage": (
        "stage.json",
        "stage-run-plan.json",
        "budget-checkpoint.json",
        "agent-results-bundle.json",
        "cleaning.json",
        "pilot-agent-results.json",
        "budget.json",
        "campaign-budget.json",
        "effect.json",
    ),
}

_JSON_ARTIFACT_SPECS = {
    "readiness.json": JsonArtifactSpec(TypeAdapter(ManifestReadiness), False),
    "lineage.json": JsonArtifactSpec(TypeAdapter(EvaluationLineage), False),
    "runtime-contract.json": JsonArtifactSpec(
        TypeAdapter(RuntimeContractReport), False
    ),
    "run-plan.json": JsonArtifactSpec(TypeAdapter(RunPlanV2), False),
    "run-plan-summary.json": JsonArtifactSpec(TypeAdapter(RunPlanSummary), False),
    "power-plan.json": JsonArtifactSpec(TypeAdapter(PowerPlan), False),
    "power-readiness.json": JsonArtifactSpec(TypeAdapter(PowerReadiness), False),
    "deterministic-evidence.json": JsonArtifactSpec(
        TypeAdapter(DeterministicEvidenceBundle), False
    ),
    "diagnostic-results.json": JsonArtifactSpec(TypeAdapter(AgentResultBundle), False),
    "diagnostic-cleaning.json": JsonArtifactSpec(TypeAdapter(ExportSummary), False),
    "diagnostic-canary.json": JsonArtifactSpec(TypeAdapter(CanaryReport), False),
    "agent-results.json": JsonArtifactSpec(TypeAdapter(AgentResultBundle), False),
    "cleaning.json": JsonArtifactSpec(TypeAdapter(ExportSummary), False),
    "report/formal-evaluation.json": JsonArtifactSpec(
        TypeAdapter(FormalEvaluationReport), False
    ),
    "formal-evaluation.json": JsonArtifactSpec(
        TypeAdapter(FormalEvaluationReport), False
    ),
    "modal-readiness.json": JsonArtifactSpec(TypeAdapter(ModalReadinessReport), True),
    "canary.json": JsonArtifactSpec(TypeAdapter(PilotCanaryReport), True),
    "calibration.json": JsonArtifactSpec(TypeAdapter(PilotCalibrationReport), True),
    "calibration-failure.json": JsonArtifactSpec(
        TypeAdapter(CalibrationFailureReport), True
    ),
    "budget-checkpoint.json": JsonArtifactSpec(
        TypeAdapter(PilotBudgetCheckpoint), True
    ),
    "agent-results-bundle.json": JsonArtifactSpec(
        TypeAdapter(AgentResultBundle), False
    ),
    "pilot-agent-results.json": JsonArtifactSpec(
        TypeAdapter(tuple[AgentResultRow, ...]), True
    ),
    "budget.json": JsonArtifactSpec(TypeAdapter(PilotBudgetReport), True),
    "campaign-budget.json": JsonArtifactSpec(TypeAdapter(PilotCampaignBudget), True),
    "stage.json": JsonArtifactSpec(TypeAdapter(PilotStagePreregistration), True),
    "stage-run-plan.json": JsonArtifactSpec(TypeAdapter(RunPlanV2), True),
    "effect.json": JsonArtifactSpec(TypeAdapter(PilotStageEffect), True),
}

_PREFIX_PROFILES = frozenset({"formal-evaluation", "pilot", "pilot-stage"})
_TERMINAL_INVENTORIES = {
    "formal-report": (frozenset(PUBLIC_ARTIFACT_FILES["formal-report"]),),
    "modal-readiness": (frozenset(PUBLIC_ARTIFACT_FILES["modal-readiness"]),),
    "pilot-canary": (frozenset(PUBLIC_ARTIFACT_FILES["pilot-canary"]),),
    "pilot-calibration": (
        frozenset({"calibration.json"}),
        frozenset({"calibration-failure.json"}),
    ),
}

_ALLOWED_WHITESPACE_VALUES = frozenset({"2025.06 Stable"})
_FORBIDDEN_FIELD_NAMES = frozenset(
    {
        "question",
        "question_text",
        "schema",
        "schema_text",
        "gold_sql",
        "database_path",
        "raw_rows",
        "transcript",
        "prompt",
        "completion",
        "api_key",
        "token",
        "secret",
    }
)
_FORBIDDEN_TEXT = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "MODAL_TOKEN_ID",
    "MODAL_TOKEN_SECRET",
)
_SECRET_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?<![A-Za-z0-9])gh[opurs]_[A-Za-z0-9]{20,}"),
    re.compile(r"(?<![A-Z0-9])AKIA[A-Z0-9]{16}(?![A-Z0-9])"),
)
_SQL_PATTERN = re.compile(
    r"(?is)\b(?:select|with|insert|update|delete|merge|drop|alter|create)\b"
    r".{0,256}\b(?:from|into|join|table|where|set)\b"
)
_PRIVATE_PATH_PATTERN = re.compile(
    r"(?:^|[/\\])(?:Users|home|workspace|private|tmp)(?:[/\\])|\.sqlite(?:\b|$)"
)


def validate_public_artifacts(root: Path, profile: str) -> tuple[str, ...]:
    if profile not in PUBLIC_ARTIFACT_FILES:
        raise ValueError("unknown public artifact profile")
    if root.is_symlink():
        raise ValueError("public artifact root must not use symbolic links")
    if not root.is_dir():
        raise ValueError("public artifact root must be a directory")

    ordered_paths = PUBLIC_ARTIFACT_FILES[profile]
    allowed_files = frozenset(ordered_paths)
    allowed_directories = _allowed_directories(ordered_paths)
    actual_files: set[str] = set()
    paths: dict[str, Path] = {}
    for path in root.rglob("*"):
        relative_path = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValueError("public artifacts must not use symbolic links")
        if path.is_dir():
            if relative_path not in allowed_directories:
                raise ValueError(f"unexpected public artifact path: {relative_path}")
            continue
        if not path.is_file():
            raise ValueError("public artifacts must contain only regular files")
        if relative_path not in allowed_files:
            raise ValueError(f"unexpected public artifact path: {relative_path}")
        actual_files.add(relative_path)
        paths[relative_path] = path

    _require_valid_inventory(profile, ordered_paths, frozenset(actual_files))
    values = {
        relative_path: _validate_json_artifact(relative_path, path.read_bytes())
        for relative_path, path in paths.items()
        if relative_path.endswith(".json")
    }
    _validate_cross_file_invariants(profile, values, paths)
    return tuple(path for path in ordered_paths if path in actual_files)


def _allowed_directories(paths: tuple[str, ...]) -> frozenset[str]:
    directories: set[str] = set()
    for path in paths:
        for parent in PurePosixPath(path).parents:
            if parent != PurePosixPath("."):
                directories.add(parent.as_posix())
    return frozenset(directories)


def _require_valid_inventory(
    profile: str,
    ordered_paths: tuple[str, ...],
    actual_files: frozenset[str],
) -> None:
    if profile in _PREFIX_PROFILES:
        valid_prefixes = {
            frozenset(ordered_paths[:length])
            for length in range(1, len(ordered_paths) + 1)
        }
        if actual_files not in valid_prefixes:
            raise ValueError("public artifact files must form an ordered prefix")
        return
    if actual_files not in _TERMINAL_INVENTORIES[profile]:
        raise ValueError("public artifact terminal inventory is invalid")


def _validate_json_artifact(relative_path: str, raw: bytes) -> Any:
    spec = _JSON_ARTIFACT_SPECS[relative_path]
    if spec.trailing_lf:
        if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
            raise ValueError("public artifact JSON has an invalid line ending")
        payload = raw[:-1]
    else:
        if raw.endswith(b"\n"):
            raise ValueError("public artifact JSON has an invalid line ending")
        payload = raw
    try:
        text = payload.decode("utf-8")
        decoded = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
        )
    except UnicodeDecodeError as error:
        raise ValueError("public artifacts must be UTF-8") from error
    except json.JSONDecodeError as error:
        raise ValueError("public artifact JSON is invalid") from error
    _reject_private_content(decoded)
    try:
        value = spec.adapter.validate_json(payload, strict=True)
    except (ValidationError, ValueError) as error:
        raise ValueError(f"public artifact schema is invalid: {relative_path}") from error
    if relative_path == "calibration.json" and value.schema_version != 7:
        raise ValueError("public calibration artifact must use schema v7")
    canonical = canonical_json(spec.adapter.dump_python(value, mode="json"))
    expected = canonical + (b"\n" if spec.trailing_lf else b"")
    if raw != expected:
        raise ValueError("public artifact JSON must be explicit and canonical")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("public artifact JSON contains duplicate keys")
        value[key] = item
    return value


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"public artifact JSON contains non-finite value: {value}")


def _reject_private_content(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.casefold() in _FORBIDDEN_FIELD_NAMES:
                raise ValueError("public artifact contains forbidden private content")
            _reject_private_text(key)
            _reject_private_content(item)
        return
    if isinstance(value, list):
        for item in value:
            _reject_private_content(item)
        return
    if isinstance(value, str):
        _reject_private_text(value)


def _reject_private_text(value: str) -> None:
    if value not in _ALLOWED_WHITESPACE_VALUES and any(
        character.isspace() for character in value
    ):
        raise ValueError("public artifact contains forbidden private content")
    if any(marker in value for marker in _FORBIDDEN_TEXT):
        raise ValueError("public artifact contains forbidden private content")
    if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
        raise ValueError("public artifact contains forbidden private content")
    if _SQL_PATTERN.search(value) or _PRIVATE_PATH_PATTERN.search(value):
        raise ValueError("public artifact contains forbidden private content")


def _validate_cross_file_invariants(
    profile: str,
    values: dict[str, Any],
    paths: dict[str, Path],
) -> None:
    if profile == "formal-evaluation":
        _validate_formal_artifacts(values, paths)
    elif profile == "formal-report":
        _validate_report_markdown(
            values["formal-evaluation.json"],
            paths["formal-evaluation.md"],
        )
    elif profile == "pilot":
        _validate_pilot_artifacts(values)
    elif profile == "pilot-stage":
        _validate_pilot_stage_artifacts(values)


def _validate_formal_artifacts(values: dict[str, Any], paths: dict[str, Path]) -> None:
    run_plan = values.get("run-plan.json")
    summary = values.get("run-plan-summary.json")
    if run_plan is not None and summary is not None:
        if summary.model_dump(mode="json") != summarize_run_plan(run_plan):
            raise ValueError("public run-plan summary does not match its run plan")

    diagnostic = values.get("diagnostic-results.json")
    diagnostic_canary = values.get("diagnostic-canary.json")
    if diagnostic is not None and diagnostic_canary is not None:
        expected_canary = diagnostic_canary_gate(diagnostic.rows)
        if diagnostic_canary != expected_canary:
            raise ValueError("public diagnostic canary does not match its rows")

    for bundle_name, summary_name in (
        ("diagnostic-results.json", "diagnostic-cleaning.json"),
        ("agent-results.json", "cleaning.json"),
    ):
        bundle = values.get(bundle_name)
        export_summary = values.get(summary_name)
        if bundle is not None and export_summary is not None:
            _validate_export_summary(bundle, export_summary)

    lineage = values.get("lineage.json")
    if lineage is not None:
        lineage_ids = [
            getattr(values[name], "lineage_id", None)
            for name in (
                "run-plan.json",
                "power-plan.json",
                "deterministic-evidence.json",
                "diagnostic-results.json",
                "agent-results.json",
            )
            if name in values
        ]
        report = values.get("report/formal-evaluation.json")
        if report is not None:
            lineage_ids.append(report.provenance.lineage_id)
        if any(value != lineage.lineage_id for value in lineage_ids):
            raise ValueError("public formal artifacts have inconsistent lineage")

    report = values.get("report/formal-evaluation.json")
    markdown = paths.get("report/formal-evaluation.md")
    if report is not None and markdown is not None:
        _validate_report_markdown(report, markdown)


def _validate_export_summary(
    bundle: AgentResultBundle,
    summary: ExportSummary,
) -> None:
    if summary.row_count != len(bundle.rows):
        raise ValueError("public export summary row count is inconsistent")
    if summary.artifact_incomplete_count != sum(
        not row.artifact_complete for row in bundle.rows
    ):
        raise ValueError("public export summary artifact count is inconsistent")
    if summary.model_ids != tuple(sorted({row.model_id for row in bundle.rows})):
        raise ValueError("public export summary model identities are inconsistent")
    if summary.conditions != tuple(sorted({row.condition for row in bundle.rows})):
        raise ValueError("public export summary conditions are inconsistent")


def _validate_report_markdown(report: FormalEvaluationReport, path: Path) -> None:
    try:
        actual = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("public artifacts must be UTF-8") from error
    if actual != render_markdown(report):
        raise ValueError("public report Markdown does not match its JSON")


def _validate_pilot_artifacts(values: dict[str, Any]) -> None:
    checkpoint = values.get("budget-checkpoint.json")
    if checkpoint is not None:
        expected_projection = (
            checkpoint.actual_model_cost_cny
            + checkpoint.remaining_model_cost_upper_cny
            + checkpoint.modal_compute_upper_cny
            + checkpoint.modal_image_build_reserve_cny
        )
        if not math.isclose(
            checkpoint.projected_total_upper_cny,
            expected_projection,
            rel_tol=0,
            abs_tol=1e-9,
        ):
            raise ValueError("public Pilot checkpoint total is inconsistent")

    bundle = values.get("agent-results-bundle.json")
    rows = values.get("pilot-agent-results.json")
    if bundle is not None and rows is not None and bundle.rows != rows:
        raise ValueError("public Pilot rows do not match their result bundle")
    summary = values.get("cleaning.json")
    if bundle is not None and summary is not None:
        _validate_export_summary(bundle, summary)

    budget = values.get("budget.json")
    if budget is not None:
        expected_total = (
            budget.actual_model_cost_cny
            + budget.modal_compute_upper_cny
            + budget.modal_image_build_reserve_cny
        )
        if not math.isclose(
            budget.total_cost_upper_cny,
            expected_total,
            rel_tol=0,
            abs_tol=1e-9,
        ):
            raise ValueError("public Pilot budget total is inconsistent")

    campaign = values.get("campaign-budget.json")
    if campaign is not None:
        expected_campaign = pilot_campaign_budget(
            campaign_budget_cny=campaign.campaign_budget_cny,
            campaign_spend_before_cny=campaign.campaign_spend_before_cny,
            pilot_cost_upper_cny=campaign.pilot_cost_upper_cny,
        )
        if campaign != expected_campaign:
            raise ValueError("public Pilot campaign budget is inconsistent")


def _validate_pilot_stage_artifacts(values: dict[str, Any]) -> None:
    _validate_pilot_artifacts(values)
    stage = values.get("stage.json")
    run_plan = values.get("stage-run-plan.json")
    bundle = values.get("agent-results-bundle.json")
    effect = values.get("effect.json")
    if stage is not None and run_plan is not None:
        if stage.stage_run_plan_sha256 != digest_value(run_plan.model_dump(mode="json")):
            raise ValueError("public Pilot stage run plan is inconsistent")
    if run_plan is not None and bundle is not None:
        if bundle.run_plan_sha256 != digest_value(run_plan.model_dump(mode="json")):
            raise ValueError("public Pilot stage results do not match their run plan")
    if stage is not None and effect is not None:
        if effect.preregistration_sha256 != digest_value(stage.model_dump(mode="json")):
            raise ValueError("public Pilot stage effect is not preregistered")
    if stage is not None and bundle is not None and effect is not None:
        if effect != stage_effect_report(bundle.rows, stage):
            raise ValueError("public Pilot stage effect does not match its result rows")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=sorted(PUBLIC_ARTIFACT_FILES), required=True)
    parser.add_argument("--root", type=Path, required=True)
    arguments = parser.parse_args(argv)
    validate_public_artifacts(arguments.root, arguments.profile)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
