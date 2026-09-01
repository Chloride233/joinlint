from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import Path

import pytest

from benchmarks.formal_eval.pilot import build_contract_ambiguity_pilot_inputs
from benchmarks.formal_eval.release_archive import (
    create_portable_archive,
    verify_portable_archive,
)


COMMIT = "a" * 40


def _pilot_bundle(tmp_path: Path) -> Path:
    root = tmp_path / "pilot-input"
    build_contract_ambiguity_pilot_inputs(root, commit=COMMIT)
    return root


def test_portable_archive_is_deterministic_and_round_trips(tmp_path: Path) -> None:
    root = _pilot_bundle(tmp_path)
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"

    first_receipt = create_portable_archive(root, first)
    second_receipt = create_portable_archive(root, second)

    assert first.read_bytes() == second.read_bytes()
    assert first_receipt == second_receipt
    assert verify_portable_archive(first) == first_receipt


def test_portable_archive_rejects_appledouble_source_files(tmp_path: Path) -> None:
    root = _pilot_bundle(tmp_path)
    (root / "databases" / "._grants.sqlite").write_bytes(b"sidecar")

    with pytest.raises(ValueError, match="AppleDouble"):
        create_portable_archive(root, tmp_path / "pilot-input.tar.gz")


def test_portable_archive_rejects_extended_attribute_headers(tmp_path: Path) -> None:
    archive_path = tmp_path / "xattr.tar.gz"
    payload = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=payload, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            info = tarfile.TarInfo("input-lock.json")
            info.size = 2
            info.pax_headers = {"SCHILY.xattr.com.apple.provenance": "value"}
            archive.addfile(info, io.BytesIO(b"{}"))
    archive_path.write_bytes(payload.getvalue())

    with pytest.raises(ValueError, match="extended attribute"):
        verify_portable_archive(archive_path)
