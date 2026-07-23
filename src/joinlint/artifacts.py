from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from joinlint.errors import JoinLintError
from joinlint.report import render_report


def write_generated_transaction(
    project: Path, files: dict[str, bytes], report_data: dict[str, Any]
) -> dict[str, Any]:
    """Atomically publish a complete generated-evidence directory."""
    generated = project / ".joinlint" / "generated"
    generated.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".generated-", dir=generated.parent))
    backup = generated.parent / ".generated-previous"
    replaced_previous = False
    try:
        complete_files = {**files, "report.html": render_report(report_data).encode("utf-8")}
        for name, body in complete_files.items():
            target = temporary / _relative_artifact_path(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            write_file(target, body)
        manifest = _read_manifest(temporary / "manifest.json")
        if backup.exists():
            shutil.rmtree(backup)
        if generated.exists():
            os.replace(generated, backup)
            replaced_previous = True
        os.replace(temporary, generated)
        if replaced_previous:
            shutil.rmtree(backup)
        return manifest
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        if replaced_previous and backup.exists() and not generated.exists():
            os.replace(backup, generated)
        raise JoinLintError("GENERATED_WRITE_FAILED", "generated evidence could not be written", 3) from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


def write_file(path: Path, body: bytes) -> None:
    path.write_bytes(body)


def load_manifest(project: Path) -> dict[str, Any]:
    try:
        return _read_manifest(project / ".joinlint" / "generated" / "manifest.json")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise JoinLintError("EVIDENCE_STALE", "generated evidence is missing or invalid", 3) from exc


def load_fresh_manifest(
    project: Path, source_fingerprints: list[str], model_digest: str
) -> dict[str, Any]:
    manifest = load_manifest(project)
    if manifest.get("source_fingerprints") != source_fingerprints or manifest.get("model_digest") != model_digest:
        raise JoinLintError("EVIDENCE_STALE", "generated evidence does not match current state", 3)
    return manifest


def _relative_artifact_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("artifact name must be relative")
    return path


def _read_manifest(path: Path) -> dict[str, Any]:
    document: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("manifest schema is invalid")
    return document
