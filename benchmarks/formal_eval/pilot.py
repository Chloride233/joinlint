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
from typing import Any, Callable, Literal

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
    require_locked_inputs,
    semantic_fingerprint,
    verify_input_lock,
    verify_sealed_manifest,
)
from benchmarks.formal_eval.run_plan import RunPlanV2, RunSpec, sample_id_for


PILOT_DATASET_RELEASE = "bird-train-2023-07-11-declared-fk-pilot-v4"
CONTRACT_SAFETY_DATASET_RELEASE = "semantic-join-contract-safety-pilot-v1"
CONTRACT_AMBIGUITY_DATASET_RELEASE = "semantic-join-contract-ambiguity-pilot-v1"
PILOT_ALLOCATION = {"citeseer": 9, "trains": 11}
PILOT_DOMAINS = {"citeseer": "research", "trains": "transportation"}
PILOT_GROUND_TRUTH_EXCLUSIONS = {
    "bird-train-citeseer-04142": "question_requests_other_paper_but_gold_returns_source_paper_words",
    "bird-train-citeseer-04143": "question_ambiguously_invokes_cites_table_but_gold_uses_content_only",
    "bird-train-citeseer-04150": "question_requires_class_intersection_but_gold_uses_union",
    "bird-train-trains-00698": "question_requests_west_but_gold_filters_east",
}
MODAL_CPU_USD_PER_CORE_SECOND = 0.00003942
MODAL_MEMORY_USD_PER_GIB_SECOND = 0.00000667
PILOT_ASSIGNMENT_DESIGN = "balanced_diagonal_crossover_v1"


