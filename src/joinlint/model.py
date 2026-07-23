from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from joinlint.config import _StrictModel, _project_boundary, _read_yaml, write_yaml_atomically
from joinlint.contracts import canonical_json
from joinlint.errors import JoinLintError
from joinlint.paths import SafeProject


class Grain(_StrictModel):
    keys: list[str]
    status: Literal["confirmed"]

    @field_validator("keys")
    @classmethod
    def require_single_key(cls, value: list[str]) -> list[str]:
        if len(value) != 1 or not value[0]:
            raise ValueError("schema version 1 requires exactly one grain key")
        return value


class Entity(_StrictModel):
    source: str
    object: str
    grain: Grain


class Relationship(_StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    id: str
    from_: str = Field(alias="from")
    to: str
    cardinality: Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"]
    status: Literal["confirmed"]

    @field_validator("from_", "to")
    @classmethod
    def require_endpoint(cls, value: str) -> str:
        if value.count(".") != 1 or any(not component for component in value.split(".")):
            raise ValueError("relationship endpoints must be entity.column")
        return value


class ModelV1(_StrictModel):
    version: Literal[1]
    entities: dict[str, Entity]
    relationships: list[Relationship]

    @model_validator(mode="after")
    def validate_relationship_ids_and_entities(self) -> ModelV1:
        relationship_ids: set[str] = set()
        for relationship in self.relationships:
            if not relationship.id or relationship.id in relationship_ids:
                raise ValueError("relationship IDs must be unique and non-empty")
            relationship_ids.add(relationship.id)
            for endpoint in (relationship.from_, relationship.to):
                entity_id, _ = endpoint.split(".")
                if entity_id not in self.entities:
                    raise ValueError("relationship endpoint references an unknown entity")
        return self


def load_model(project: Path | SafeProject) -> ModelV1:
    with _project_boundary(project) as boundary:
        document: Any = _read_yaml(boundary, PurePosixPath(".joinlint/model.yaml"), "MALFORMED_MODEL")
        try:
            return ModelV1.model_validate(document)
        except ValidationError as exc:
            raise JoinLintError("MALFORMED_MODEL", "model.yaml does not match schema version 1", 2) from exc


def model_digest(model: ModelV1) -> str:
    return hashlib.sha256(canonical_json(model.model_dump(mode="json", by_alias=True))).hexdigest()


def write_model(project: Path | SafeProject, model: ModelV1) -> None:
    with _project_boundary(project) as boundary:
        write_yaml_atomically(boundary.root / ".joinlint" / "model.yaml", model)
