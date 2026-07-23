from __future__ import annotations

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
