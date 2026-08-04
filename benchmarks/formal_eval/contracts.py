from __future__ import annotations

import math
from datetime import date
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Corpus = Literal["natural", "adversarial", "semantic_join_failure"]
SourceType = Literal["sqlite", "csv"]
Split = Literal["development", "calibration", "confirmatory", "diagnostic"]
Provenance = Literal["declared", "curated", "verified"]
EvidenceMode = Literal["exact", "sampled", "approximate"]
FKState = Literal["visible", "ablated", "absent"]
SQLClass = Literal["safe", "dangerous"]
SQLDangerType = Literal[
    "unsupported_edge",
    "missing_required_edge",
    "uniqueness",
    "orphan",
    "cardinality",
    "grain",
    "many_to_many",
    "compound_fanout",
    "unsafe_statement",
    "multi_statement",
]
AdversarialType = Literal[
    "same_name_id",
    "integer_domain_collision",
    "small_domain_overlap",
    "multiple_parents",
    "non_unique_parent",
    "orphan",
    "drift",
]
SQLShape = Literal[
    "alias",
    "cte",
    "subquery",
    "where_equality",
    "using",
    "self_join",
    "composite",
    "wrong_edge",
    "grain_change",
    "many_to_many",
    "compound_fanout",
    "ddl_dml",
    "multi_statement",
]
ValidationDecision = Literal["pass", "block", "inconclusive", "error"]
Condition = Literal["control", "treatment", "oracle_inline", "oracle_mcp", "no_harness"]
Host = Literal["codex", "claude_code"]
ModelTier = Literal["high_capability", "cost_efficient"]
RelationshipScope = Literal["declared_only", "verified_inference"]
FailureCode = Literal[
    "TOOL_NOT_CALLED",
    "ENTITY_SET_INCOMPLETE",
    "PLAN_INCONCLUSIVE",
    "NO_VERIFIED_PATH",
    "WRONG_PLAN",
    "SQL_NOT_GROUNDED",
    "VALIDATION_NOT_CALLED",
    "FINAL_SQL_NOT_VALIDATED",
    "VALIDATION_BLOCKED",
    "AGENT_BYPASS",
    "SQL_PARSE_FAILED",
    "EXECUTION_FAILED",
    "MODEL_TIMEOUT",
    "MODEL_LIMIT",
    "MCP_TOOL_ERROR",
    "GROUND_TRUTH_AMBIGUOUS",
    "INFRASTRUCTURE_FAILURE",
]
ProtocolViolation = Literal[
    "PLAN_AFTER_VALIDATION",
    "PLAN_RETRY_LIMIT",
    "PLAN_RETRY_NOT_ALLOWED",
    "PLAN_RETRY_NOT_CHANGED",
    "VALIDATION_BEFORE_PLAN_RESULT",
    "VALIDATION_RETRY_LIMIT",
    "VALIDATION_RETRY_NOT_ALLOWED",
    "VALIDATION_RETRY_NOT_CHANGED",
]
Edge = tuple[str, str]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _validate_identifier(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or value.startswith(("/", "\\"))
        or value.startswith("~/")
        or "\\" in value
        or ".." in path.parts
        or (len(value) >= 3 and value[1:3] == ":/")
        or len(value) > 256
    ):
        raise ValueError("identifier must be a bounded relative identifier")
    return value