class PilotRegistration(StrictModel):
    schema_version: Literal[3, 4, 5, 6, 7, 8]
    evaluation_id: str
    dataset_release: str
    seed: int = Field(ge=0)
    task_count: Literal[20] = 20
    models: tuple[FrozenModel, FrozenModel]
    hosts: tuple[Literal["codex"], Literal["claude_code"]] = ("codex", "claude_code")
    host_versions: dict[Host, str]
    joinlint_commit: str
    budget_cny: Literal[20.0, 22.0] = 20.0
    assignment_design: Literal["balanced_diagonal_crossover_v1"] = (
        "balanced_diagonal_crossover_v1"
    )
    token_limit_per_run: Literal[35_000, 45_000] = 35_000
    token_accounting_ceiling_per_run: int | None = None
    token_limit_type: Literal["(input*0.5)+output"] = "(input*0.5)+output"
    message_limit_per_run: Literal[20, 30] = 20
    time_limit_seconds: Literal[90] = 90
    modal_sandbox_timeout_seconds: Literal[150, 170]
    modal_image_builder_version: Literal["2025.06 Stable"] = "2025.06 Stable"
    max_sandboxes: Literal[2] = 2
    cpu_cores: Literal[0.5] = 0.5
    memory_mib: Literal[2048] = 2048
    usd_to_cny_upper: Literal[8.0] = 8.0
    modal_image_build_reserve_cny: Literal[2.0] = 2.0

    @model_validator(mode="before")
    @classmethod
    def default_legacy_sandbox_timeout(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        value = {
            **value,
            **(
                {
                    "models": tuple(
                        FrozenModel.model_validate_json(json.dumps(model))
                        for model in value["models"]
                    )
                }
                if isinstance(value.get("models"), list)
                else {}
            ),
            **(
                {"hosts": tuple(value["hosts"])}
                if isinstance(value.get("hosts"), list)
                else {}
            ),
        }
        if (
            value.get("schema_version") in (3, 4)
            and "modal_sandbox_timeout_seconds" not in value
        ):
            return {**value, "modal_sandbox_timeout_seconds": 150}
        if (
            value.get("schema_version") in (5, 6, 7, 8)
            and "modal_sandbox_timeout_seconds" not in value
        ):
            raise ValueError(
                "current pilot registration must explicitly declare its sandbox timeout"
            )
        return value

    @model_validator(mode="after")
    def require_frozen_matrix_and_budget(self) -> PilotRegistration:
        if self.schema_version == 8 and (
            self.evaluation_id != "joinlint-semantic-contract-ambiguity-pilot-v1"
            or self.dataset_release != CONTRACT_AMBIGUITY_DATASET_RELEASE
            or self.token_limit_per_run != 45_000
            or self.token_accounting_ceiling_per_run != 50_000
            or self.message_limit_per_run != 30
            or self.budget_cny != 22.0
        ):
            raise ValueError(
                "contract ambiguity Pilot resource limits do not match schema version 8"
            )
        if self.schema_version == 7 and (
            self.evaluation_id != "joinlint-semantic-contract-safety-pilot-v1"
            or self.dataset_release != CONTRACT_SAFETY_DATASET_RELEASE
            or self.token_limit_per_run != 45_000
            or self.token_accounting_ceiling_per_run != 50_000
            or self.message_limit_per_run != 30
            or self.budget_cny != 22.0
        ):
            raise ValueError(
                "contract safety Pilot resource limits do not match schema version 7"
            )
        if self.schema_version == 6 and (
            self.evaluation_id != "joinlint-semantic-safety-pilot-v1"
            or self.dataset_release != "semantic-join-safety-pilot-v1"
            or self.token_limit_per_run != 45_000
            or self.token_accounting_ceiling_per_run != 50_000
            or self.message_limit_per_run != 30
            or self.budget_cny != 22.0
        ):
            raise ValueError("safety Pilot resource limits do not match schema version 6")
        if self.schema_version in (4, 5) and (
            self.token_limit_per_run != 35_000
            or self.token_accounting_ceiling_per_run != 45_000
            or self.message_limit_per_run != 20
            or self.budget_cny != 20.0
        ):
            raise ValueError("Pilot resource limits do not match its schema version")
        if self.schema_version == 3 and self.token_accounting_ceiling_per_run is not None:
            raise ValueError("legacy pilot registration cannot define an accounting ceiling")
        if (
            self.schema_version >= 5
            and self.modal_sandbox_timeout_seconds != 170
        ) or (
            self.schema_version < 5
            and self.modal_sandbox_timeout_seconds != 150
        ):
            raise ValueError("pilot sandbox timeout does not match its schema version")
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


class PilotCalibrationSpec(StrictModel):
    schema_version: Literal[2] = 2
    task_ids: tuple[str, str]
    selection_rule: Literal["highest_join_depth_distinct_database_then_task_id_v2"] = (
        "highest_join_depth_distinct_database_then_task_id_v2"
    )

    @model_validator(mode="after")
    def require_two_distinct_tasks(self) -> PilotCalibrationSpec:
        if len(set(self.task_ids)) != 2:
            raise ValueError("pilot calibration requires two distinct task IDs")
        return self


def frozen_pilot_registration(commit: str) -> PilotRegistration:
    observed_at = date(2026, 7, 26)
    return PilotRegistration(
        schema_version=5,
        evaluation_id="joinlint-deepseek-modal-pilot-v4",
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
        assignment_design=PILOT_ASSIGNMENT_DESIGN,
        token_accounting_ceiling_per_run=45_000,
        modal_sandbox_timeout_seconds=170,
    )


def frozen_safety_pilot_registration(commit: str) -> PilotRegistration:
    values = frozen_pilot_registration(commit).model_dump(mode="json")
    return PilotRegistration.model_validate(
        {
            **values,
            "schema_version": 6,
            "evaluation_id": "joinlint-semantic-safety-pilot-v1",
            "dataset_release": "semantic-join-safety-pilot-v1",
            "token_limit_per_run": 45_000,
            "token_accounting_ceiling_per_run": 50_000,
            "message_limit_per_run": 30,
            "budget_cny": 22.0,
        }
    )


def frozen_contract_safety_pilot_registration(commit: str) -> PilotRegistration:
    values = frozen_safety_pilot_registration(commit).model_dump(mode="json")
    return PilotRegistration.model_validate(
        {
            **values,
            "schema_version": 7,
            "evaluation_id": "joinlint-semantic-contract-safety-pilot-v1",
            "dataset_release": CONTRACT_SAFETY_DATASET_RELEASE,
        }
    )


def frozen_contract_ambiguity_pilot_registration(commit: str) -> PilotRegistration:
    values = frozen_contract_safety_pilot_registration(commit).model_dump(mode="json")
    return PilotRegistration.model_validate(
        {
            **values,
            "schema_version": 8,
            "evaluation_id": "joinlint-semantic-contract-ambiguity-pilot-v1",
            "dataset_release": CONTRACT_AMBIGUITY_DATASET_RELEASE,
        }
    )


def budget_envelope(registration: PilotRegistration) -> PilotBudgetEnvelope:
    model_upper = sum(model_batch_upper_costs(registration))
    runs_per_model = registration.task_count * 2
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
    tasks_per_batch = registration.task_count // len(registration.hosts)
    for model in registration.models:
        weighted_unit_rate = max(
            model.pricing_cny.input_cache_hit_per_million_cny / 0.5,
            model.pricing_cny.input_cache_miss_per_million_cny / 0.5,
            model.pricing_cny.output_per_million_cny,
        )
        batch_cost = (
            tasks_per_batch
            * _token_accounting_ceiling(registration)
            * weighted_unit_rate
            / 1_000_000
        )
        for _host in registration.hosts:
            for _condition in ("control", "treatment"):
                costs.append(batch_cost)
    return tuple(costs)


def _token_accounting_ceiling(registration: PilotRegistration) -> int:
    return registration.token_accounting_ceiling_per_run or registration.token_limit_per_run


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
    observed_ground_truth_exclusions: set[str] = set()
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
        if candidate is not None and candidate["task_id"] in PILOT_GROUND_TRUTH_EXCLUSIONS:
            observed_ground_truth_exclusions.add(candidate["task_id"])
            continue
        if candidate is not None and candidate["all_edges_declared_fk"]:
            candidates[database_id].append(candidate)
    if observed_ground_truth_exclusions != set(PILOT_GROUND_TRUTH_EXCLUSIONS):
        raise ValueError("pilot ground-truth exclusions do not match the frozen source")

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
                require_grain_provable=True,
            )
        )
    selected.sort(key=lambda task: task["task_id"].encode("utf-8"))
    if len(selected) != 20 or dict(
        Counter(task["database_id"] for task in selected)
    ) != PILOT_ALLOCATION:
        raise ValueError("pilot allocation is not the frozen 9/11 selection")
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
        calibration = build_pilot_calibration_spec(manifest)
        registration = frozen_pilot_registration(commit)
        envelope = budget_envelope(registration)

        _write_canonical(
            staging / "agent-tasks.json",
            [task.model_dump(mode="json", by_alias=True) for task in sealed_tasks],
        )
        _write_canonical(staging / "manifest.json", manifest.model_dump(mode="json"))
        _write_canonical(staging / "calibration.json", calibration.model_dump(mode="json"))
        _write_canonical(staging / "registration.json", registration.model_dump(mode="json"))
        source = {
            "schema_version": 1,
            "dataset_release": PILOT_DATASET_RELEASE,
            "source_subset": source_manifest["source"],
            "allocation": PILOT_ALLOCATION,
            "uses_joinlint_output": False,
            "relationship_scope": "declared_fk_only",
            "ground_truth_exclusions": dict(sorted(PILOT_GROUND_TRUTH_EXCLUSIONS.items())),
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


def build_safety_pilot_inputs(output: Path, *, commit: str) -> dict[str, Any]:
    from benchmarks.formal_eval.semantic_failure import build_semantic_failure_v1

    return _build_safety_pilot_inputs(
        output,
        commit=commit,
        source_builder=build_semantic_failure_v1,
        registration=frozen_safety_pilot_registration(commit),
        dataset_release="semantic-join-safety-pilot-v1",
        status="frozen_independent_safety_pilot",
        claim_boundary="synthetic_join_safety_stress_only",
    )


def build_contract_safety_pilot_inputs(output: Path, *, commit: str) -> dict[str, Any]:
    from benchmarks.formal_eval.semantic_contract_failure import (
        build_semantic_contract_failure_v1,
    )

    return _build_safety_pilot_inputs(
        output,
        commit=commit,
        source_builder=build_semantic_contract_failure_v1,
        registration=frozen_contract_safety_pilot_registration(commit),
        dataset_release=CONTRACT_SAFETY_DATASET_RELEASE,
        status="frozen_independent_contract_safety_pilot",
        claim_boundary="trusted_query_contract_join_safety_stress_only",
    )


def build_contract_ambiguity_pilot_inputs(output: Path, *, commit: str) -> dict[str, Any]:
    from benchmarks.formal_eval.semantic_contract_ambiguity import (
        build_semantic_contract_ambiguity_v1,
    )

    return _build_safety_pilot_inputs(
        output,
        commit=commit,
        source_builder=build_semantic_contract_ambiguity_v1,
        registration=frozen_contract_ambiguity_pilot_registration(commit),
        dataset_release=CONTRACT_AMBIGUITY_DATASET_RELEASE,
        status="frozen_independent_contract_ambiguity_pilot",
        claim_boundary="trusted_query_contract_opaque_relationship_stress_only",
    )


def _build_safety_pilot_inputs(
    output: Path,
    *,
    commit: str,
    source_builder: Callable[[Path, Path], dict[str, Any]],
    registration: PilotRegistration,
    dataset_release: str,
    status: str,
    claim_boundary: str,
) -> dict[str, Any]:

    if output.exists() or output.is_symlink():
        raise ValueError("safety Pilot output already exists")
    parent = output.parent.resolve(strict=True)
    source = parent / f".semantic-safety-source-{uuid.uuid4().hex}"
    staging = parent / f".{output.name}.staging-{uuid.uuid4().hex}"
    try:
        source_report = source_builder(parent, source)
        source_tasks = TypeAdapter(list[SealedAgentTask]).validate_json(
            (source / "agent-tasks.json").read_bytes()
        )
        source_manifest = load_document(source / "manifest.json", FormalManifestV2)
        tasks = tuple(
            task.model_copy(update={"database_path": f"databases/{task.database_id}.sqlite"})
            for task in source_tasks
        )
        manifest = source_manifest.model_copy(
            update={
                "dataset_release": dataset_release,
                "tasks": tuple(
                    task.model_copy(update={"split": "confirmatory"})
                    for task in source_manifest.tasks
                ),
            }
        )
        verify_sealed_manifest(manifest, tasks)
        if registration.joinlint_commit != commit:
            raise ValueError("safety Pilot registration commit mismatch")
        calibration = build_pilot_calibration_spec(manifest)

        staging.mkdir()
        shutil.copytree(source / "databases", staging / "databases")
        _write_canonical(
            staging / "agent-tasks.json",
            [task.model_dump(mode="json", by_alias=True) for task in tasks],
        )
        _write_canonical(staging / "manifest.json", manifest.model_dump(mode="json"))
        _write_canonical(staging / "calibration.json", calibration.model_dump(mode="json"))
        _write_canonical(staging / "registration.json", registration.model_dump(mode="json"))
        _write_canonical(
            staging / "source-manifest.json",
            {
                **source_report,
                "status": status,
                "dataset_release": manifest.dataset_release,
                "claim_boundary": claim_boundary,
            },
        )
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
            "status": status,
            "claim_boundary": claim_boundary,
            "lineage_id": lineage_id,
            "task_count": len(tasks),
            "run_count": len(run_plan.runs),
            "input_lock_sha256": digest_value(lock.model_dump(mode="json")),
        }
        _write_canonical(staging / "pilot-report.json", report)
        os.replace(staging, output)
        return report
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(source, ignore_errors=True)


