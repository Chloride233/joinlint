from __future__ import annotations

import argparse
import json
import random
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

if __package__ in {None, ""}:
    sys_path = Path(__file__).parents[2] / "src"
    import sys

    sys.path.insert(0, str(sys_path))

from joinlint.candidates import discover_candidates
from joinlint.baseline import compare_baseline
from joinlint.config import add_source
from joinlint.model import ModelV1
from joinlint.scanner import scan_snapshot
from joinlint.snapshots import snapshot_source
from joinlint.validation import validate_path, validate_relationship
from joinlint.model import Relationship


ROOT = Path(__file__).parent


def load_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def evaluate_candidate_manifest(manifest: dict[str, Any]) -> dict[str, float]:
    true_positive = false_positive = false_negative = 0
    for case in manifest["cases"]:
        predicted = _candidate_exists(case)
        positive = case["label"] == "positive"
        if predicted and positive:
            true_positive += 1
        elif predicted:
            false_positive += 1
        elif positive:
            false_negative += 1
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    return {"precision": precision, "recall": recall}


def generate(output: Path, seed: int) -> None:
    rng = random.Random(seed)
    manifest = load_manifest(ROOT / "candidate-manifest.json")
    output.mkdir(parents=True, exist_ok=True)
    for case in manifest["cases"]:
        directory = output / case["id"]
        directory.mkdir(exist_ok=True)
        _write_csv(directory / "children.csv", "child_key", rng.sample(case["child_values"], len(case["child_values"])))
        _write_csv(directory / "parents.csv", "parent_key", rng.sample(case["parent_values"], len(case["parent_values"])))
    safety_manifest = load_manifest(ROOT / "join-safety-manifest.json")
    for case in safety_manifest["cases"]:
        _materialize_safety_case(output / "join-safety" / case["id"], case["recipe"])
    shutil.copy2(ROOT / "candidate-manifest.json", output / "candidate-manifest.json")
    shutil.copy2(ROOT / "join-safety-manifest.json", output / "join-safety-manifest.json")


def evaluate_join_safety_manifest(manifest: dict[str, Any]) -> list[dict[str, object]]:
    """Run every frozen case through the scanner, validator, or drift checker."""
    return [
        {"id": case["id"], "codes": _run_safety_case(case["recipe"])}
        for case in manifest["cases"]
    ]


def _candidate_exists(case: dict[str, Any]) -> bool:
    with tempfile.TemporaryDirectory(prefix="joinlint-benchmark-") as temporary:
        project = Path(temporary)
        data = project / "data"
        data.mkdir()
        _write_csv(data / "children.csv", "child_key", case["child_values"])
        _write_csv(data / "parents.csv", "parent_key", case["parent_values"])
        (project / ".joinlint").mkdir()
        (project / ".joinlint" / "config.yaml").write_text("version: 1\nsources: {}\n", encoding="utf-8")
        (project / ".joinlint" / "model.yaml").write_text(
            "version: 1\nentities: {}\nrelationships: []\n", encoding="utf-8"
        )
        add_source(project, "fixture", "data", "csv_directory")
        with snapshot_source(project, "fixture") as snapshot:
            catalog = scan_snapshot(snapshot)
            candidates = discover_candidates(snapshot, catalog, ModelV1(version=1, entities={}, relationships=[]))
        return any(
            candidate.from_endpoint == "children.child_key" and candidate.to_endpoint == "parents.parent_key"
            for candidate in candidates
        )


def _write_csv(path: Path, header: str, values: list[str]) -> None:
    path.write_text(header + "\n" + "\n".join(values) + "\n", encoding="utf-8")


