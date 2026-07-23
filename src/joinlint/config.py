from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator
from yaml.resolver import BaseResolver

from joinlint.errors import JoinLintError
from joinlint.paths import SafeProject


SourceKind = Literal["csv_directory", "sqlite"]
DEFAULT_MAX_SOURCE_BYTES = 1_073_741_824
DEFAULT_MAX_SCAN_SECONDS = 60


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Limits(_StrictModel):
    max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES
    max_scan_seconds: int = DEFAULT_MAX_SCAN_SECONDS

    @field_validator("max_source_bytes", "max_scan_seconds")
    @classmethod
    def require_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("limit must be positive")
        return value


class SourceConfig(_StrictModel):
    kind: SourceKind
    path: PurePosixPath
    limits: Limits = Limits()

    @field_validator("path", mode="before")
    @classmethod
    def require_safe_relative_path(cls, value: object) -> PurePosixPath:
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError("source path must be a non-empty string without NUL bytes")
        path = PurePosixPath(value)
        if path.is_absolute() or not path.parts or ".." in path.parts or str(path) == ".":
            raise ValueError("source path must be project-relative")
        return path


class ConfigV1(_StrictModel):
    version: Literal[1]
    sources: dict[str, SourceConfig]

    @model_validator(mode="after")
    def validate_source_ids(self) -> ConfigV1:
        for source_id in self.sources:
            if not source_id or "\x00" in source_id:
                raise ValueError("source IDs must be non-empty strings without NUL bytes")
        return self


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key: {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


@contextmanager
def _project_boundary(project: Path | SafeProject) -> Iterator[SafeProject]:
    if isinstance(project, SafeProject):
        yield project
        return
    with SafeProject(project) as boundary:
        yield boundary


def _read_yaml(project: SafeProject, relative_path: PurePosixPath, code: str) -> Any:
    try:
        descriptor = project.open_relative(relative_path, os.O_RDONLY)
        with os.fdopen(descriptor, "r", encoding="utf-8") as source:
            return yaml.load(source, Loader=_UniqueKeyLoader)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise JoinLintError(code, "YAML document is malformed", 2) from exc


def _validate_config(document: Any) -> ConfigV1:
    try:
        return ConfigV1.model_validate(document)
    except ValidationError as exc:
        raise JoinLintError("MALFORMED_CONFIG", "config.yaml does not match schema version 1", 2) from exc


def load_config(project: Path | SafeProject) -> ConfigV1:
    with _project_boundary(project) as boundary:
        return _validate_config(_read_yaml(boundary, PurePosixPath(".joinlint/config.yaml"), "MALFORMED_CONFIG"))


def write_yaml_atomically(path: Path, document: BaseModel) -> None:
    payload = yaml.safe_dump(
        document.model_dump(mode="json", by_alias=True),
        allow_unicode=True,
        sort_keys=True,
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
        parent_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _source_kind_for_path(path: PurePosixPath) -> SourceKind:
    return "sqlite" if path.suffix == ".sqlite" else "csv_directory"


def _validate_source_argument(kind: SourceKind, path: str) -> SourceConfig:
    try:
        return SourceConfig(kind=kind, path=path)
    except ValidationError as exc:
        raise JoinLintError("INVALID_ARGUMENT", "source path or kind is invalid", 2) from exc


def _validate_source_id(source_id: str) -> None:
    if not source_id or "\x00" in source_id:
        raise JoinLintError("INVALID_ARGUMENT", "source ID is invalid", 2)


def add_source(project: Path | SafeProject, source_id: str, path: str, kind: SourceKind) -> None:
    with _project_boundary(project) as boundary:
        config = load_config(boundary)
        _validate_source_id(source_id)
        if source_id in config.sources:
            raise JoinLintError("DUPLICATE_SOURCE_ID", "source ID already exists", 2)
        source = _validate_source_argument(kind, path)
        updated = ConfigV1(version=1, sources={**config.sources, source_id: source})
        write_yaml_atomically(boundary.root / ".joinlint" / "config.yaml", updated)


def set_source_path(project: Path | SafeProject, source_id: str, path: str) -> None:
    with _project_boundary(project) as boundary:
        config = load_config(boundary)
        existing = config.sources.get(source_id)
        if existing is None:
            raise JoinLintError("SOURCE_NOT_FOUND", "source ID does not exist", 2)

        parsed_path = _validate_source_argument(existing.kind, path).path
        if _source_kind_for_path(parsed_path) != existing.kind:
            raise JoinLintError("SOURCE_KIND_MISMATCH", "source kind cannot change", 2)
        updated_sources = {**config.sources, source_id: existing.model_copy(update={"path": parsed_path})}
        write_yaml_atomically(boundary.root / ".joinlint" / "config.yaml", ConfigV1(version=1, sources=updated_sources))


def remove_source(project: Path | SafeProject, source_id: str) -> None:
    from joinlint.model import load_model

    with _project_boundary(project) as boundary:
        config = load_config(boundary)
        if source_id not in config.sources:
            raise JoinLintError("SOURCE_NOT_FOUND", "source ID does not exist", 2)

        model = load_model(boundary)
        dependents = [entity_id for entity_id, entity in model.entities.items() if entity.source == source_id]
        if dependents:
            raise JoinLintError("SOURCE_IN_USE", f"source is used by {', '.join(dependents[:20])}", 2)
        updated_sources = {key: value for key, value in config.sources.items() if key != source_id}
        write_yaml_atomically(boundary.root / ".joinlint" / "config.yaml", ConfigV1(version=1, sources=updated_sources))