def build_pilot_run_plan(
    manifest: FormalManifestV2,
    registration: PilotRegistration,
    lineage_id: str,
) -> RunPlanV2:
    runs: list[RunSpec] = []
    for model in registration.models:
        for host in registration.hosts:
            partition = pilot_partition_for(model.tier, host)
            for task in pilot_partition_tasks(manifest, partition):
                for condition in ("control", "treatment"):
                    runs.append(
                        RunSpec(
                            sample_id=sample_id_for(
                                task_id=task.task_id,
                                database_id=task.database_id,
                                corpus=task.corpus,
                                condition=condition,
                                model_id=model.returned_id,
                                host=host,
                                repetition=0,
                            ),
                            task_id=task.task_id,
                            database_id=task.database_id,
                            corpus=task.corpus,
                            condition=condition,
                            model_id=model.returned_id,
                            host=host,
                            repetition=0,
                            confirmatory=False,
                        )
                    )
    runs.sort(key=lambda run: run.sample_id.encode("utf-8"))
    if len(runs) != 80:
        raise ValueError("pilot run plan must contain exactly 80 runs")
    run_counts = Counter(run.task_id for run in runs)
    if set(run_counts.values()) != {4}:
        raise ValueError("each pilot task must have four paired crossover runs")
    return RunPlanV2(
        evaluation_id=registration.evaluation_id,
        lineage_id=lineage_id,
        runs=tuple(runs),
        blind_review_sample_ids=(),
    )


