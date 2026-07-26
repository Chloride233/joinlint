from __future__ import annotations

import io
import json
import sqlite3
import stat
import zipfile
from pathlib import Path

import pytest
import yaml

from benchmarks.formal_eval.bird_dataset import (
    OFFICIAL_TRAIN_SOURCE,
    BirdArchiveSource,
    download_archive,
    is_eligible_bird_sql,
    prepare_bird_subset,
    select_eligible_tasks,
    verify_bird_subset,
)


def test_bird_sql_eligibility_is_independent_and_conservative() -> None:
    assert is_eligible_bird_sql(
        "SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id"
    )
    assert is_eligible_bird_sql(
        "SELECT o.id FROM orders o LEFT JOIN customers c ON c.id = o.customer_id"
    )
    assert not is_eligible_bird_sql("SELECT * FROM orders")
    assert not is_eligible_bird_sql(
        "SELECT * FROM orders o CROSS JOIN customers c"
    )
    assert not is_eligible_bird_sql(
        "SELECT * FROM orders o JOIN customers c ON o.customer_id > c.id"
    )
    assert not is_eligible_bird_sql(
        "SELECT * FROM orders o JOIN customers c "
        "ON o.customer_id = c.id OR o.owner_id = c.id"
    )


def test_select_eligible_tasks_preserves_source_indices_and_database_boundary() -> None:
    tasks = [
        {
            "db_id": "alpha",
            "question": "single table",
            "evidence": "",
            "SQL": "SELECT * FROM a",
        },
        {
            "db_id": "alpha",
            "question": "joined",
            "evidence": "hint",
            "SQL": "SELECT * FROM a JOIN b ON a.b_id = b.id",
        },
        {
            "db_id": "beta",
            "question": "other database",
            "evidence": "",
            "SQL": "SELECT * FROM x JOIN y ON x.y_id = y.id",
        },
    ]

    selected = select_eligible_tasks(tasks, ("alpha",))

    assert selected == (
        {
            "task_index": 1,
            "db_id": "alpha",
            "question": "joined",
            "evidence": "hint",
            "SQL": "SELECT * FROM a JOIN b ON a.b_id = b.id",
        },
    )


