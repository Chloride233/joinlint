from __future__ import annotations

import math
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Arm = Literal["A", "B", "C", "D"]
Edge = tuple[str, str]


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
        strict=True,
    )


class SelectedTask(StrictModel):
    task_id: str
    db_id: str
    question: str
    schema_text: str
    schema_map: dict[str, dict[str, str]] = Field(alias="schema")
    gold_sql: str
    allowed_graphs: list[list[Edge]]
    oracle_relationships: list[dict[str, object]]
    database_path: str


class PilotSample(StrictModel):
    suite: Literal["primary", "zero_config", "safety"]
    sample_id: str
    task_id: str
    db_id: str
    arm: Arm
    repetition: int = Field(ge=0, le=2)
    question: str
    schema_text: str
    schema_map: dict[str, dict[str, str]] = Field(alias="schema")
    gold_sql: str
    allowed_graphs: list[list[Edge]]
    oracle_relationships: list[dict[str, object]]
    oracle_validations: list[dict[str, object]]
    database_path: str
    mcp_project: str | None = None

    @model_validator(mode="after")
    def require_project_only_for_mcp_arms(self) -> PilotSample:
        if (self.arm in {"C", "D"}) != (self.mcp_project is not None):
            raise ValueError("only MCP arms require mcp_project")
        return self


class JoinScore(StrictModel):
    wrong_join: bool
    precision: float
    recall: float
    f1: float
    predicted_edges: list[Edge]
    matched_graph: list[Edge]
    error_code: str | None = None


class ValidatedSQL(StrictModel):
    sql: str | None = None
    error_code: str | None = None

    @model_validator(mode="after")
    def require_exactly_one_result(self) -> ValidatedSQL:
        if (self.sql is None) == (self.error_code is None):
            raise ValueError("exactly one of sql or error_code is required")
        return self


class Submission(StrictModel):
    sql: str
    warning: str


class TraceScore(StrictModel):
    tool_called: bool
    successful_tool_call: bool
    correct_tool_use: bool
    grounded: bool
    validated: bool
    blocking_compliant: bool | None
    bypassed: bool
    tool_error: bool
    returned_edges: list[Edge]
    called_tools: list[str]


class ResultRow(StrictModel):
    sample_id: str
    task_id: str
    db_id: str
    arm: Arm
    suite: Literal["primary", "zero_config", "safety"]
    repetition: int
    artifact_complete: bool
    wrong_join: bool
    join_precision: float
    join_recall: float
    join_f1: float
    execution_correct: bool | None
    grounded: bool | None
    tool_called: bool | None
    validated: bool | None
    blocking_compliant: bool | None
    input_tokens: int
    input_cache_read_tokens: int
    output_tokens: int
    total_cost_usd: float
    total_time_seconds: float
    error_code: str | None

    @model_validator(mode="after")
    def require_sanitized_values(self) -> ResultRow:
        for name in ("sample_id", "task_id", "db_id"):
            value = getattr(self, name)
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
                raise ValueError(f"{name} must be a non-empty relative identifier")
        if self.repetition < 0 or min(
            self.input_tokens,
            self.input_cache_read_tokens,
            self.output_tokens,
        ) < 0:
            raise ValueError("repetition and token counts must be non-negative")
        if self.input_cache_read_tokens > self.input_tokens:
            raise ValueError("cache-read tokens cannot exceed input tokens")
        if not all(
            0.0 <= value <= 1.0
            for value in (self.join_precision, self.join_recall, self.join_f1)
        ):
            raise ValueError("join metrics must be within [0, 1]")
        if (
            not math.isfinite(self.total_cost_usd)
            or not math.isfinite(self.total_time_seconds)
            or self.total_cost_usd < 0.0
            or self.total_time_seconds < 0.0
        ):
            raise ValueError("cost and time must be non-negative")
        if self.error_code is not None and (
            not self.error_code
            or len(self.error_code) > 64
            or any(
                character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
                for character in self.error_code
            )
        ):
            raise ValueError("error_code must be a sanitized category")
        if self.arm in {"A", "B"} and any(
            value is not None
            for value in (
                self.grounded,
                self.tool_called,
                self.validated,
                self.blocking_compliant,
            )
        ):
            raise ValueError("non-MCP arms cannot carry MCP behavior values")
        if not self.artifact_complete and (
            not self.wrong_join
            or self.join_precision != 0.0
            or self.join_recall != 0.0
            or self.join_f1 != 0.0
            or self.execution_correct is not False
            or self.error_code != "MISSING_ARTIFACT"
        ):
            raise ValueError("missing artifacts must retain the frozen ITT values")
        if self.artifact_complete and self.error_code == "MISSING_ARTIFACT":
            raise ValueError("complete artifacts cannot use MISSING_ARTIFACT")
        return self