def pilot_partition_for(
    model_tier: str,
    host: Host,
) -> Literal["even", "odd"]:
    if model_tier not in {"high_capability", "cost_efficient"}:
        raise ValueError("unsupported pilot model tier")
    diagonal = (model_tier == "high_capability") == (host == "codex")
    return "even" if diagonal else "odd"


def pilot_partition_tasks(
    manifest: FormalManifestV2,
    partition: Literal["even", "odd"],
) -> tuple[FormalTask, ...]:
    ordered = sorted(
        manifest.tasks,
        key=lambda task: (task.database_id.encode("utf-8"), task.task_id.encode("utf-8")),
    )
    offset = 0 if partition == "even" else 1
    selected = tuple(ordered[offset::2])
    expected = (len(ordered) + (1 if partition == "even" else 0)) // 2
    if len(selected) != expected:
        raise ValueError("pilot crossover partition is not reproducible")
    return selected


def build_pilot_calibration_spec(manifest: FormalManifestV2) -> PilotCalibrationSpec:
    ranked = sorted(
        manifest.tasks,
        key=lambda task: (-task.join_depth, task.task_id.encode("utf-8")),
    )
    selected: list[str] = []
    selected_databases: set[str] = set()
    for task in ranked:
        if task.database_id in selected_databases:
            continue
        selected.append(task.task_id)
        selected_databases.add(task.database_id)
        if len(selected) == 2:
            break
    if len(selected) < 2:
        raise ValueError("pilot calibration requires tasks from two distinct databases")
    return PilotCalibrationSpec(task_ids=(selected[0], selected[1]))


