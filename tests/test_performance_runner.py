from __future__ import annotations

import json
from pathlib import Path

from scripts import run_benchmark
from scripts.run_benchmark import run_performance_fixture


def test_benchmark_result_has_runtime_and_peak_rss(tmp_path: Path) -> None:
    output = tmp_path / "result.json"

    result = run_performance_fixture(rows=1_000, output=output)

    assert result["rows"] == 1_000
    assert result["elapsed_seconds"] >= 0
    assert result["peak_rss_bytes"] > 0
    assert json.loads(output.read_text(encoding="utf-8")) == result


def test_benchmark_runs_validation_after_scan(tmp_path: Path, monkeypatch) -> None:
    called = False
    original_validate = run_benchmark.validate_project

    def validate(project: Path):
        nonlocal called
        called = True
        return original_validate(project)

    monkeypatch.setattr(run_benchmark, "validate_project", validate)

    run_performance_fixture(rows=10, output=tmp_path / "result.json")

    assert called