def test_prepare_subset_exports_only_selected_database_and_metadata(tmp_path: Path) -> None:
    outer = _bird_outer_archive(tmp_path, database_ids=("alpha", "beta"))
    output = tmp_path / "output"
    source = _source_for(outer)

    manifest = prepare_bird_subset(
        outer,
        output,
        database_ids=("alpha",),
        source=source,
        min_tasks_per_database=1,
    )

    assert (output / "databases" / "alpha.sqlite").is_file()
    assert not (output / "databases" / "beta.sqlite").exists()
    candidates = json.loads((output / "candidate-tasks.json").read_text())
    schemas = json.loads((output / "selected-tables.json").read_text())
    assert {task["db_id"] for task in candidates} == {"alpha"}
    assert {schema["db_id"] for schema in schemas} == {"alpha"}
    assert manifest["source"]["sha256"] == source.sha256
    assert manifest["selected_databases"][0]["database_id"] == "alpha"
    assert manifest["selection"]["uses_joinlint_output"] is False
    assert verify_bird_subset(output) == manifest

    (output / "candidate-tasks.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_bird_subset(output)


def test_prepare_subset_rejects_archive_traversal_before_writing_output(
    tmp_path: Path,
) -> None:
    outer = _bird_outer_archive(tmp_path, database_ids=("alpha",), unsafe_name="../escape")
    output = tmp_path / "output"

    with pytest.raises(ValueError, match="unsafe archive member"):
        prepare_bird_subset(
            outer,
            output,
            database_ids=("alpha",),
            source=_source_for(outer),
            min_tasks_per_database=1,
        )

    assert not output.exists()
    assert not (tmp_path / "escape").exists()


def test_prepare_subset_rejects_symlink_and_duplicate_members(tmp_path: Path) -> None:
    symlink_outer = _bird_outer_archive(tmp_path, database_ids=("alpha",), symlink=True)
    with pytest.raises(ValueError, match="symlink"):
        prepare_bird_subset(
            symlink_outer,
            tmp_path / "symlink-output",
            database_ids=("alpha",),
            source=_source_for(symlink_outer),
            min_tasks_per_database=1,
        )

    duplicate_outer = _bird_outer_archive(
        tmp_path,
        database_ids=("alpha",),
        duplicate=True,
        name="duplicate.zip",
    )
    with pytest.raises(ValueError, match="duplicate archive member"):
        prepare_bird_subset(
            duplicate_outer,
            tmp_path / "duplicate-output",
            database_ids=("alpha",),
            source=_source_for(duplicate_outer),
            min_tasks_per_database=1,
        )


def test_prepare_subset_rejects_missing_database_and_existing_destination(
    tmp_path: Path,
) -> None:
    outer = _bird_outer_archive(
        tmp_path,
        database_ids=("alpha",),
        metadata_ids=("alpha", "beta"),
    )
    source = _source_for(outer)
    with pytest.raises(ValueError, match="selected database is unavailable"):
        prepare_bird_subset(
            outer,
            tmp_path / "missing",
            database_ids=("beta",),
            source=source,
            min_tasks_per_database=1,
        )

    destination = tmp_path / "existing"
    destination.mkdir()
    with pytest.raises(ValueError, match="destination already exists"):
        prepare_bird_subset(
            outer,
            destination,
            database_ids=("alpha",),
            source=source,
            min_tasks_per_database=1,
        )


def test_download_archive_is_atomic_and_checks_headers(tmp_path: Path) -> None:
    destination = tmp_path / "train.zip"
    response = _Response(b"official", etag='"etag-1"')

    record = download_archive(
        "https://example.test/train.zip",
        destination,
        expected_size=8,
        expected_etag='"etag-1"',
        opener=lambda _request: response,
    )

    assert destination.read_bytes() == b"official"
    assert record.size == 8
    assert record.etag == '"etag-1"'
    assert len(record.sha256) == 64
    assert not destination.with_suffix(".zip.part").exists()

    bad_destination = tmp_path / "bad.zip"
    with pytest.raises(ValueError, match="ETag"):
        download_archive(
            "https://example.test/train.zip",
            bad_destination,
            expected_size=8,
            expected_etag='"expected"',
            opener=lambda _request: _Response(b"official", etag='"changed"'),
        )
    assert not bad_destination.exists()
    assert not bad_destination.with_suffix(".zip.part").exists()

    with pytest.raises(ValueError, match="Last-Modified"):
        download_archive(
            "https://example.test/train.zip",
            tmp_path / "changed-date.zip",
            expected_size=8,
            expected_etag='"etag-1"',
            expected_last_modified="changed",
            opener=lambda _request: _Response(b"official", etag='"etag-1"'),
        )


def test_modal_wrapper_is_large_disk_explicit_and_never_uploads_repository() -> None:
    source = Path("benchmarks/formal_eval/bird_modal.py").read_text(encoding="utf-8")

    assert "ephemeral_disk=25_000" in source
    assert "add_local_file" in source
    assert "add_local_dir" not in source
    assert "OFFICIAL_TRAIN_SOURCE" in source
    assert "create_if_missing=True" in source
    assert "@app.local_entrypoint()" in source


def test_official_train_source_is_frozen_to_observed_headers() -> None:
    assert OFFICIAL_TRAIN_SOURCE.size == 8_919_543_554
    assert OFFICIAL_TRAIN_SOURCE.etag == '"1FBA2891BD215A42CE1004736AB7F47F-400"'
    assert OFFICIAL_TRAIN_SOURCE.last_modified == "Tue, 11 Jul 2023 06:13:29 GMT"


def test_bird_preparation_workflow_is_approval_and_paid_gated() -> None:
    path = Path(".github/workflows/prepare-bird-subset.yml")
    source = path.read_text(encoding="utf-8")
    workflow = yaml.load(source, Loader=yaml.BaseLoader)
    job = workflow["jobs"]["prepare"]

    assert job["runs-on"] == "ubuntu-latest"
    assert job["environment"] == "formal-evaluation"
    assert "confirm_paid" in workflow["on"]["workflow_dispatch"]["inputs"]
    assert "benchmarks/formal_eval/bird_modal.py" in source
    assert "benchmarks.formal_eval.bird_dataset" in source
    assert "actions/upload-artifact@v4" in source
    assert "modal volume rm --recursive" in source
    assert "if: always()" in source
    assert "OPENAI_API_KEY" not in source
    assert "ANTHROPIC_API_KEY" not in source


class _Response(io.BytesIO):
    def __init__(self, payload: bytes, *, etag: str) -> None:
        super().__init__(payload)
        self.headers = {
            "Content-Length": str(len(payload)),
            "ETag": etag,
            "Last-Modified": "Tue, 11 Jul 2023 06:13:29 GMT",
        }

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _source_for(path: Path) -> BirdArchiveSource:
    import hashlib

    payload = path.read_bytes()
    return BirdArchiveSource(
        url="https://example.test/train.zip",
        size=len(payload),
        etag='"fixture"',
        last_modified="fixture",
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _bird_outer_archive(
    tmp_path: Path,
    *,
    database_ids: tuple[str, ...],
    metadata_ids: tuple[str, ...] | None = None,
    unsafe_name: str | None = None,
    symlink: bool = False,
    duplicate: bool = False,
    name: str = "train.zip",
) -> Path:
    nested_bytes = io.BytesIO()
    with zipfile.ZipFile(nested_bytes, "w", zipfile.ZIP_DEFLATED) as nested:
        for database_id in database_ids:
            member = f"train_databases/{database_id}/{database_id}.sqlite"
            nested.writestr(member, _sqlite_bytes(tmp_path, database_id))
        if unsafe_name is not None:
            nested.writestr(unsafe_name, b"escape")
        if symlink:
            info = zipfile.ZipInfo("train_databases/link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            nested.writestr(info, "target")
        if duplicate:
            member = "train_databases/alpha/alpha.sqlite"
            with pytest.warns(UserWarning, match="Duplicate name"):
                nested.writestr(member, _sqlite_bytes(tmp_path, "duplicate"))

    tasks = []
    tables = []
    for database_id in metadata_ids or database_ids:
        tasks.extend(
            [
                {
                    "db_id": database_id,
                    "question": "joined",
                    "evidence": "",
                    "SQL": "SELECT * FROM child JOIN parent ON child.parent_id = parent.id",
                },
                {
                    "db_id": database_id,
                    "question": "single",
                    "evidence": "",
                    "SQL": "SELECT * FROM child",
                },
            ]
        )
        tables.append({"db_id": database_id, "table_names_original": ["child", "parent"]})

    outer = tmp_path / name
    with zipfile.ZipFile(outer, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        archive.writestr("train/train_databases.zip", nested_bytes.getvalue())
        archive.writestr("train/train.json", json.dumps(tasks))
        archive.writestr("train/train_tables.json", json.dumps(tables))
    return outer


def _sqlite_bytes(tmp_path: Path, name: str) -> bytes:
    path = tmp_path / f"{name}.sqlite"
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE child(id INTEGER PRIMARY KEY, parent_id INTEGER)")
        connection.commit()
    finally:
        connection.close()
    return path.read_bytes()
