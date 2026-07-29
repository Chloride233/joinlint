from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import uuid
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Literal

import rfc8785
from pydantic import Field, TypeAdapter, model_validator

from benchmarks.formal_eval.bird_dataset import _file_record, verify_bird_subset
from benchmarks.formal_eval.bird_freeze import (
    _database_scale,
    _fanout_type,
    _text_digest,
    _unique_column_sets,
)
from benchmarks.formal_eval.bird_review import _candidate, select_executable_candidates
from benchmarks.formal_eval.contracts import (
    AgentResultBundle,
    FormalManifestV2,
    FormalTask,
    FrozenModel,
    Host,
    InputLockV2,
    ModelPricingCNY,
    SealedAgentTask,
    StrictModel,
)
from benchmarks.formal_eval.lineage import digest_value
from benchmarks.formal_eval.manifest import (
    load_document,
    semantic_fingerprint,
    verify_input_lock,
    verify_sealed_manifest,
)
from benchmarks.formal_eval.run_plan import RunPlanV2, RunSpec, sample_id_for


PILOT_DATASET_RELEASE = "bird-train-2023-07-11-declared-fk-pilot-v1"
PILOT_ALLOCATION = {"citeseer": 8, "genes": 4, "trains": 8}
PILOT_DOMAINS = {"citeseer": "research", "genes": "biology", "trains": "transportation"}
MODAL_CPU_USD_PER_CORE_SECOND = 0.00003942
MODAL_MEMORY_USD_PER_GIB_SECOND = 0.00000667


class PilotRegistration(StrictModel):
    schema_version: Literal[1] = 1
    evaluation_id: str
    dataset_release: str
    seed: int = Field(ge=0)
    task_count: Literal[20] = 20
    models: tuple[FrozenModel, FrozenModel]
    hosts: tuple[Literal["codex"], Literal["claude_code"]] = ("codex", "claude_code")
    host_versions: dict[Host, str]
    joinlint_commit: str
    budget_cny: Literal[20.0] = 20.0
    token_limit_per_run: Literal[20_000] = 20_000
    time_limit_seconds: Literal[90] = 90
    modal_sandbox_timeout_seconds: Literal[120] = 120
    modal_image_builder_version: Literal["2025.06 Stable"] = "2025.06 Stable"
    max_sandboxes: Literal[2] = 2
    cpu_cores: Literal[0.5] = 0.5
    memory_mib: Literal[2048] = 2048
    usd_to_cny_upper: Literal[8.0] = 8.0
    modal_image_build_reserve_cny: Literal[2.0] = 2.0

    @model_validator(mode="after")
    def require_frozen_matrix_and_budget(self) -> PilotRegistration:
        if {model.tier for model in self.models} != {"high_capability", "cost_efficient"}:
            raise ValueError("pilot requires one model from each tier")
        if len({model.id for model in self.models}) != 2:
            raise ValueError("pilot model IDs must be distinct")
        if {model.family for model in self.models} != {"deepseek-v4"}:
            raise ValueError("pilot is frozen to the DeepSeek V4 family")
        if set(self.host_versions) != set(self.hosts):
            raise ValueError("pilot requires one version for each host")
        if len(self.joinlint_commit) != 40 or any(
            character not in "0123456789abcdef" for character in self.joinlint_commit
        ):
            raise ValueError("pilot commit must be one full lowercase Git SHA")
        if budget_envelope(self).total_upper_cny > self.budget_cny:
            raise ValueError("pilot resource envelope exceeds the approved budget")
        return self


class PilotBudgetEnvelope(StrictModel):
    run_count: int
    model_cost_upper_cny: float
    modal_compute_upper_cny: float
    modal_image_build_reserve_cny: float
    total_upper_cny: float


class PilotBudgetReport(StrictModel):
    approved_budget_cny: float
    actual_model_cost_cny: float
    modal_compute_upper_cny: float
    modal_image_build_reserve_cny: float
    total_cost_upper_cny: float
    run_count: int
    expected_run_count: int
    passed: bool


