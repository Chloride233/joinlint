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


def test_source_command_does_not_create_lock_through_project_symlink(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert runner.invoke(app, ["init", "--project", str(project)]).exit_code == 0
    alias = tmp_path / "project-link"
    alias.symlink_to(project, target_is_directory=True)

    result = runner.invoke(app, ["source", "remove", "missing", "--project", str(alias)])

    assert result.exit_code == 2
    assert "SYMLINK_NOT_ALLOWED" in result.output
    assert not (project / ".joinlint" / ".lock").exists()


def test_source_add_reports_invalid_relative_path_as_user_error(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert runner.invoke(app, ["init", "--project", str(project)]).exit_code == 0

    result = runner.invoke(app, ["source", "add", "sales", "../data", "--project", str(project)])

    assert result.exit_code == 2
    assert "INVALID_ARGUMENT" in result.output


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


def test_validate_reports_blocking_drift_with_exit_one(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    data = project / "data"
    data.mkdir()
    (data / "parents.csv").write_text("id\na\nb\n", encoding="utf-8")
    (data / "children.csv").write_text("id,parent_id\n1,a\n2,a\n", encoding="utf-8")
    assert runner.invoke(app, ["init", "--project", str(project)]).exit_code == 0
    assert runner.invoke(app, ["source", "add", "sales", "data", "--project", str(project)]).exit_code == 0
    (project / ".joinlint" / "model.yaml").write_text(
        """version: 1
entities:
  children:
    source: sales
    object: children.csv
    grain:
      keys: [id]
      status: confirmed
  parents:
    source: sales
    object: parents.csv
    grain:
      keys: [id]
      status: confirmed
relationships:
  - id: child_to_parent
    from: children.parent_id
    to: parents.id
    cardinality: many_to_one
    status: confirmed
""",
        encoding="utf-8",
    )

    valid = runner.invoke(app, ["validate", "--project", str(project), "--json"])
    with (data / "parents.csv").open("a", encoding="utf-8") as source:
        source.write("a\n")
    drifted = runner.invoke(app, ["validate", "--project", str(project), "--json"])

    assert valid.exit_code == 0, valid.output
    assert json.loads(valid.output)["status"] == "findings"
    assert drifted.exit_code == 1
    assert "CARDINALITY_DRIFT" in {
        finding["code"] for finding in json.loads(drifted.output)["findings"]
    }


def test_check_reports_missing_baseline_as_inconclusive(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert runner.invoke(app, ["init", "--project", str(project)]).exit_code == 0

    result = runner.invoke(app, ["check", "--project", str(project), "--json"])

    assert result.exit_code == 3
    assert json.loads(result.output)["error"]["code"] == "BASELINE_MISSING"


def test_report_requires_fresh_generated_evidence(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    data = project / "data"
    data.mkdir()
    (data / "records.csv").write_text("id\n1\n", encoding="utf-8")
    assert runner.invoke(app, ["init", "--project", str(project)]).exit_code == 0
    assert runner.invoke(app, ["source", "add", "sales", "data", "--project", str(project)]).exit_code == 0
    assert runner.invoke(app, ["scan", "--project", str(project)]).exit_code == 0

    current = runner.invoke(app, ["report", "--project", str(project), "--json"])
    with (data / "records.csv").open("a", encoding="utf-8") as source:
        source.write("2\n")
    stale = runner.invoke(app, ["report", "--project", str(project), "--json"])

    assert current.exit_code == 0, current.output
    assert (project / ".joinlint" / "generated" / "report.html").exists()
    assert stale.exit_code == 3
    assert json.loads(stale.output)["error"]["code"] == "EVIDENCE_STALE"


def test_report_lists_sanitized_sources_relationships_candidates_and_risks(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    data = project / "data"
    data.mkdir()
    (data / "parents.csv").write_text("id\na\nb\n", encoding="utf-8")
    (data / "children.csv").write_text(
        "id,parent_id\n1,a\n2,a\n3,exclusive-raw-value\n", encoding="utf-8"
    )
    assert runner.invoke(app, ["init", "--project", str(project)]).exit_code == 0
    assert runner.invoke(app, ["source", "add", "sales", "data", "--project", str(project)]).exit_code == 0
    (project / ".joinlint" / "model.yaml").write_text(
        """version: 1
entities:
  children:
    source: sales
    object: children.csv
    grain:
      keys: [id]
      status: confirmed
  parents:
    source: sales
    object: parents.csv
    grain:
      keys: [id]
      status: confirmed
relationships:
  - id: child_to_parent
    from: children.parent_id
    to: parents.id
    cardinality: many_to_one
    status: confirmed
""",
        encoding="utf-8",
    )
    assert runner.invoke(app, ["scan", "--project", str(project)]).exit_code == 0

    result = runner.invoke(app, ["report", "--project", str(project), "--json"])
    report = (project / ".joinlint" / "generated" / "report.html").read_text(encoding="utf-8")

    assert result.exit_code == 0, result.output
    assert "Sources" in report
    assert "Confirmed relationships" in report
    assert "Candidates and evidence" in report
    assert "Validation evidence" in report
    assert "ORPHAN_CHILD_ROW" in report
    assert "exclusive-raw-value" not in report


def test_report_rejects_generated_evidence_from_a_different_policy(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    data = project / "data"
    data.mkdir()
    (data / "records.csv").write_text("id\n1\n", encoding="utf-8")
    assert runner.invoke(app, ["init", "--project", str(project)]).exit_code == 0
    assert runner.invoke(app, ["source", "add", "sales", "data", "--project", str(project)]).exit_code == 0
    assert runner.invoke(app, ["scan", "--project", str(project)]).exit_code == 0
    manifest_path = project / ".joinlint" / "generated" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["policy_version"] = "candidate-next"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = runner.invoke(app, ["report", "--project", str(project), "--json"])

    assert result.exit_code == 3
    assert json.loads(result.output)["error"]["code"] == "EVIDENCE_STALE"


def test_source_set_invalidates_generated_rejections_and_baseline(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    for name in ("data", "replacement"):
        directory = project / name
        directory.mkdir()
        (directory / "records.csv").write_text("id\n1\n", encoding="utf-8")
    assert runner.invoke(app, ["init", "--project", str(project)]).exit_code == 0
    assert runner.invoke(app, ["source", "add", "sales", "data", "--project", str(project)]).exit_code == 0
    assert runner.invoke(app, ["scan", "--project", str(project)]).exit_code == 0
    assert runner.invoke(app, ["baseline", "update", "--project", str(project)]).exit_code == 0
    rejections = project / ".joinlint" / "state" / "rejections.json"
    rejections.write_text('{"version":1,"rejections":["old"]}', encoding="utf-8")

    result = runner.invoke(app, ["source", "set", "sales", "replacement", "--project", str(project)])

    assert result.exit_code == 0, result.output
    assert not (project / ".joinlint" / "generated").exists()
    assert not (project / ".joinlint" / "baseline.json").exists()
    assert not rejections.exists()
