from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from joinlint.cli import app


runner = CliRunner()


def test_init_then_add_csv_directory(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "data").mkdir()

    init = runner.invoke(app, ["init", "--project", str(project)])
    add = runner.invoke(app, ["source", "add", "sales", "data", "--project", str(project)])

    assert init.exit_code == 0, init.output
    assert add.exit_code == 0, add.output
    config = (project / ".joinlint" / "config.yaml").read_text(encoding="utf-8")
    assert "sales:" in config
    assert "kind: csv_directory" in config
    assert (project / ".joinlint" / "model.yaml").exists()
    assert not (project / ".joinlint" / "generated" / "manifest.json").exists()


def test_init_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    assert runner.invoke(app, ["init", "--project", str(project)]).exit_code == 0
    repeated = runner.invoke(app, ["init", "--project", str(project)])

    assert repeated.exit_code == 2
    assert "ALREADY_INITIALIZED" in repeated.output


def test_source_add_rejects_symlink(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    assert runner.invoke(app, ["init", "--project", str(project)]).exit_code == 0
    (project / "data").symlink_to(outside, target_is_directory=True)
    result = runner.invoke(app, ["source", "add", "sales", "data", "--project", str(project)])

    assert result.exit_code == 2
    assert "SYMLINK_NOT_ALLOWED" in result.output


def test_scan_writes_generated_evidence_without_mutating_model(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    data = project / "data"
    data.mkdir()
    (data / "parents.csv").write_text("id\na\nb\n", encoding="utf-8")
    (data / "children.csv").write_text("id,parent_id\n1,a\n2,a\n", encoding="utf-8")
    assert runner.invoke(app, ["init", "--project", str(project)]).exit_code == 0
    assert runner.invoke(app, ["source", "add", "sales", "data", "--project", str(project)]).exit_code == 0
    model_before = (project / ".joinlint" / "model.yaml").read_bytes()

    result = runner.invoke(app, ["scan", "--project", str(project), "--json"])

    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    assert envelope["status"] == "ok"
    assert (project / ".joinlint" / "generated" / "manifest.json").exists()
    assert (project / ".joinlint" / "generated" / "relationship-candidates.json").exists()
    assert (project / ".joinlint" / "model.yaml").read_bytes() == model_before


def test_candidates_and_reject_commands_require_fresh_evidence(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    data = project / "data"
    data.mkdir()
    (data / "parents.csv").write_text("id\na\nb\n", encoding="utf-8")
    (data / "children.csv").write_text("id,parent_id\n1,a\n2,a\n", encoding="utf-8")
    assert runner.invoke(app, ["init", "--project", str(project)]).exit_code == 0
    assert runner.invoke(app, ["source", "add", "sales", "data", "--project", str(project)]).exit_code == 0
    assert runner.invoke(app, ["scan", "--project", str(project)]).exit_code == 0

    candidates = runner.invoke(app, ["candidates", "--project", str(project), "--json"])
    candidate_id = json.loads(candidates.output)["data"]["candidates"][0]["id"]
    rejected = runner.invoke(app, ["reject", candidate_id, "--project", str(project)])
    candidates_after_reject = runner.invoke(app, ["candidates", "--project", str(project), "--json"])

    assert candidates.exit_code == 0, candidates.output
    assert rejected.exit_code == 0, rejected.output
    assert candidate_id not in {item["id"] for item in json.loads(candidates_after_reject.output)["data"]["candidates"]}

    with (data / "children.csv").open("a", encoding="utf-8") as source:
        source.write("3,missing\n")
    stale = runner.invoke(app, ["candidates", "--project", str(project), "--json"])
    assert stale.exit_code == 3
    assert json.loads(stale.output)["error"]["code"] == "EVIDENCE_STALE"