class PilotBudgetCheckpoint(StrictModel):
    approved_budget_cny: float
    completed_batches: int
    total_batches: int
    actual_model_cost_cny: float
    remaining_model_cost_upper_cny: float
    modal_compute_upper_cny: float
    modal_image_build_reserve_cny: float
    projected_total_upper_cny: float
    safe_to_continue: bool


def frozen_pilot_registration(commit: str) -> PilotRegistration:
    observed_at = date(2026, 7, 26)
    return PilotRegistration(
        evaluation_id="joinlint-deepseek-modal-pilot-v1",
        dataset_release=PILOT_DATASET_RELEASE,
        seed=20260727,
        models=(
            FrozenModel(
                id="openai-api/deepseek/deepseek-v4-pro",
                returned_id="openai-api/deepseek/deepseek-v4-pro",
                family="deepseek-v4",
                tier="high_capability",
                thinking="disabled",
                pricing_cny=ModelPricingCNY(
                    observed_at=observed_at,
                    input_cache_hit_per_million_cny=0.025,
                    input_cache_miss_per_million_cny=3.0,
                    output_per_million_cny=6.0,
                ),
            ),
            FrozenModel(
                id="openai-api/deepseek/deepseek-v4-flash",
                returned_id="openai-api/deepseek/deepseek-v4-flash",
                family="deepseek-v4",
                tier="cost_efficient",
                thinking="disabled",
                pricing_cny=ModelPricingCNY(
                    observed_at=observed_at,
                    input_cache_hit_per_million_cny=0.02,
                    input_cache_miss_per_million_cny=1.0,
                    output_per_million_cny=2.0,
                ),
            ),
        ),
        host_versions={"codex": "0.144.1", "claude_code": "2.1.212"},
        joinlint_commit=commit,
    )


def budget_envelope(registration: PilotRegistration) -> PilotBudgetEnvelope:
    model_upper = sum(model_batch_upper_costs(registration))
    runs_per_model = registration.task_count * len(registration.hosts) * 2
    run_count = runs_per_model * len(registration.models)
    modal_usd = run_count * registration.modal_sandbox_timeout_seconds * (
        registration.cpu_cores * MODAL_CPU_USD_PER_CORE_SECOND
        + (registration.memory_mib / 1024) * MODAL_MEMORY_USD_PER_GIB_SECOND
    )
    modal_cny = modal_usd * registration.usd_to_cny_upper
    total = model_upper + modal_cny + registration.modal_image_build_reserve_cny
    return PilotBudgetEnvelope(
        run_count=run_count,
        model_cost_upper_cny=model_upper,
        modal_compute_upper_cny=modal_cny,
        modal_image_build_reserve_cny=registration.modal_image_build_reserve_cny,
        total_upper_cny=total,
    )


def model_batch_upper_costs(registration: PilotRegistration) -> tuple[float, ...]:
    costs: list[float] = []
    for model in registration.models:
        rate = max(
            model.pricing_cny.input_cache_hit_per_million_cny,
            model.pricing_cny.input_cache_miss_per_million_cny,
            model.pricing_cny.output_per_million_cny,
        )
        batch_cost = (
            registration.task_count
            * registration.token_limit_per_run
            * rate
            / 1_000_000
        )
        for _host in registration.hosts:
            for _condition in ("control", "treatment"):
                costs.append(batch_cost)
    return tuple(costs)


