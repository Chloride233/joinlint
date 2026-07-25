from __future__ import annotations

import json
import os
import secrets
from pathlib import Path, PurePosixPath
from typing import Any

from joinlint.errors import JoinLintError
from joinlint.paths import SafeProject
from joinlint.report import render_report


def write_generated_transaction(
    project: Path, files: dict[str, bytes], report_data: dict[str, Any]
) -> dict[str, Any]:
    """Atomically publish a complete generated-evidence directory."""
    generated = PurePosixPath(".joinlint/generated")
    temporary = PurePosixPath(f".joinlint/.generated-{secrets.token_hex(12)}")
    backup = PurePosixPath(".joinlint/.generated-previous")
    replaced_previous = False
    try:
        complete_files = {**files, "report.html": render_report(report_data).encode("utf-8")}
        manifest = _read_manifest_document(json.loads(complete_files["manifest.json"]))
        with SafeProject(project) as safe_project:
            safe_project.ensure_directory_relative(PurePosixPath(".joinlint"))
            safe_project.create_directory_relative(temporary)
            for name, body in complete_files.items():
                target = _relative_artifact_path(name)
                if len(target.parts) > 1:
                    safe_project.ensure_directory_relative(temporary / PurePosixPath(*target.parts[:-1]))
                write_file(safe_project, temporary / target, body)
            if safe_project.exists_relative(backup):
                safe_project.remove_tree_relative(backup)
            if safe_project.exists_relative(generated):
                safe_project.replace_relative(generated, backup)
                replaced_previous = True
            safe_project.replace_relative(temporary, generated)
            if replaced_previous:
                safe_project.remove_tree_relative(backup)
        return manifest
    except (JoinLintError, OSError, ValueError, json.JSONDecodeError) as exc:
        if replaced_previous:
            try:
                with SafeProject(project) as safe_project:
                    if safe_project.exists_relative(backup) and not safe_project.exists_relative(generated):
                        safe_project.replace_relative(backup, generated)
            except JoinLintError:
                pass
        raise JoinLintError("GENERATED_WRITE_FAILED", "generated evidence could not be written", 3) from exc
    finally:
        try:
            with SafeProject(project) as safe_project:
                safe_project.remove_tree_relative(temporary, missing_ok=True)
        except JoinLintError:
            pass


def write_file(project: SafeProject, relative_path: PurePosixPath, body: bytes) -> None:
    project.write_relative_atomically(relative_path, body)


def load_manifest(project: Path) -> dict[str, Any]:
    try:
        with SafeProject(project) as safe_project:
            descriptor = safe_project.open_relative(
                PurePosixPath(".joinlint/generated/manifest.json"), os.O_RDONLY
            )
            with os.fdopen(descriptor, "r", encoding="utf-8") as source:
                return _read_manifest_document(json.load(source))
    except (JoinLintError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise JoinLintError("EVIDENCE_STALE", "generated evidence is missing or invalid", 3) from exc


def load_fresh_manifest(
    project: Path, source_fingerprints: list[str], model_digest: str
) -> dict[str, Any]:
    manifest = load_manifest(project)
    if manifest.get("source_fingerprints") != source_fingerprints or manifest.get("model_digest") != model_digest:
        raise JoinLintError("EVIDENCE_STALE", "generated evidence does not match current state", 3)
    return manifest


def write_report_atomically(project: Path, report_data: dict[str, Any]) -> None:
    with SafeProject(project) as safe_project:
        safe_project.write_relative_atomically(
            PurePosixPath(".joinlint/generated/report.html"), render_report(report_data).encode("utf-8")
        )


def _relative_artifact_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("artifact name must be relative")
    return path


def _read_manifest_document(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("manifest schema is invalid")
    return document
