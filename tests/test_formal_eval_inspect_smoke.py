from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("inspect_ai")

from benchmarks.formal_eval.inspect_smoke import run_inspect_smoke


def test_inspect_mock_completes_current_two_tool_flow(tmp_path: Path) -> None:
    summary = run_inspect_smoke(
        Path("tests/fixtures/chinook"),
        tmp_path / "inspect-smoke",
    )

    assert summary == {
        "schema_version": 2,
        "evidence_class": "synthetic_non_evidentiary_inspect_smoke",
        "inspect_sample_count": 1,
        "passed": True,
        "model": "mockllm/model",
        "tools": ["get_join_plan", "validate_sql"],
    }