def pilot_budget_checkpoint(
    registration: PilotRegistration,
    *,
    completed_batches: int,
    actual_model_cost_cny: float,
) -> PilotBudgetCheckpoint:
    batch_costs = model_batch_upper_costs(registration)
    if completed_batches < 0 or completed_batches > len(batch_costs):
        raise ValueError("completed pilot batch count is outside the frozen matrix")
    if actual_model_cost_cny < 0:
        raise ValueError("observed model cost cannot be negative")
    envelope = budget_envelope(registration)
    remaining_model_upper = sum(batch_costs[completed_batches:])
    projected = (
        actual_model_cost_cny
        + remaining_model_upper
        + envelope.modal_compute_upper_cny
        + envelope.modal_image_build_reserve_cny
    )
    return PilotBudgetCheckpoint(
        approved_budget_cny=registration.budget_cny,
        completed_batches=completed_batches,
        total_batches=len(batch_costs),
        actual_model_cost_cny=actual_model_cost_cny,
        remaining_model_cost_upper_cny=remaining_model_upper,
        modal_compute_upper_cny=envelope.modal_compute_upper_cny,
        modal_image_build_reserve_cny=envelope.modal_image_build_reserve_cny,
        projected_total_upper_cny=projected,
        safe_to_continue=projected <= registration.budget_cny,
    )


