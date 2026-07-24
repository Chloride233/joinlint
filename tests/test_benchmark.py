from __future__ import annotations

from pathlib import Path

from benchmarks.joinsafety.generate import evaluate_candidate_manifest, load_manifest


ROOT = Path(__file__).parents[1] / "benchmarks" / "joinsafety"


def test_frozen_manifest_has_required_case_counts() -> None:
    candidates = load_manifest(ROOT / "candidate-manifest.json")
    safety = load_manifest(ROOT / "join-safety-manifest.json")

    assert sum(case["label"] == "positive" for case in candidates["cases"]) == 12
    assert sum(case["label"] == "negative" for case in candidates["cases"]) == 12
    assert sum(case["kind"] == "blocking" for case in safety["cases"]) == 16
    assert sum(case["kind"] == "benign" for case in safety["cases"]) == 8


def test_candidate_gate_and_safety_cases_pass() -> None:
    candidates = load_manifest(ROOT / "candidate-manifest.json")
    safety = load_manifest(ROOT / "join-safety-manifest.json")

    metrics = evaluate_candidate_manifest(candidates)

    assert metrics["precision"] >= 0.85
    assert metrics["recall"] >= 0.80
    assert all(case["expected_code"] for case in safety["cases"] if case["kind"] == "blocking")
    assert all(case["expected_code"] is None for case in safety["cases"] if case["kind"] == "benign")
