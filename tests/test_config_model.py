from __future__ import annotations

from pathlib import Path

import pytest

from joinlint.config import add_source, load_config, remove_source, set_source_path
from joinlint.errors import JoinLintError
from joinlint.model import load_model


def test_source_set_preserves_kind(project: Path) -> None:
    add_source(project, "sales", "data", "csv_directory")

    with pytest.raises(JoinLintError) as captured:
        set_source_path(project, "sales", "data/app.sqlite")

    assert captured.value.code == "SOURCE_KIND_MISMATCH"
    assert load_config(project).sources["sales"].path.as_posix() == "data"


def test_remove_referenced_source_is_rejected(project: Path) -> None:
    add_source(project, "sales", "data", "csv_directory")
    (project / ".joinlint" / "model.yaml").write_text(
        """version: 1
entities:
  orders:
    source: sales
    object: orders.csv
    grain:
      keys: [order_id]
      status: confirmed
relationships: []
""",
        encoding="utf-8",
    )

    with pytest.raises(JoinLintError) as captured:
        remove_source(project, "sales")

    assert captured.value.code == "SOURCE_IN_USE"
    assert "sales" in load_config(project).sources


def test_config_rejects_unknown_fields_and_parent_paths(project: Path) -> None:
    config = project / ".joinlint" / "config.yaml"
    config.write_text("version: 1\nunknown: true\nsources: {}\n", encoding="utf-8")

    with pytest.raises(JoinLintError) as captured:
        load_config(project)
    assert captured.value.code == "MALFORMED_CONFIG"

    config.write_text(
        """version: 1
sources:
  sales:
    kind: csv_directory
    path: data
  sales:
    kind: csv_directory
    path: other-data
""",
        encoding="utf-8",
    )
    with pytest.raises(JoinLintError) as captured:
        load_config(project)
    assert captured.value.code == "MALFORMED_CONFIG"

    config.write_text(
        """version: 1
sources:
  sales:
    kind: csv_directory
    path: ../data
    limits:
      max_source_bytes: 10
      max_scan_seconds: 1
""",
        encoding="utf-8",
    )
    with pytest.raises(JoinLintError) as captured:
        load_config(project)
    assert captured.value.code == "MALFORMED_CONFIG"


def test_model_requires_exactly_one_confirmed_grain_key(project: Path) -> None:
    model = project / ".joinlint" / "model.yaml"
    model.write_text(
        """version: 1
entities:
  orders:
    source: sales
    object: orders.csv
    grain:
      keys: [order_id, revision]
      status: confirmed
relationships: []
""",
        encoding="utf-8",
    )

    with pytest.raises(JoinLintError) as captured:
        load_model(project)
    assert captured.value.code == "MALFORMED_MODEL"


def test_source_add_rejects_unsafe_path_argument(project: Path) -> None:
    with pytest.raises(JoinLintError) as captured:
        add_source(project, "sales", "../data", "csv_directory")

    assert captured.value.code == "INVALID_ARGUMENT"
    assert load_config(project).sources == {}
