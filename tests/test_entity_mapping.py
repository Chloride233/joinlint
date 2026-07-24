from __future__ import annotations

from pathlib import Path

from joinlint.config import add_source
from joinlint.model import load_model
from joinlint.scanner import scan_snapshot
from joinlint.snapshots import snapshot_source
from joinlint.validation import validate_relationship


def test_validation_resolves_entity_aliases_to_csv_objects(project: Path) -> None:
    (project / "data").mkdir()
    (project / "data" / "parents.csv").write_text("id\na\nb\n", encoding="utf-8")
    (project / "data" / "children.csv").write_text("id,parent_id\n1,a\n2,a\n", encoding="utf-8")
    add_source(project, "sales", "data", "csv_directory")
    (project / ".joinlint" / "model.yaml").write_text(
        """version: 1
entities:
  parent_records:
    source: sales
    object: parents.csv
    grain:
      keys: [id]
      status: confirmed
  child_records:
    source: sales
    object: children.csv
    grain:
      keys: [id]
      status: confirmed
relationships:
  - id: child_to_parent
    from: child_records.parent_id
    to: parent_records.id
    cardinality: many_to_one
    status: confirmed
""",
        encoding="utf-8",
    )
    model = load_model(project)

    with snapshot_source(project, "sales") as snapshot:
        result = validate_relationship(model.relationships[0], snapshot, scan_snapshot(snapshot), model)

    assert result.observed_cardinality == "many_to_one"