def _run_safety_case(recipe: str) -> list[str]:
    with tempfile.TemporaryDirectory(prefix="joinlint-safety-") as temporary:
        project = Path(temporary)
        data = project / "data"
        _materialize_safety_case(data, recipe)
        (project / ".joinlint").mkdir()
        (project / ".joinlint" / "config.yaml").write_text("version: 1\nsources: {}\n", encoding="utf-8")
        add_source(project, "fixture", "data", "csv_directory")
        if recipe == "schema_drift":
            return _schema_drift_codes(project, data)
        with snapshot_source(project, "fixture") as snapshot:
            catalog = scan_snapshot(snapshot)
            if recipe == "compound_fanout":
                findings = validate_path(_compound_relationships(), snapshot, catalog).findings
            else:
                findings = validate_relationship(_relationship_for_recipe(recipe), snapshot, catalog).findings
    return [finding.code for finding in findings]


def _materialize_safety_case(directory: Path, recipe: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if recipe == "compound_fanout":
        (directory / "children.csv").write_text("id,parent_id\n1,a\n2,a\n3,b\n", encoding="utf-8")
        (directory / "parents.csv").write_text("id,grand_id\na,g1\nb,g1\n", encoding="utf-8")
        (directory / "grands.csv").write_text("id\ng1\n", encoding="utf-8")
        return
    if recipe == "schema_drift":
        (directory / "left.csv").write_text("id,key\n1,a\n2,b\n", encoding="utf-8")
        (directory / "right.csv").write_text("key\na\nb\n", encoding="utf-8")
        return
    if recipe == "null_child":
        (directory / "left.csv").write_text('key\na\n""\nb\n', encoding="utf-8")
        (directory / "right.csv").write_text("key\na\nb\n", encoding="utf-8")
        return
    left_values, right_values = {
        "duplicate_parent": (["a", "b"], ["a", "a", "b"]),
        "orphan_child": (["a", "b", "missing"], ["a", "b"]),
        "cardinality_drift": (["a", "a", "b"], ["a", "b"]),
        "many_to_many": (["a", "a"], ["a", "a"]),
        "safe_one_to_one": (["a", "b"], ["a", "b"]),
        "safe_many_to_one": (["a", "a", "b"], ["a", "b"]),
    }[recipe]
    _write_csv(directory / "left.csv", "key", left_values)
    _write_csv(directory / "right.csv", "key", right_values)


def _relationship_for_recipe(recipe: str) -> Relationship:
    cardinality = {
        "duplicate_parent": "one_to_many",
        "null_child": "one_to_one",
        "orphan_child": "one_to_one",
        "cardinality_drift": "one_to_one",
        "many_to_many": "many_to_many",
        "safe_one_to_one": "one_to_one",
        "safe_many_to_one": "many_to_one",
    }[recipe]
    return Relationship(
        id="left_to_right",
        from_="left.key",
        to="right.key",
        cardinality=cardinality,
        status="confirmed",
    )


def _compound_relationships() -> list[Relationship]:
    return [
        Relationship(id="child_to_parent", from_="children.parent_id", to="parents.id", cardinality="many_to_one", status="confirmed"),
        Relationship(id="parent_to_grand", from_="parents.grand_id", to="grands.id", cardinality="many_to_one", status="confirmed"),
    ]


def _schema_drift_codes(project: Path, data: Path) -> list[str]:
    with snapshot_source(project, "fixture") as snapshot:
        initial = scan_snapshot(snapshot)
    baseline = {"schemas": [_catalog_document(initial)], "relationship_results": []}
    (data / "left.csv").write_text("id,key,extra\n1,a,1\n2,b,2\n", encoding="utf-8")
    with snapshot_source(project, "fixture") as snapshot:
        changed = scan_snapshot(snapshot)
    findings = compare_baseline(
        baseline,
        SimpleNamespace(schemas=[_catalog_document(changed)], relationship_results=[]),
    )
    return [finding.code for finding in findings]


def _catalog_document(catalog: object) -> dict[str, object]:
    return {"source_id": catalog.source_id, "tables": [asdict(table) for table in catalog.tables]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, required=True)
    arguments = parser.parse_args()
    generate(arguments.output, arguments.seed)


if __name__ == "__main__":
    main()