def load_pilot_calibration_spec(
    root: Path,
    manifest: FormalManifestV2,
) -> PilotCalibrationSpec:
    specification = load_document(root / "calibration.json", PilotCalibrationSpec)
    expected = build_pilot_calibration_spec(manifest)
    if specification != expected:
        raise ValueError("pilot calibration task selection is not reproducible")
    return specification


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


def _require_pilot_locked_inputs(
    lock: InputLockV2,
    root: Path,
) -> None:
    require_locked_inputs(
        lock,
        root,
        [
            root / "registration.json",
            root / "manifest.json",
            root / "calibration.json",
            root / "agent-tasks.json",
            root / "source-manifest.json",
            root / "databases",
        ],
    )


def _read_locked_bytes(lock: InputLockV2, root: Path, relative_name: str) -> bytes:
    path = root / relative_name
    require_locked_inputs(lock, root, [path])
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != lock.files[relative_name]:
        raise ValueError(f"locked input hash mismatch: {relative_name}")
    return raw


def verify_pilot_input_bundle(
    root: Path,
) -> tuple[PilotRegistration, FormalManifestV2, RunPlanV2, InputLockV2]:
    registration = load_document(root / "registration.json", PilotRegistration)
    manifest = load_document(root / "manifest.json", FormalManifestV2)
    run_plan = load_document(root / "run-plan.json", RunPlanV2)
    lock = load_document(root / "input-lock.json", InputLockV2)
    verify_input_lock(lock, root)
    _require_pilot_locked_inputs(lock, root)
    tasks = TypeAdapter(list[SealedAgentTask]).validate_json(
        _read_locked_bytes(lock, root, "agent-tasks.json")
    )
    require_locked_inputs(
        lock,
        root,
        [root / task.database_path for task in tasks],
    )
    verify_sealed_manifest(manifest, tasks)
    load_pilot_calibration_spec(root, manifest)
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
    return registration, manifest, run_plan, lock


def verify_pilot_inputs(root: Path) -> tuple[PilotRegistration, FormalManifestV2, RunPlanV2]:
    registration, manifest, run_plan, _ = verify_pilot_input_bundle(root)
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
    build_safety = commands.add_parser("build-safety")
    build_safety.add_argument("--output", type=Path, required=True)
    build_safety.add_argument("--commit", required=True)
    build_contract_safety = commands.add_parser("build-contract-safety")
    build_contract_safety.add_argument("--output", type=Path, required=True)
    build_contract_safety.add_argument("--commit", required=True)
    build_contract_ambiguity = commands.add_parser("build-contract-ambiguity")
    build_contract_ambiguity.add_argument("--output", type=Path, required=True)
    build_contract_ambiguity.add_argument("--commit", required=True)
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
    if arguments.command == "build-safety":
        report = build_safety_pilot_inputs(arguments.output, commit=arguments.commit)
        print(json.dumps(report, sort_keys=True))
        return 0
    if arguments.command == "build-contract-safety":
        report = build_contract_safety_pilot_inputs(
            arguments.output,
            commit=arguments.commit,
        )
        print(json.dumps(report, sort_keys=True))
        return 0
    if arguments.command == "build-contract-ambiguity":
        report = build_contract_ambiguity_pilot_inputs(
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
