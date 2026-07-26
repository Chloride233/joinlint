from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath

import modal


app = modal.App("joinlint-bird-prepare")
output_volume = modal.Volume.from_name("joinlint-bird-prep", create_if_missing=True)
image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("rfc8785>=0.1.4", "sqlglot==30.13.0")
    .add_local_file(
        "benchmarks/formal_eval/bird_dataset.py",
        remote_path="/root/bird_dataset.py",
        copy=True,
    )
)


@app.function(
    image=image,
    timeout=14_400,
    memory=4_096,
    ephemeral_disk=25_000,
    volumes={"/output": output_volume},
)
def prepare_official_train(
    database_ids: tuple[str, ...], output_prefix: str
) -> dict[str, object]:
    from bird_dataset import (
        OFFICIAL_TRAIN_SOURCE,
        download_archive,
        prepare_bird_subset,
    )

    prefix = _validated_output_prefix(output_prefix)
    work = Path("/tmp/joinlint-bird-prepare")
    work.mkdir(parents=True, exist_ok=False)
    archive = work / "train.zip"
    source = download_archive(
        OFFICIAL_TRAIN_SOURCE.url,
        archive,
        expected_size=OFFICIAL_TRAIN_SOURCE.size,
        expected_etag=OFFICIAL_TRAIN_SOURCE.etag,
        expected_last_modified=OFFICIAL_TRAIN_SOURCE.last_modified,
    )
    destination = Path("/output") / prefix
    manifest = prepare_bird_subset(
        archive,
        destination,
        database_ids=database_ids,
        source=source,
        min_tasks_per_database=5,
        expected_databases_member_size=OFFICIAL_TRAIN_SOURCE.databases_member_size,
        expected_databases_member_crc32=OFFICIAL_TRAIN_SOURCE.databases_member_crc32,
    )
    output_volume.commit()
    return {
        "output_prefix": prefix.as_posix(),
        "source_sha256": source.sha256,
        "eligible_task_count": manifest["selection"]["eligible_task_count"],
        "selected_database_count": len(manifest["selected_databases"]),
    }


@app.local_entrypoint()
def main(database_ids: str, output_prefix: str) -> None:
    selected = tuple(value.strip() for value in database_ids.split(",") if value.strip())
    result = prepare_official_train.remote(selected, output_prefix)
    print(json.dumps(result, sort_keys=True))


def _validated_output_prefix(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or len(value) > 200
        or value.startswith(("/", "~/"))
        or "\\" in value
        or ".." in path.parts
        or any(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", part) is None for part in path.parts)
    ):
        raise ValueError("output prefix must be a bounded relative path")
    return path
