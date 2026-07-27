from __future__ import annotations

import argparse
import binascii
import hashlib
import json
import os
import shutil
import struct
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, Mapping

from benchmarks.formal_eval.bird_dataset import (
    OFFICIAL_TRAIN_SOURCE,
    BirdArchiveSource,
    BirdSourceExpectation,
    prepare_bird_subset_from_components,
)


_CHUNK_BYTES = 8 * 1024 * 1024
_HEADER_PROBE_BYTES = 4096
_LOCAL_FILE_HEADER = struct.Struct("<4s5H3I2H")
_LOCAL_FILE_SIGNATURE = b"PK\x03\x04"


@dataclass(frozen=True)
class BirdRemoteMember:
    name: str
    local_header_offset: int
    compressed_size: int
    uncompressed_size: int
    crc32: int


OFFICIAL_DATABASES_MEMBER = BirdRemoteMember(
    name="train/train_databases.zip",
    local_header_offset=302,
    compressed_size=8_918_702_073,
    uncompressed_size=9_347_158_408,
    crc32=0xE28EE04B,
)
OFFICIAL_TASKS_MEMBER = BirdRemoteMember(
    name="train/train.json",
    local_header_offset=8_918_703_390,
    compressed_size=583_528,
    uncompressed_size=4_321_661,
    crc32=0xE3B16A33,
)
OFFICIAL_TABLES_MEMBER = BirdRemoteMember(
    name="train/train_tables.json",
    local_header_offset=8_919_494_763,
    compressed_size=47_263,
    uncompressed_size=726_087,
    crc32=0xA57FCD33,
)