def build_pilot_inputs(train_subset: Path, output: Path, *, commit: str) -> dict[str, Any]:
    if output.exists() or output.is_symlink():
        raise ValueError("pilot output already exists")
    source_manifest = verify_bird_subset(train_subset)
    table_rows = {
        row["db_id"]: row
        for row in json.loads((train_subset / "selected-tables.json").read_bytes())
    }
    raw_tasks = json.loads((train_subset / "candidate-tasks.json").read_bytes())
    selected_database_records = {
        record["database_id"]: record for record in source_manifest["selected_databases"]
    }
    candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in raw_tasks:
        database_id = raw.get("db_id")
        if database_id not in PILOT_ALLOCATION:
            continue
        candidate = _candidate(
            raw,
            int(raw["task_index"]),
            "train",
            table_rows[database_id],
        )
        if candidate is not None and candidate["all_edges_declared_fk"]:
            candidates[database_id].append(candidate)

    database_paths = {
        database_id: train_subset / selected_database_records[database_id]["path"]
        for database_id in PILOT_ALLOCATION
    }
    selected: list[dict[str, Any]] = []
    for database_id, count in PILOT_ALLOCATION.items():
        selected.extend(
            select_executable_candidates(
                {database_id: candidates[database_id]},
                database_ids=(database_id,),
                tasks_per_database=count,
                database_paths={database_id: database_paths[database_id]},
            )
        )
    selected.sort(key=lambda task: task["task_id"].encode("utf-8"))
    if len(selected) != 20 or dict(
        Counter(task["database_id"] for task in selected)
    ) != PILOT_ALLOCATION:
        raise ValueError("pilot allocation is not the frozen 8/4/8 selection")
    near_pairs = Counter(
        (task["question_template_id"], task["sql_structure_id"]) for task in selected
    )
    if any(count > 1 for count in near_pairs.values()):
        raise ValueError("pilot selection contains a near duplicate")

    staging = output.with_name(f".{output.name}.staging-{uuid.uuid4().hex}")
    staging.mkdir(parents=True)
    try:
        database_dir = staging / "databases"
        database_dir.mkdir()
        database_records: list[dict[str, Any]] = []
        uniqueness: dict[str, tuple[frozenset[str], ...]] = {}
        for database_id in PILOT_ALLOCATION:
            source = database_paths[database_id]
            destination = database_dir / f"{database_id}.sqlite"
            shutil.copyfile(source, destination)
            record = {"database_id": database_id, "path": f"databases/{destination.name}"}
            record.update(_file_record(destination))
            database_records.append(record)
            uniqueness[database_id] = _unique_column_sets(destination)

        sealed_tasks: list[SealedAgentTask] = []
        manifest_tasks: list[FormalTask] = []
        for raw in selected:
            database_id = raw["database_id"]
            allowed_graphs = tuple(
                tuple((str(edge[0]), str(edge[1])) for edge in graph)
                for graph in raw["proposed_allowed_graphs"]
            )
            task = SealedAgentTask(
                task_id=raw["task_id"],
                database_id=database_id,
                question=raw["question"],
                schema_text=raw["schema_text"],
                schema=raw["schema"],
                sql_shape=raw["sql_shape"],
                gold_sql=raw["gold_sql"],
                database_path=f"databases/{database_id}.sqlite",
                expected_entities=tuple(raw["expected_entities"]),
                allowed_graphs=allowed_graphs,
                oracle_has_safe_path=True,
            )
            record = selected_database_records[database_id]
            manifest_task = FormalTask(
                task_id=task.task_id,
                database_id=database_id,
                database_variant_group=f"bird-pilot-{database_id}",
                corpus="natural",
                split="confirmatory",
                domain=PILOT_DOMAINS[database_id],
                source_type="sqlite",
                database_scale=_database_scale(record["size"]),
                ambiguity="none",
                fanout_type=_fanout_type(allowed_graphs[0], uniqueness[database_id]),
                question_sha256=_text_digest(task.question),
                schema_sha256=_text_digest(task.schema_text),
                sql_shape_sha256=_text_digest(task.sql_shape),
                schema_family_id=semantic_fingerprint("schema", task.schema_text),
                question_template_id=raw["question_template_id"],
                sql_structure_id=raw["sql_structure_id"],
                allowed_graphs=task.allowed_graphs,
                oracle_has_safe_path=True,
                join_depth=raw["join_depth"],
                ambiguous_ground_truth=False,
            )
            sealed_tasks.append(task)
            manifest_tasks.append(manifest_task)

        manifest = FormalManifestV2(
            dataset_release=PILOT_DATASET_RELEASE,
            tasks=tuple(sorted(manifest_tasks, key=lambda task: task.task_id.encode("utf-8"))),
        )
        sealed_tasks.sort(key=lambda task: task.task_id.encode("utf-8"))
        verify_sealed_manifest(manifest, sealed_tasks)
        registration = frozen_pilot_registration(commit)
        envelope = budget_envelope(registration)

        _write_canonical(
            staging / "agent-tasks.json",
            [task.model_dump(mode="json", by_alias=True) for task in sealed_tasks],
        )
        _write_canonical(staging / "manifest.json", manifest.model_dump(mode="json"))
        _write_canonical(staging / "registration.json", registration.model_dump(mode="json"))
        source = {
            "schema_version": 1,
            "dataset_release": PILOT_DATASET_RELEASE,
            "source_subset": source_manifest["source"],
            "allocation": PILOT_ALLOCATION,
            "uses_joinlint_output": False,
            "relationship_scope": "declared_fk_only",
            "databases": database_records,
        }
        _write_canonical(staging / "source-manifest.json", source)
        lock = _input_lock(staging)
        _write_canonical(staging / "input-lock.json", lock.model_dump(mode="json"))
        lineage_id = digest_value(
            {
                "registration": registration.model_dump(mode="json"),
                "manifest": manifest.model_dump(mode="json"),
                "input_lock": lock.model_dump(mode="json"),
            }
        )
        run_plan = build_pilot_run_plan(manifest, registration, lineage_id)
        _write_canonical(staging / "run-plan.json", run_plan.model_dump(mode="json"))
        report = {
            "schema_version": 1,
            "status": "frozen_independent_pilot",
            "lineage_id": lineage_id,
            "task_count": len(sealed_tasks),
            "database_counts": dict(sorted(Counter(task.database_id for task in sealed_tasks).items())),
            "run_count": len(run_plan.runs),
            "budget_envelope": envelope.model_dump(mode="json"),
            "input_lock_sha256": digest_value(lock.model_dump(mode="json")),
        }
        _write_canonical(staging / "pilot-report.json", report)
        os.replace(staging, output)
        return report
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def build_pilot_run_plan(
    manifest: FormalManifestV2,
    registration: PilotRegistration,
    lineage_id: str,
) -> RunPlanV2:
    runs: list[RunSpec] = []
    for task in manifest.tasks:
        for model in registration.models:
            for host in registration.hosts:
                for condition in ("control", "treatment"):
                    runs.append(
                        RunSpec(
                            sample_id=sample_id_for(
                                task_id=task.task_id,
                                database_id=task.database_id,
                                corpus="natural",
                                condition=condition,
                                model_id=model.returned_id,
                                host=host,
                                repetition=0,
                            ),
                            task_id=task.task_id,
                            database_id=task.database_id,
                            corpus="natural",
                            condition=condition,
                            model_id=model.returned_id,
                            host=host,
                            repetition=0,
                            confirmatory=False,
                        )
                    )
    runs.sort(key=lambda run: run.sample_id.encode("utf-8"))
    if len(runs) != 160:
        raise ValueError("pilot run plan must contain exactly 160 runs")
    return RunPlanV2(
        evaluation_id=registration.evaluation_id,
        lineage_id=lineage_id,
        runs=tuple(runs),
        blind_review_sample_ids=(),
    )


