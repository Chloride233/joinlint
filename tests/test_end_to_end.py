from __future__ import annotations

import json
import hashlib
import shutil
from pathlib import Path

from typer.testing import CliRunner

from joinlint.cli import app


FIXTURE = Path(__file__).parent / "fixtures" / "chinook" / "chinook.sqlite"
runner = CliRunner()


def test_chinook_fixture_sha256_is_pinned() -> None:
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == (
        "bdf635be69850bd3be09c9a2dbeef7ddfb80036bd3ef3381383cd03b61e4a61a"
    )


def test_chinook_sqlite_workflow_from_init_to_report(tmp_path: Path) -> None:
    project = tmp_path / "project"
    data = project / "data"
    data.mkdir(parents=True)
    shutil.copy2(FIXTURE, data / "chinook.sqlite")

    assert runner.invoke(app, ["init", "--project", str(project)]).exit_code == 0
    assert runner.invoke(app, ["source", "add", "chinook", "data/chinook.sqlite", "--project", str(project)]).exit_code == 0
    assert runner.invoke(app, ["scan", "--project", str(project)]).exit_code == 0
    _write_chinook_entities(project)
    assert runner.invoke(app, ["scan", "--project", str(project)]).exit_code == 0

    candidates = runner.invoke(app, ["candidates", "--project", str(project), "--json"])
    candidate_id = next(
        candidate["id"]
        for candidate in json.loads(candidates.output)["data"]["candidates"]
        if candidate["from_endpoint"] == "invoice_lines.InvoiceId"
        and candidate["to_endpoint"] == "invoices.InvoiceId"
    )
    assert candidates.exit_code == 0, candidates.output
    assert runner.invoke(app, ["accept", candidate_id, "--project", str(project)]).exit_code == 0

    validate = runner.invoke(app, ["validate", "--project", str(project), "--json"])
    assert validate.exit_code == 0, validate.output
    assert runner.invoke(app, ["baseline", "update", "--project", str(project)]).exit_code == 0
    check = runner.invoke(app, ["check", "--project", str(project), "--json"])
    assert check.exit_code == 0, check.output
    assert runner.invoke(app, ["scan", "--project", str(project)]).exit_code == 0
    assert runner.invoke(app, ["report", "--project", str(project)]).exit_code == 0
    report = (project / ".joinlint" / "generated" / "report.html").read_text(encoding="utf-8")
    assert "InvoiceId (integer" in report


def _write_chinook_entities(project: Path) -> None:
    (project / ".joinlint" / "model.yaml").write_text(
        """version: 1
entities:
  invoices:
    source: chinook
    object: Invoice
    grain:
      keys: [InvoiceId]
      status: confirmed
  invoice_lines:
    source: chinook
    object: InvoiceLine
    grain:
      keys: [InvoiceLineId]
      status: confirmed
relationships: []
""",
        encoding="utf-8",
    )
