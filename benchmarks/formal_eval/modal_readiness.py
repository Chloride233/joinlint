from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from benchmarks.formal_eval.contracts import Host, StrictModel
from benchmarks.formal_eval.pilot_canary import dependency_versions
from joinlint.contracts import canonical_json


MODAL_READINESS_BUDGET_CNY = 2.10
MODAL_READINESS_COMPUTE_UPPER_CNY = 0.063456
MODAL_IMAGE_BUILD_RESERVE_CNY = 2.0
HOST_VERSIONS: dict[Host, str] = {
    "codex": "0.144.1",
    "claude_code": "2.1.212",
}


class ModalReadinessCell(StrictModel):
    host: Host
    agent_version: str
    host_binary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    infrastructure_preparation_duration_seconds: float = Field(ge=0)
    tool_names: tuple[str, ...]
    provider_short_circuited: Literal[True] = True

    @model_validator(mode="after")
    def require_canonical_tool_surface(self) -> ModalReadinessCell:
        if self.tool_names != tuple(sorted(set(self.tool_names))):
            raise ValueError("Modal readiness tool names must be unique and sorted")
        return self


class ModalReadinessReport(StrictModel):
    schema_version: Literal[2] = 2
    status: Literal["passed"] = "passed"
    workflow_run_id: int = Field(gt=0)
    joinlint_commit: str
    modal_image_builder_version: Literal["2025.06 Stable"] = "2025.06 Stable"
    cells: tuple[ModalReadinessCell, ModalReadinessCell]
    provider_token_count: Literal[0] = 0
    modal_compute_upper_cny: Literal[0.063456] = MODAL_READINESS_COMPUTE_UPPER_CNY
    modal_image_build_reserve_cny: Literal[2.0] = MODAL_IMAGE_BUILD_RESERVE_CNY
    total_cost_upper_cny: Literal[2.063456] = 2.063456
    dependency_versions: dict[str, str]

    @model_validator(mode="after")
    def require_frozen_hosts(self) -> ModalReadinessReport:
        if tuple(cell.host for cell in self.cells) != ("claude_code", "codex"):
            raise ValueError("Modal readiness cells must cover both frozen hosts")
        if any(cell.agent_version != HOST_VERSIONS[cell.host] for cell in self.cells):
            raise ValueError("Modal readiness host version mismatch")
        return self


def verify_modal_readiness(
    log_dir: Path,
    output: Path,
    *,
    workflow_run_id: int,
    joinlint_commit: str,
) -> ModalReadinessReport:
    from inspect_ai.log import list_eval_logs, read_eval_log

    infos = list_eval_logs(str(log_dir), recursive=True)
    if len(infos) != 2:
        raise RuntimeError("Modal readiness did not produce exactly two Inspect logs")
    samples = [
        sample
        for info in infos
        for sample in (read_eval_log(info.name, header_only=False).samples or [])
    ]
    if len(samples) != 2:
        raise RuntimeError("Modal readiness did not produce exactly two samples")
    cells: list[ModalReadinessCell] = []
    provider_tokens = 0
    for sample in samples:
        if sample.error is not None:
            raise RuntimeError("Modal readiness sample has a runtime error")
        for usage in sample.model_usage.values():
            provider_tokens += (
                (usage.input_tokens or 0)
                + (usage.input_tokens_cache_read or 0)
                + (usage.input_tokens_cache_write or 0)
                + (usage.output_tokens or 0)
            )
        score = (sample.scores or {}).get("formal_host_context_scorer")
        metadata = (
            score.metadata
            if score is not None and isinstance(score.metadata, dict)
            else {}
        )
        if (
            score is None
            or score.value != 1
            or metadata.get("readiness_attested") is not True
            or metadata.get("provider_short_circuited") is not True
        ):
            raise RuntimeError("Modal host context was not attested")
        digest = metadata.get("host_binary_sha256")
        duration = metadata.get("infrastructure_preparation_duration_seconds")
        tool_names = metadata.get("tool_names")
        if not isinstance(digest, str) or len(digest) != 64:
            raise RuntimeError("Modal readiness host digest is missing")
        if not isinstance(duration, int | float) or duration < 0:
            raise RuntimeError("Modal readiness duration is missing")
        if not isinstance(tool_names, list | tuple) or not all(
            isinstance(name, str) and name for name in tool_names
        ):
            raise RuntimeError("Modal readiness tool surface is missing")
        cells.append(
            ModalReadinessCell(
                host=metadata.get("host"),
                agent_version=metadata.get("agent_version"),
                host_binary_sha256=digest,
                infrastructure_preparation_duration_seconds=float(duration),
                tool_names=tuple(tool_names),
                provider_short_circuited=True,
            )
        )
    if provider_tokens != 0:
        raise RuntimeError("Modal readiness unexpectedly consumed provider tokens")
    cells.sort(key=lambda cell: cell.host)
    report = ModalReadinessReport(
        workflow_run_id=workflow_run_id,
        joinlint_commit=joinlint_commit,
        cells=tuple(cells),  # type: ignore[arg-type]
        provider_token_count=0,
        dependency_versions=dependency_versions(),
    )
    output.mkdir(parents=True, exist_ok=True)
    (output / "modal-readiness.json").write_bytes(
        canonical_json(report.model_dump(mode="json")) + b"\n"
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify two no-model host-context runs")
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workflow-run-id", type=int, required=True)
    parser.add_argument("--joinlint-commit", required=True)
    arguments = parser.parse_args(argv)
    report = verify_modal_readiness(
        arguments.log_dir,
        arguments.output,
        workflow_run_id=arguments.workflow_run_id,
        joinlint_commit=arguments.joinlint_commit,
    )
    print(json.dumps(report.model_dump(mode="json"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
