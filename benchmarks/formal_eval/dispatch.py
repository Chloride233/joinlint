from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from benchmarks.formal_eval.contracts import FormalManifestV2, PreregistrationV2
from benchmarks.formal_eval.lineage import EvaluationLineage
from benchmarks.formal_eval.manifest import load_document, validate_manifest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dispatch frozen Inspect jobs to Modal sandboxes")
    parser.add_argument("--preregistration", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--sealed-tasks", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--phase", choices=("confirmatory", "diagnostic"), required=True)
    parser.add_argument("--lineage", type=Path, required=True)
    arguments = parser.parse_args(argv)

    preregistration = load_document(arguments.preregistration, PreregistrationV2)
    manifest = load_document(arguments.manifest, FormalManifestV2)
    lineage = load_document(arguments.lineage, EvaluationLineage)
    readiness = validate_manifest(manifest, preregistration)
    if not readiness.ready:
        raise ValueError(f"formal manifest is not ready: {readiness.findings}")
    inspect = shutil.which("inspect")
    if inspect is None:
        raise ValueError("Inspect CLI is unavailable")
    commands = build_dispatch_commands(
        inspect=inspect,
        preregistration=preregistration,
        manifest=arguments.manifest,
        sealed_tasks=arguments.sealed_tasks,
        log_dir=arguments.log_dir,
        phase=arguments.phase,
        lineage_id=lineage.lineage_id,
    )
    for command in commands:
        Path(command[command.index("--log-dir") + 1]).mkdir(parents=True, exist_ok=True)
        subprocess.run(command, check=True, env=inspect_subprocess_environment())
    return 0


def build_dispatch_commands(
    *,
    inspect: str,
    preregistration: PreregistrationV2,
    manifest: Path,
    sealed_tasks: Path,
    log_dir: Path,
    phase: Literal["confirmatory", "diagnostic"],
    lineage_id: str,
) -> tuple[list[str], ...]:
    resolved_manifest = manifest.resolve()
    resolved_sealed_tasks = sealed_tasks.resolve()
    conditions = (
        ("control", "treatment")
        if phase == "confirmatory"
        else ("oracle_inline", "oracle_mcp", "no_harness")
    )
    epochs = "3" if phase == "confirmatory" else "1"
    commands: list[list[str]] = []
    for model in preregistration.models:
        for host in preregistration.hosts:
            for condition in conditions:
                destination = log_dir / model.tier / host / condition
                commands.append(
                    [
                        inspect,
                        "eval",
                        "benchmarks/formal_eval/inspect_task.py@formal_agent_eval",
                        "--model",
                        model.id,
                        "--log-dir",
                        str(destination),
                        "--epochs",
                        epochs,
                        "--max-retries",
                        "2",
                        "--time-limit",
                        "120",
                        "--max-sandboxes",
                        "10",
                        "--no-fail-on-error",
                        "--score-on-error",
                        "--display",
                        "none",
                        "--seed",
                        str(preregistration.seed),
                        "-T",
                        f"sealed_tasks={resolved_sealed_tasks}",
                        "-T",
                        f"manifest={resolved_manifest}",
                        "-T",
                        f"host={host}",
                        "-T",
                        f"condition={condition}",
                        "-T",
                        f"agent_version={preregistration.host_versions[host]}",
                        "-T",
                        f"image_reference={preregistration.image_reference}",
                        "-T",
                        f"lineage_id={lineage_id}",
                    ]
                )
    return tuple(commands)


def inspect_subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(REPOSITORY_ROOT) + (
        f"{os.pathsep}{existing}" if existing else ""
    )
    return environment


if __name__ == "__main__":
    raise SystemExit(main())
