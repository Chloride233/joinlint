from __future__ import annotations

import argparse
import json
import random
import shutil
import tempfile
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys_path = Path(__file__).parents[2] / "src"
    import sys

    sys.path.insert(0, str(sys_path))

from joinlint.candidates import discover_candidates
from joinlint.config import add_source
from joinlint.model import ModelV1
from joinlint.scanner import scan_snapshot
from joinlint.snapshots import snapshot_source


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
    shutil.copy2(ROOT / "candidate-manifest.json", output / "candidate-manifest.json")
    shutil.copy2(ROOT / "join-safety-manifest.json", output / "join-safety-manifest.json")


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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, required=True)
    arguments = parser.parse_args()
    generate(arguments.output, arguments.seed)


if __name__ == "__main__":
    main()