def prepare_streaming_subset(
    work_dir: Path,
    destination: Path,
    *,
    database_ids: tuple[str, ...],
    source_expectation: BirdSourceExpectation = OFFICIAL_TRAIN_SOURCE,
    database_member: BirdRemoteMember = OFFICIAL_DATABASES_MEMBER,
    tasks_member: BirdRemoteMember = OFFICIAL_TASKS_MEMBER,
    tables_member: BirdRemoteMember = OFFICIAL_TABLES_MEMBER,
    opener: Callable[[urllib.request.Request], BinaryIO] = urllib.request.urlopen,
) -> dict[str, Any]:
    if work_dir.exists() or work_dir.is_symlink():
        raise ValueError("streaming work directory already exists")
    work_dir.parent.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir()
    nested_archive = work_dir / "train_databases.zip"
    try:
        source = stream_archive_member(
            source_expectation,
            database_member,
            nested_archive,
            opener=opener,
        )
        tasks = read_json_member(
            source_expectation,
            tasks_member,
            opener=opener,
        )
        tables = read_json_member(
            source_expectation,
            tables_member,
            opener=opener,
        )
        return prepare_bird_subset_from_components(
            nested_archive,
            destination,
            tasks=tasks,
            tables=tables,
            database_ids=database_ids,
            source=source,
            min_tasks_per_database=5,
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def stream_archive_member(
    source: BirdSourceExpectation,
    member: BirdRemoteMember,
    destination: Path,
    *,
    opener: Callable[[urllib.request.Request], BinaryIO] = urllib.request.urlopen,
) -> BirdArchiveSource:
    if destination.exists() or destination.is_symlink():
        raise ValueError("stream destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    data_start = _member_data_start(source, member, opener=opener)
    data_end = data_start + member.compressed_size
    if data_end > source.size:
        raise ValueError("member range exceeds the source archive")

    partial = destination.with_suffix(destination.suffix + ".part")
    partial.unlink(missing_ok=True)
    source_digest = hashlib.sha256()
    decompressor = zlib.decompressobj(-15)
    source_size = 0
    compressed_size = 0
    output_size = 0
    output_crc = 0
    request = urllib.request.Request(
        source.url,
        headers={"If-Match": source.etag, "User-Agent": "JoinLint-formal-eval/2"},
    )
    try:
        with opener(request) as response:
            _validate_response(response, source, expected_status=200)
            with partial.open("xb") as output:
                while chunk := response.read(_CHUNK_BYTES):
                    chunk_start = source_size
                    chunk_end = chunk_start + len(chunk)
                    source_digest.update(chunk)
                    overlap_start = max(chunk_start, data_start)
                    overlap_end = min(chunk_end, data_end)
                    if overlap_start < overlap_end:
                        compressed = chunk[
                            overlap_start - chunk_start : overlap_end - chunk_start
                        ]
                        compressed_size += len(compressed)
                        decompressed = decompressor.decompress(compressed)
                        output.write(decompressed)
                        output_size += len(decompressed)
                        output_crc = binascii.crc32(decompressed, output_crc)
                    source_size = chunk_end
                tail = decompressor.flush()
                output.write(tail)
                output_size += len(tail)
                output_crc = binascii.crc32(tail, output_crc)
                output.flush()
                os.fsync(output.fileno())
        if source_size != source.size:
            raise ValueError("source archive download is incomplete")
        if compressed_size != member.compressed_size:
            raise ValueError("compressed member download is incomplete")
        if not decompressor.eof or decompressor.unused_data:
            raise ValueError("compressed member stream is invalid")
        if output_size != member.uncompressed_size:
            raise ValueError("decompressed member size changed")
        if output_crc & 0xFFFFFFFF != member.crc32:
            raise ValueError("decompressed member CRC changed")
        os.replace(partial, destination)
        return BirdArchiveSource(
            url=source.url,
            size=source_size,
            etag=source.etag,
            last_modified=source.last_modified,
            sha256=source_digest.hexdigest(),
        )
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def read_json_member(
    source: BirdSourceExpectation,
    member: BirdRemoteMember,
    *,
    opener: Callable[[urllib.request.Request], BinaryIO] = urllib.request.urlopen,
) -> Any:
    data_start = _member_data_start(source, member, opener=opener)
    data_end = data_start + member.compressed_size - 1
    request = urllib.request.Request(
        source.url,
        headers={
            "If-Match": source.etag,
            "Range": f"bytes={data_start}-{data_end}",
            "User-Agent": "JoinLint-formal-eval/2",
        },
    )
    with opener(request) as response:
        _validate_response(
            response,
            source,
            expected_status=206,
            expected_range=(data_start, data_end),
        )
        compressed = response.read(member.compressed_size + 1)
    if len(compressed) != member.compressed_size:
        raise ValueError("compressed metadata member download is incomplete")
    try:
        payload = zlib.decompress(compressed, -15)
    except zlib.error as error:
        raise ValueError("compressed metadata member is invalid") from error
    if len(payload) != member.uncompressed_size:
        raise ValueError("metadata member size changed")
    if binascii.crc32(payload) & 0xFFFFFFFF != member.crc32:
        raise ValueError("metadata member CRC changed")
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("metadata member is not valid JSON") from error


def _member_data_start(
    source: BirdSourceExpectation,
    member: BirdRemoteMember,
    *,
    opener: Callable[[urllib.request.Request], BinaryIO],
) -> int:
    probe_end = min(
        member.local_header_offset + _HEADER_PROBE_BYTES - 1,
        source.size - 1,
    )
    probe_size = probe_end - member.local_header_offset + 1
    request = urllib.request.Request(
        source.url,
        headers={
            "If-Match": source.etag,
            "Range": f"bytes={member.local_header_offset}-{probe_end}",
            "User-Agent": "JoinLint-formal-eval/2",
        },
    )
    with opener(request) as response:
        _validate_response(
            response,
            source,
            expected_status=206,
            expected_range=(member.local_header_offset, probe_end),
        )
        probe = response.read(probe_size + 1)
    if len(probe) != probe_size:
        raise ValueError("local member header probe is incomplete")
    if len(probe) < _LOCAL_FILE_HEADER.size:
        raise ValueError("local member header is truncated")
    header = _LOCAL_FILE_HEADER.unpack_from(probe)
    signature, flags, method = header[0], header[2], header[3]
    name_length, extra_length = header[9], header[10]
    if signature != _LOCAL_FILE_SIGNATURE or method != 8:
        raise ValueError("local member header is unsupported")
    header_size = _LOCAL_FILE_HEADER.size + name_length + extra_length
    if header_size > len(probe):
        raise ValueError("local member header exceeds the bounded probe")
    encoding = "utf-8" if flags & 0x800 else "cp437"
    try:
        name = probe[_LOCAL_FILE_HEADER.size : _LOCAL_FILE_HEADER.size + name_length].decode(
            encoding
        )
    except UnicodeDecodeError as error:
        raise ValueError("local member name is invalid") from error
    if name != member.name:
        raise ValueError("local member identity changed")
    return member.local_header_offset + header_size


def _validate_response(
    response: BinaryIO,
    source: BirdSourceExpectation,
    *,
    expected_status: int,
    expected_range: tuple[int, int] | None = None,
) -> None:
    status = getattr(response, "status", None)
    if status != expected_status:
        raise ValueError("source server returned an unexpected HTTP status")
    headers: Mapping[str, str] = response.headers
    if headers.get("ETag", "").strip() != source.etag:
        raise ValueError("source archive ETag changed")
    if headers.get("Last-Modified", "").strip() != source.last_modified:
        raise ValueError("source archive Last-Modified changed")
    if expected_range is None:
        if headers.get("Content-Length", "").strip() != str(source.size):
            raise ValueError("source archive size changed")
        return
    start, end = expected_range
    if headers.get("Content-Length", "").strip() != str(end - start + 1):
        raise ValueError("source range size changed")
    expected_content_range = f"bytes {start}-{end}/{source.size}"
    if headers.get("Content-Range", "").strip() != expected_content_range:
        raise ValueError("source server returned an unexpected byte range")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare BIRD Train without storing the outer ZIP")
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--database-ids", required=True)
    arguments = parser.parse_args(argv)
    database_ids = tuple(
        value.strip() for value in arguments.database_ids.split(",") if value.strip()
    )
    manifest = prepare_streaming_subset(
        arguments.work_dir,
        arguments.destination,
        database_ids=database_ids,
    )
    print(
        json.dumps(
            {
                "source_sha256": manifest["source"]["sha256"],
                "eligible_task_count": manifest["selection"]["eligible_task_count"],
                "selected_database_count": len(manifest["selected_databases"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