def _validate_digest(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("digest must be a lowercase SHA-256 value")
    return value


class ModelPricingCNY(StrictModel):
    observed_at: date
    input_cache_hit_per_million_cny: float = Field(ge=0)
    input_cache_miss_per_million_cny: float = Field(ge=0)
    output_per_million_cny: float = Field(ge=0)

    @model_validator(mode="after")
    def require_finite_prices(self) -> ModelPricingCNY:
        prices = (
            self.input_cache_hit_per_million_cny,
            self.input_cache_miss_per_million_cny,
            self.output_per_million_cny,
        )
        if not all(math.isfinite(value) for value in prices):
            raise ValueError("model prices must be finite")
        if self.input_cache_hit_per_million_cny > self.input_cache_miss_per_million_cny:
            raise ValueError("cache-hit input price cannot exceed cache-miss input price")
        return self


class FrozenModel(StrictModel):
    id: str
    returned_id: str
    family: str
    tier: ModelTier
    thinking: Literal["disabled"]
    pricing_cny: ModelPricingCNY

    @field_validator("id", "returned_id", "family")
    @classmethod
    def require_nonempty(cls, value: str) -> str:
        if not value or len(value) > 256:
            raise ValueError("model metadata must be non-empty and bounded")
        return value


class PublicationThresholds(StrictModel):
    completion_improvement_min: float = 0.10
    execution_noninferiority_margin: float = -0.05
    cell_harm_floor: float = -0.05
    complete_entity_planning_min: float = 0.90
    final_sql_validation_min: float = 0.90
    mcp_grounding_min: float = 0.90
    blocking_compliance_min: float = 0.95
    bypass_max: float = 0.05
    tool_error_max: float = 0.02

    @model_validator(mode="after")
    def require_rates(self) -> PublicationThresholds:
        values = self.model_dump().values()
        if not all(math.isfinite(value) and -1.0 <= value <= 1.0 for value in values):
            raise ValueError("publication thresholds must be finite rates")
        return self


class PreregistrationV2(StrictModel):
    schema_version: Literal[2] = 2
    evaluation_id: str
    dataset_release: str
    seed: int = Field(ge=0)
    repetitions: Literal[3] = 3
    bootstrap_draws: int = Field(default=10_000, ge=1_000)
    models: tuple[FrozenModel, FrozenModel]
    hosts: tuple[Literal["codex"], Literal["claude_code"]] = ("codex", "claude_code")
    host_versions: dict[Host, str]
    joinlint_commit: str
    harness_version: str
    inference_policy_version: str
    relationship_scope: RelationshipScope
    image_digest: str
    image_reference: str
    formal_environment: Literal["github-actions-modal"] = "github-actions-modal"
    thresholds: PublicationThresholds = Field(default_factory=PublicationThresholds)

    @field_validator("evaluation_id", "dataset_release", "harness_version", "inference_policy_version")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _validate_identifier(value)

    @field_validator("joinlint_commit")
    @classmethod
    def validate_commit(cls, value: str) -> str:
        if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("joinlint_commit must be a full lowercase Git SHA")
        return value

    @field_validator("image_digest")
    @classmethod
    def validate_image_digest(cls, value: str) -> str:
        if not value.startswith("sha256:"):
            raise ValueError("image_digest must use sha256:<digest>")
        _validate_digest(value.removeprefix("sha256:"))
        return value

    @model_validator(mode="after")
    def require_digest_pinned_image(self) -> PreregistrationV2:
        if not self.image_reference.endswith(f"@{self.image_digest}"):
            raise ValueError("image reference must end with the frozen image digest")
        return self

    @model_validator(mode="after")
    def require_distinct_models(self) -> PreregistrationV2:
        if len({model.id for model in self.models}) != 2:
            raise ValueError("formal evaluation requires two distinct model IDs")
        if len({model.returned_id for model in self.models}) != 2:
            raise ValueError("formal evaluation requires two distinct returned model identities")
        if {model.tier for model in self.models} != {"high_capability", "cost_efficient"}:
            raise ValueError("formal evaluation requires one model from each tier")
        if len({model.family for model in self.models}) != 1:
            raise ValueError("formal evaluation is frozen to one provider family with two tiers")
        if set(self.host_versions) != set(self.hosts) or any(
            not value or len(value) > 64 for value in self.host_versions.values()
        ):
            raise ValueError("every formal host requires one bounded pinned version")
        return self


class SealedAgentTask(StrictModel):
    task_id: str
    database_id: str
    question: str
    schema_text: str
    schema_map: dict[str, dict[str, str]] = Field(alias="schema")
    sql_shape: str
    gold_sql: str
    database_path: str
    expected_entities: tuple[str, ...]
    allowed_graphs: tuple[tuple[Edge, ...], ...]
    oracle_has_safe_path: bool

    @field_validator("task_id", "database_id")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        return _validate_identifier(value)

    @field_validator("database_path")
    @classmethod
    def validate_database_path(cls, value: str) -> str:
        return _validate_identifier(value)

    @model_validator(mode="after")
    def validate_payload(self) -> SealedAgentTask:
        if not self.question or not self.schema_text or not self.sql_shape or not self.database_path:
            raise ValueError("sealed task text and database path must be non-empty")
        if len(set(self.expected_entities)) != len(self.expected_entities):
            raise ValueError("expected entities must be unique")
        if self.oracle_has_safe_path != bool(self.allowed_graphs):
            raise ValueError("oracle path state must agree with allowed graphs")
        return self


class FormalTask(StrictModel):
    task_id: str
    database_id: str
    database_variant_group: str
    corpus: Literal["natural", "semantic_join_failure"]
    split: Literal["confirmatory", "diagnostic"]
    domain: str
    source_type: SourceType
    database_scale: Literal["small", "medium", "large"]
    ambiguity: Literal["none", "low", "high"]
    fanout_type: Literal["none", "one_to_many", "many_to_many", "compound"]
    question_sha256: str
    schema_sha256: str
    sql_shape_sha256: str
    schema_family_id: str
    question_template_id: str
    sql_structure_id: str
    allowed_graphs: tuple[tuple[Edge, ...], ...]
    oracle_has_safe_path: bool
    join_depth: int = Field(ge=0, le=4)
    ambiguous_ground_truth: bool = False

    @field_validator(
        "task_id",
        "database_id",
        "database_variant_group",
        "domain",
        "schema_family_id",
        "question_template_id",
        "sql_structure_id",
    )
    @classmethod
    def validate_ids(cls, value: str) -> str:
        return _validate_identifier(value)

    @field_validator("question_sha256", "schema_sha256", "sql_shape_sha256")
    @classmethod
    def validate_hashes(cls, value: str) -> str:
        return _validate_digest(value)

    @model_validator(mode="after")
    def validate_graphs(self) -> FormalTask:
        if self.oracle_has_safe_path and not self.allowed_graphs:
            raise ValueError("a task with a safe path requires at least one allowed graph")
        if not self.oracle_has_safe_path and self.allowed_graphs:
            raise ValueError("a task without a safe path cannot carry an allowed graph")
        if self.split == "confirmatory" and self.ambiguous_ground_truth:
            raise ValueError("ambiguous ground truth cannot enter the confirmatory split")
        return self


class FormalManifestV2(StrictModel):
    schema_version: Literal[2] = 2
    dataset_release: str
    tasks: tuple[FormalTask, ...]

    @field_validator("dataset_release")
    @classmethod
    def validate_release(cls, value: str) -> str:
        return _validate_identifier(value)

    @model_validator(mode="after")
    def require_unique_tasks(self) -> FormalManifestV2:
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("formal manifest contains duplicate task IDs")
        return self


class InputLockV2(StrictModel):
    schema_version: Literal[2] = 2
    files: dict[str, str]

    @field_validator("files")
    @classmethod
    def validate_files(cls, value: dict[str, str]) -> dict[str, str]:
        if not value:
            raise ValueError("input lock cannot be empty")
        for name, digest in value.items():
            _validate_identifier(name)
            _validate_digest(digest)
        return value


class RelationshipResultRow(StrictModel):
    case_id: str
    database_id: str
    variant_group: str
    corpus: Literal["natural", "adversarial"]
    source_type: SourceType
    domain: str
    split: Split
    provenance: Provenance
    eligible: bool
    expected_relation: bool
    extracted: bool
    auto_usable: bool
    prediction_correct: bool | None
    expected_edges: tuple[Edge, ...]
    predicted_edges: tuple[Edge, ...]
    direction_correct: bool | None
    evidence_mode: EvidenceMode
    stale: bool
    fk_state: FKState | None = None
    adversarial_type: AdversarialType | None = None
    reviewer_labels: tuple[bool, bool] | None = None
    adjudicated_label: bool | None = None

    @field_validator("case_id", "database_id", "variant_group", "domain")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        return _validate_identifier(value)

    @model_validator(mode="after")
    def require_prediction_state(self) -> RelationshipResultRow:
        if self.auto_usable and self.prediction_correct is None:
            raise ValueError("an auto-usable relationship requires a correctness label")
        if self.prediction_correct is not None and not self.extracted:
            raise ValueError("an unextracted relationship cannot have prediction correctness")
        expected_correct = (
            self.extracted
            and self.predicted_edges == self.expected_edges
            and self.direction_correct is True
        )
        if self.prediction_correct is not None and self.prediction_correct != expected_correct:
            raise ValueError("relationship correctness must match endpoints and direction")
        if self.corpus == "adversarial" and self.adversarial_type is None:
            raise ValueError("adversarial relationships require a trap type")
        if self.corpus == "natural" and self.adversarial_type is not None:
            raise ValueError("natural relationships cannot carry an adversarial trap type")
        if self.corpus == "natural" and self.fk_state is None:
            raise ValueError("natural relationships require an FK visibility state")
        if self.corpus == "adversarial" and self.fk_state is not None:
            raise ValueError("adversarial relationships cannot carry an FK visibility state")
        if self.provenance == "declared" and self.fk_state == "absent":
            raise ValueError("declared relationships require visible or ablated FK provenance")
        if self.provenance in {"curated", "verified"} and (
            self.reviewer_labels is None or self.adjudicated_label is None
        ):
            raise ValueError("curated and verified relationships require adjudicated labels")
        return self


class SQLValidationResultRow(StrictModel):
    case_id: str
    sql_class: SQLClass
    supported_shape: bool
    sql_shape: SQLShape
    danger_type: SQLDangerType | None = None
    decision: ValidationDecision
    extracted_edges_correct: bool
    repair_uses_only_auto_usable: bool
    execution_count: int = Field(ge=0)

    @field_validator("case_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _validate_identifier(value)

    @model_validator(mode="after")
    def require_danger_taxonomy(self) -> SQLValidationResultRow:
        if self.sql_class == "dangerous" and not self.danger_type:
            raise ValueError("dangerous SQL requires a danger type")
        if self.sql_class == "safe" and self.danger_type is not None:
            raise ValueError("safe SQL cannot carry a danger type")
        return self


class PerformanceMeasurement(StrictModel):
    cached_plan_p95_ms: float = Field(ge=0)
    cached_validation_p95_ms: float = Field(ge=0)
    metadata_plan_p95_ms: float = Field(ge=0)
    million_row_seconds: float = Field(ge=0)
    million_row_peak_rss_bytes: int = Field(ge=0)


class BlindReviewDecision(StrictModel):
    sample_id: str
    output_sha256: str
    reviewer_id_sha256: str
    automated_join_correct: bool
    human_join_correct: bool

    @field_validator("sample_id")
    @classmethod
    def validate_sample_id(cls, value: str) -> str:
        return _validate_identifier(value)

    @field_validator("output_sha256", "reviewer_id_sha256")
    @classmethod
    def validate_review_digests(cls, value: str) -> str:
        return _validate_digest(value)


class BlindReviewBundle(StrictModel):
    schema_version: Literal[2] = 2
    lineage_id: str
    formal_results_sha256: str
    run_plan_sha256: str
    review_protocol_version: str
    arm_labels_removed: Literal[True]
    decisions: tuple[BlindReviewDecision, ...]

    @field_validator("lineage_id", "formal_results_sha256", "run_plan_sha256")
    @classmethod
    def validate_digests(cls, value: str) -> str:
        return _validate_digest(value)

    @model_validator(mode="after")
    def require_unique_decisions(self) -> BlindReviewBundle:
        sample_ids = [decision.sample_id for decision in self.decisions]
        if len(sample_ids) != len(set(sample_ids)):
            raise ValueError("blind review contains duplicate sample IDs")
        return self


class AgentResultRow(StrictModel):
    sample_id: str
    task_id: str
    database_id: str
    corpus: Literal["natural", "semantic_join_failure"]
    condition: Condition
    model_id: str
    host: Host
    domain: str
    source_type: SourceType
    database_scale: Literal["small", "medium", "large"]
    join_depth: int = Field(ge=0, le=4)
    ambiguity: Literal["none", "low", "high"]
    fanout_type: Literal["none", "one_to_many", "many_to_many", "compound"]
    repetition: int = Field(ge=0)
    output_sha256: str
    artifact_complete: bool
    oracle_has_safe_path: bool
    submitted_sql: bool
    safe_abstention: bool
    join_graph_correct: bool
    evaluator_validation_passed: bool
    join_correct_task_completion: bool
    dangerous_sql_submitted: bool
    execution_correct: bool | None
    plan_called: bool | None
    plan_usable: bool | None
    complete_entity_planning: bool | None
    final_sql_validated: bool | None
    mcp_grounded: bool | None
    protocol_compliant: bool | None
    protocol_violation: ProtocolViolation | None
    blocking_applicable: bool | None
    blocking_compliant: bool | None
    bypassed: bool | None
    tool_error: bool | None
    total_time_seconds: float = Field(ge=0)
    input_tokens: int = Field(ge=0)
    input_cache_read_tokens: int = Field(ge=0)
    input_cache_write_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    calculated_cost_cny: float = Field(ge=0)
    provider_reported_cost_usd: float | None = Field(default=None, ge=0)
    failure_code: FailureCode | None = None

    @field_validator("sample_id", "task_id", "database_id", "domain")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        return _validate_identifier(value)

    @field_validator("output_sha256")
    @classmethod
    def validate_output_digest(cls, value: str) -> str:
        return _validate_digest(value)

    @model_validator(mode="after")
    def require_consistent_outcome(self) -> AgentResultRow:
        behavior = (
            self.plan_called,
            self.plan_usable,
            self.complete_entity_planning,
            self.final_sql_validated,
            self.mcp_grounded,
            self.protocol_compliant,
            self.protocol_violation,
            self.blocking_applicable,
            self.blocking_compliant,
            self.bypassed,
            self.tool_error,
        )
        mcp_condition = self.condition in {"treatment", "oracle_mcp", "no_harness"}
        if not mcp_condition and any(value is not None for value in behavior):
            raise ValueError("control rows and other non-MCP rows cannot contain MCP behavior values")
        required_behavior = (
            self.plan_called,
            self.plan_usable,
            self.complete_entity_planning,
            self.final_sql_validated,
            self.mcp_grounded,
            self.protocol_compliant,
            self.blocking_applicable,
            self.bypassed,
            self.tool_error,
        )
        if mcp_condition and any(value is None for value in required_behavior):
            raise ValueError("MCP conditions require complete behavior values")
        if mcp_condition and (
            self.blocking_applicable == (self.blocking_compliant is None)
        ):
            raise ValueError("blocking compliance must be present exactly when applicable")
        if self.protocol_violation is not None and self.protocol_compliant:
            raise ValueError("protocol violations require protocol noncompliance")
        if self.safe_abstention and (
            self.oracle_has_safe_path
            or self.submitted_sql
            or not self.join_correct_task_completion
            or self.failure_code is not None
        ):
            raise ValueError("safe abstention must be a successful no-path outcome")
        if self.join_correct_task_completion and not self.safe_abstention and not (
            self.submitted_sql and self.join_graph_correct and self.evaluator_validation_passed
        ):
            raise ValueError("join-correct completion requires safe SQL or safe abstention")
        if self.join_correct_task_completion == (self.failure_code is not None):
            raise ValueError("exactly one of successful completion or failure code is required")
        if self.dangerous_sql_submitted and not self.submitted_sql:
            raise ValueError("dangerous submission requires submitted SQL")
        if not self.artifact_complete and self.failure_code != "INFRASTRUCTURE_FAILURE":
            raise ValueError("missing artifacts require INFRASTRUCTURE_FAILURE")
        if self.input_cache_read_tokens > self.input_tokens:
            raise ValueError("cache-read tokens cannot exceed input tokens")
        if self.input_cache_write_tokens > self.input_tokens:
            raise ValueError("cache-write tokens cannot exceed input tokens")
        for value in (
            self.total_time_seconds,
            self.calculated_cost_cny,
            self.provider_reported_cost_usd,
        ):
            if value is None:
                continue
            if not math.isfinite(value):
                raise ValueError("time and cost must be finite")
        return self


class AgentResultBundle(StrictModel):
    schema_version: Literal[3] = 3
    lineage_id: str
    run_plan_sha256: str
    rows: tuple[AgentResultRow, ...]

    @field_validator("lineage_id", "run_plan_sha256")
    @classmethod
    def validate_digests(cls, value: str) -> str:
        return _validate_digest(value)

    @model_validator(mode="after")
    def require_unique_rows(self) -> AgentResultBundle:
        sample_ids = [row.sample_id for row in self.rows]
        if len(sample_ids) != len(set(sample_ids)):
            raise ValueError("agent result bundle contains duplicate sample IDs")
        return self


class DeterministicEvidenceBundle(StrictModel):
    schema_version: Literal[2] = 2
    lineage_id: str
    deterministic_suite_sha256: str
    producer: Literal["joinlint-formal-deterministic-v2"]
    relationship_rows: tuple[RelationshipResultRow, ...]
    sql_validation_rows: tuple[SQLValidationResultRow, ...]
    performance: PerformanceMeasurement

    @field_validator("lineage_id", "deterministic_suite_sha256")
    @classmethod
    def validate_lineage(cls, value: str) -> str:
        return _validate_digest(value)

    @model_validator(mode="after")
    def require_unique_cases(self) -> DeterministicEvidenceBundle:
        relationship_ids = [row.case_id for row in self.relationship_rows]
        sql_ids = [row.case_id for row in self.sql_validation_rows]
        if len(relationship_ids) != len(set(relationship_ids)):
            raise ValueError("deterministic relationship evidence contains duplicate cases")
        if len(sql_ids) != len(set(sql_ids)):
            raise ValueError("deterministic SQL evidence contains duplicate cases")
        return self