def pilot_budget_report(
    registration: PilotRegistration,
    run_plan: RunPlanV2,
    results: AgentResultBundle,
) -> PilotBudgetReport:
    expected_ids = {run.sample_id for run in run_plan.runs}
    actual_ids = {row.sample_id for row in results.rows}
    if actual_ids != expected_ids or results.run_plan_sha256 != digest_value(
        run_plan.model_dump(mode="json")
    ):
        raise ValueError("pilot results do not match the frozen run plan")
    envelope = budget_envelope(registration)
    actual_model_cost = sum(row.calculated_cost_cny for row in results.rows)
    total_upper = (
        actual_model_cost
        + envelope.modal_compute_upper_cny
        + envelope.modal_image_build_reserve_cny
    )
    return PilotBudgetReport(
        approved_budget_cny=registration.budget_cny,
        actual_model_cost_cny=actual_model_cost,
        modal_compute_upper_cny=envelope.modal_compute_upper_cny,
        modal_image_build_reserve_cny=envelope.modal_image_build_reserve_cny,
        total_cost_upper_cny=total_upper,
        run_count=len(results.rows),
        expected_run_count=len(run_plan.runs),
        passed=total_upper <= registration.budget_cny,
    )


def _input_lock(root: Path) -> InputLockV2:
    files = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink() and path.name != "input-lock.json"
    }
    return InputLockV2(files=files)


def verify_pilot_inputs(root: Path) -> tuple[PilotRegistration, FormalManifestV2, RunPlanV2]:
    registration = load_document(root / "registration.json", PilotRegistration)
    manifest = load_document(root / "manifest.json", FormalManifestV2)
    run_plan = load_document(root / "run-plan.json", RunPlanV2)
    lock = load_document(root / "input-lock.json", InputLockV2)
    verify_input_lock(lock, root)
    tasks = TypeAdapter(list[SealedAgentTask]).validate_json(
        (root / "agent-tasks.json").read_bytes()
    )
    verify_sealed_manifest(manifest, tasks)
    if manifest.dataset_release != registration.dataset_release:
        raise ValueError("pilot registration and manifest releases differ")
    expected_lineage = digest_value(
        {
            "registration": registration.model_dump(mode="json"),
            "manifest": manifest.model_dump(mode="json"),
            "input_lock": lock.model_dump(mode="json"),
        }
    )
    expected_plan = build_pilot_run_plan(manifest, registration, expected_lineage)
    if run_plan != expected_plan:
        raise ValueError("pilot run plan is not reproducible")
    return registration, manifest, run_plan


def _write_canonical(path: Path, value: Any) -> None:
    path.write_bytes(rfc8785.dumps(value) + b"\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and verify the independent formal pilot")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--train-subset", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--commit", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--root", type=Path, required=True)
    verify.add_argument("--current-commit")
    arguments = parser.parse_args(argv)
    if arguments.command == "build":
        report = build_pilot_inputs(
            arguments.train_subset,
            arguments.output,
            commit=arguments.commit,
        )
        print(json.dumps(report, sort_keys=True))
        return 0
    registration, _, _ = verify_pilot_inputs(arguments.root)
    if (
        arguments.current_commit is not None
        and registration.joinlint_commit != arguments.current_commit
    ):
        raise ValueError("checked-out JoinLint commit does not match the pilot registration")
    print(json.dumps({"status": "ready", "root": arguments.root.name}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
