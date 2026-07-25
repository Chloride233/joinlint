from __future__ import annotations

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
