from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import zipfile
from pathlib import Path
from urllib.request import Request

import pytest

from benchmarks.formal_eval.bird_dataset import (
    BirdSourceExpectation,
    verify_bird_subset,
)
from benchmarks.formal_eval.bird_stream import (
    BirdRemoteMember,
    prepare_streaming_subset,
    read_json_member,
    stream_archive_member,
)


def test_streaming_preparation_never_stores_the_outer_archive(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    work = tmp_path / "work"
    destination = tmp_path / "subset"

    manifest = prepare_streaming_subset(
        work,
        destination,
        database_ids=("alpha",),
        source_expectation=fixture.source,
        database_member=fixture.databases,
        tasks_member=fixture.tasks,
        tables_member=fixture.tables,
        opener=fixture.http,
    )

    assert not work.exists()
    assert not (tmp_path / "train.zip").exists()
    assert (destination / "databases" / "alpha.sqlite").is_file()
    assert manifest["source"]["sha256"] == hashlib.sha256(fixture.payload).hexdigest()
    assert verify_bird_subset(destination) == manifest
    assert fixture.http.full_requests == 1
    assert fixture.http.range_requests == 5


def test_streamed_member_fails_closed_on_source_header_drift(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture.http.etag = '"changed"'
    destination = tmp_path / "nested.zip"

    with pytest.raises(ValueError, match="ETag changed"):
        stream_archive_member(
            fixture.source,
            fixture.databases,
            destination,
            opener=fixture.http,
        )

    assert not destination.exists()
    assert not destination.with_suffix(".zip.part").exists()


def test_range_reader_rejects_changed_member_identity(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    wrong = BirdRemoteMember(
        name="train/different.json",
        local_header_offset=fixture.tasks.local_header_offset,
        compressed_size=fixture.tasks.compressed_size,
        uncompressed_size=fixture.tasks.uncompressed_size,
        crc32=fixture.tasks.crc32,
    )

    with pytest.raises(ValueError, match="identity changed"):
        read_json_member(fixture.source, wrong, opener=fixture.http)


class _Fixture:
    def __init__(
        self,
        payload: bytes,
        source: BirdSourceExpectation,
        databases: BirdRemoteMember,
        tasks: BirdRemoteMember,
        tables: BirdRemoteMember,
    ) -> None:
        self.payload = payload
        self.source = source
        self.databases = databases
        self.tasks = tasks
        self.tables = tables
        self.http = _HTTP(payload, source.etag, source.last_modified)


class _HTTP:
    def __init__(self, payload: bytes, etag: str, last_modified: str) -> None:
        self.payload = payload
        self.etag = etag
        self.last_modified = last_modified
        self.full_requests = 0
        self.range_requests = 0

    def __call__(self, request: Request) -> _Response:
        range_header = request.headers.get("Range")
        if range_header is None:
            self.full_requests += 1
            return _Response(
                self.payload,
                status=200,
                headers={
                    "Content-Length": str(len(self.payload)),
                    "ETag": self.etag,
                    "Last-Modified": self.last_modified,
                },
            )
        self.range_requests += 1
        start_text, end_text = range_header.removeprefix("bytes=").split("-")
        start, end = int(start_text), int(end_text)
        return _Response(
            self.payload[start : end + 1],
            status=206,
            headers={
                "Content-Length": str(end - start + 1),
                "Content-Range": f"bytes {start}-{end}/{len(self.payload)}",
                "ETag": self.etag,
                "Last-Modified": self.last_modified,
            },
        )


class _Response(io.BytesIO):
    def __init__(self, payload: bytes, *, status: int, headers: dict[str, str]) -> None:
        super().__init__(payload)
        self.status = status
        self.headers = headers

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _fixture(tmp_path: Path) -> _Fixture:
    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "train_databases/alpha/alpha.sqlite",
            _sqlite_bytes(tmp_path),
        )
    tasks = [
        {
            "db_id": "alpha",
            "question": f"joined {index}",
            "evidence": "",
            "SQL": "SELECT * FROM child JOIN parent ON child.parent_id = parent.id",
        }
        for index in range(5)
    ]
    tables = [{"db_id": "alpha", "table_names_original": ["child", "parent"]}]
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        archive.writestr("train/train_databases.zip", nested.getvalue())
        archive.writestr("train/train.json", json.dumps(tasks))
        archive.writestr("train/train_tables.json", json.dumps(tables))
    payload = outer.getvalue()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        databases = _member(archive.getinfo("train/train_databases.zip"))
        task_member = _member(archive.getinfo("train/train.json"))
        table_member = _member(archive.getinfo("train/train_tables.json"))
    source = BirdSourceExpectation(
        url="https://example.test/train.zip",
        size=len(payload),
        etag='"fixture"',
        last_modified="fixture-date",
        databases_member_size=databases.uncompressed_size,
        databases_member_crc32=databases.crc32,
    )
    return _Fixture(payload, source, databases, task_member, table_member)


def _member(info: zipfile.ZipInfo) -> BirdRemoteMember:
    return BirdRemoteMember(
        name=info.filename,
        local_header_offset=info.header_offset,
        compressed_size=info.compress_size,
        uncompressed_size=info.file_size,
        crc32=info.CRC,
    )


def _sqlite_bytes(tmp_path: Path) -> bytes:
    path = tmp_path / "alpha.sqlite"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE child(id INTEGER PRIMARY KEY, parent_id INTEGER)")
        connection.commit()
    finally:
        connection.close()
    return path.read_bytes()
