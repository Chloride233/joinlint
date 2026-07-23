from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import typer

from joinlint.config import ConfigV1, SourceConfig, add_source, load_config, remove_source, set_source_path, write_yaml_atomically
from joinlint.contracts import Envelope, envelope_for
from joinlint.errors import JoinLintError
from joinlint.model import ModelV1
from joinlint.paths import SafeProject
from joinlint.services import accept_candidate_by_id, list_candidates, reject_candidate_by_id, scan_project


app = typer.Typer(no_args_is_help=True)
source_app = typer.Typer(no_args_is_help=True)
app.add_typer(source_app, name="source")


def _exit_for_error(error: JoinLintError) -> None:
    typer.echo(f"{error.code}: {error}", err=True)
    raise typer.Exit(error.exit_code)


def _emit(envelope: Envelope, *, json_output: bool) -> None:
    if json_output:
        body = envelope.model_dump_json(exclude_none=False)
        if len(body.encode("utf-8")) > 1_048_576:
            body = envelope_for(command=envelope.command, status="inconclusive", error_code="OUTPUT_LIMIT_EXCEEDED").model_dump_json(
                exclude_none=False
            )
        typer.echo(body)
    else:
        typer.echo(f"{envelope.status}: {envelope.command}")
    exit_code = 0 if envelope.status in {"ok", "findings"} else 3 if envelope.status == "inconclusive" else 2
    raise typer.Exit(exit_code)


def _emit_service_error(command: str, error: JoinLintError, *, json_output: bool) -> None:
    if json_output:
        status = "inconclusive" if error.exit_code == 3 else "error"
        _emit(envelope_for(command=command, status=status, error_code=error.code), json_output=True)
    _exit_for_error(error)


def _init_project(project: Path, force: bool) -> None:
    try:
        root = project.resolve(strict=True)
    except OSError as exc:
        raise JoinLintError("PROJECT_NOT_FOUND", "project directory is unavailable", 2) from exc
    if not root.is_dir() or root.is_symlink():
        raise JoinLintError("PROJECT_NOT_FOUND", "project must be a non-symlink directory", 2)
    joinlint_directory = root / ".joinlint"
    config_path = joinlint_directory / "config.yaml"
    model_path = joinlint_directory / "model.yaml"
    if (config_path.exists() or model_path.exists()) and not force:
        raise JoinLintError("ALREADY_INITIALIZED", "JoinLint files already exist; use --force to replace", 2)
    joinlint_directory.mkdir(exist_ok=True)
    if joinlint_directory.is_symlink():
        raise JoinLintError("SYMLINK_NOT_ALLOWED", "JoinLint directory cannot be a symlink", 2)
    for directory in ("state", "analyses", "generated"):
        (joinlint_directory / directory).mkdir(exist_ok=True)
    write_yaml_atomically(config_path, ConfigV1(version=1, sources={}))
    write_yaml_atomically(model_path, ModelV1(version=1, entities={}, relationships=[]))


@contextmanager
def _config_lock(project: Path) -> Iterator[None]:
    import fcntl

    lock_path = project / ".joinlint" / ".lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _source_kind(project: Path, source_path: str) -> str:
    candidate = SourceConfig(kind="csv_directory", path=source_path).path
    with SafeProject(project) as safe_project:
        try:
            descriptor = safe_project.open_relative(candidate, os.O_RDONLY | os.O_DIRECTORY)
        except JoinLintError as error:
            if error.code != "SAFE_PATH_OPEN_FAILED":
                raise
        else:
            os.close(descriptor)
            return "csv_directory"
        descriptor = safe_project.open_relative(candidate, os.O_RDONLY)
        try:
            if candidate.suffix != ".sqlite":
                raise JoinLintError("UNSUPPORTED_SOURCE", "source file must be a SQLite database", 2)
            return "sqlite"
        finally:
            os.close(descriptor)


@app.command()
def init(
    project: Path = typer.Option(Path.cwd(), "--project"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    """Create the JoinLint project files without scanning data."""
    try:
        _init_project(project, force)
    except JoinLintError as error:
        _exit_for_error(error)


@app.command()
def scan(
    project: Path = typer.Option(Path.cwd(), "--project"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Create one complete generated-evidence transaction for all sources."""
    try:
        _emit(scan_project(project), json_output=json_output)
    except JoinLintError as error:
        _emit_service_error("scan", error, json_output=json_output)


@app.command()
def candidates(
    project: Path = typer.Option(Path.cwd(), "--project"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List fresh, non-rejected relationship candidates."""
    try:
        _emit(list_candidates(project), json_output=json_output)
    except JoinLintError as error:
        _emit_service_error("candidates", error, json_output=json_output)


@app.command()
def accept(
    candidate_id: str,
    project: Path = typer.Option(Path.cwd(), "--project"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Confirm a fresh candidate in model.yaml."""
    try:
        _emit(accept_candidate_by_id(project, candidate_id), json_output=json_output)
    except JoinLintError as error:
        _emit_service_error("accept", error, json_output=json_output)


@app.command()
def reject(
    candidate_id: str,
    project: Path = typer.Option(Path.cwd(), "--project"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Record a local rejection for a fresh candidate."""
    try:
        _emit(reject_candidate_by_id(project, candidate_id), json_output=json_output)
    except JoinLintError as error:
        _emit_service_error("reject", error, json_output=json_output)


@source_app.command("add")
def source_add(
    source_id: str,
    path: str,
    project: Path = typer.Option(Path.cwd(), "--project"),
) -> None:
    """Register a safe local CSV-directory or SQLite source."""
    try:
        with _config_lock(project):
            add_source(project, source_id, path, _source_kind(project, path))
    except JoinLintError as error:
        _exit_for_error(error)


@source_app.command("set")
def source_set(
    source_id: str,
    path: str,
    project: Path = typer.Option(Path.cwd(), "--project"),
) -> None:
    """Replace a source path without changing its registered kind."""
    try:
        with _config_lock(project):
            existing = load_config(project).sources.get(source_id)
            if existing is None:
                raise JoinLintError("SOURCE_NOT_FOUND", "source ID does not exist", 2)
            if _source_kind(project, path) != existing.kind:
                raise JoinLintError("SOURCE_KIND_MISMATCH", "source kind cannot change", 2)
            set_source_path(project, source_id, path)
    except JoinLintError as error:
        _exit_for_error(error)


@source_app.command("remove")
def source_remove(
    source_id: str,
    project: Path = typer.Option(Path.cwd(), "--project"),
) -> None:
    """Remove an unreferenced source from the project."""
    try:
        with _config_lock(project):
            remove_source(project, source_id)
    except JoinLintError as error:
        _exit_for_error(error)
