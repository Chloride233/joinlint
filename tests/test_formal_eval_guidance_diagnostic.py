from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("inspect_ai")

from benchmarks.formal_eval.guidance_diagnostic import (  # noqa: E402
    CASES,
    _samples,
    run_guidance_diagnostic,
)
from benchmarks.formal_eval.cli import main  # noqa: E402


def test_guidance_diagnostic_matrix_is_balanced_and_uses_v3_only_in_treatment() -> None:
    samples = _samples()

    assert len(CASES) == 4
    assert len(samples) == 8
    assert sum(sample.metadata["condition"] == "guidance_v3" for sample in samples) == 4
    assert sum(sample.metadata["condition"] == "guidance_removed" for sample in samples) == 4
    for sample in samples:
        if sample.metadata["condition"] == "guidance_v3":
            assert '"schema_version":3' in sample.input
            assert '"guidance"' in sample.input
        else:
            assert '"schema_version":2' in sample.input
            assert '"guidance"' not in sample.input


def test_inspect_mock_guidance_ablation_pipeline(tmp_path: Path) -> None:
    summary = run_guidance_diagnostic(tmp_path / "guidance")

    assert summary.pipeline_passed
    assert summary.total_by_condition == {"guidance_removed": 4, "guidance_v3": 4}
    assert summary.passed_by_condition == {"guidance_removed": 1, "guidance_v3": 4}
    assert (tmp_path / "guidance" / "guidance-diagnostic.json").is_file()


def test_guidance_diagnostic_cli_writes_non_evidentiary_report(tmp_path: Path) -> None:
    output = tmp_path / "cli-guidance"

    assert main(["guidance-smoke", "--output", str(output)]) == 0
    assert (output / "guidance-diagnostic.json").is_file()
