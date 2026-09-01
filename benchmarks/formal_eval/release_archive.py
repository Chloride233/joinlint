from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import stat
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from benchmarks.formal_eval.pilot import verify_pilot_input_bundle


def create_portable_archive(root: Path, output: Path) -> dict[str, Any]:
    members = _source_members(root)
    verify_pilot_input_bundle(root)
    if output.exists() or output.is_symlink():
        raise ValueError("archive output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with tarfile.open(
                    fileobj=compressed,
                    mode="w",
                    format=tarfile.PAX_FORMAT,
                ) as archive:
                    for path, relative in members:
                        info = _tar_info(path, relative)
                        if info.isfile():
                            with path.open("rb") as source:
                                archive.addfile(info, source)
                        else:
                            archive.addfile(info)
        receipt = verify_portable_archive(temporary)
        temporary.replace(output)
        return receipt
    finally:
        temporary.unlink(missing_ok=True)


def verify_portable_archive(archive_path: Path) -> dict[str, Any]:
    if archive_path.is_symlink() or not archive_path.is_file():
        raise ValueError("archive must be one regular file")
    with tempfile.TemporaryDirectory(prefix="joinlint-pilot-archive-") as directory:
        extracted = Path(directory)
        with tarfile.open(archive_path, mode="r:gz") as archive:
            members = archive.getmembers()
            _validate_archive_members(members)
            archive.extractall(extracted, members=members, filter="data")
        verify_pilot_input_bundle(extracted)
    return {
        "schema_version": 1,
        "sha256": _sha256(archive_path),
        "member_count": len(members),
        "file_count": sum(member.isfile() for member in members),
    }


def _source_members(root: Path) -> list[tuple[Path, PurePosixPath]]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("archive source must be one regular directory")
    members: list[tuple[Path, PurePosixPath]] = []
    for path in sorted(root.rglob("*"), key=lambda value: value.relative_to(root).as_posix()):
        relative = PurePosixPath(path.relative_to(root).as_posix())
        _reject_apple_metadata(relative)
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not (
            stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)
        ):
            raise ValueError(f"archive source contains a special file: {relative}")
        members.append((path, relative))
    return members


def _tar_info(path: Path, relative: PurePosixPath) -> tarfile.TarInfo:
    metadata = path.stat()
    info = tarfile.TarInfo(relative.as_posix())
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    info.pax_headers = {}
    if stat.S_ISDIR(metadata.st_mode):
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
    else:
        info.type = tarfile.REGTYPE
        info.mode = 0o644
        info.size = metadata.st_size
    return info


def _validate_archive_members(members: list[tarfile.TarInfo]) -> None:
    names: set[str] = set()
    for member in members:
        relative = PurePosixPath(member.name)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError("archive contains an unsafe member path")
        _reject_apple_metadata(relative)
        normalized = relative.as_posix().rstrip("/")
        if normalized in names:
            raise ValueError("archive contains duplicate members")
        names.add(normalized)
        if not (member.isfile() or member.isdir()):
            raise ValueError("archive contains links or special files")
        if any(
            "xattr" in key.lower()
            or key.lower().startswith(("com.apple.", "schily.acl."))
            for key in member.pax_headers
        ):
            raise ValueError("archive contains an extended attribute header")


def _reject_apple_metadata(relative: PurePosixPath) -> None:
    if any(part.startswith("._") or part == ".DS_Store" for part in relative.parts):
        raise ValueError("archive contains AppleDouble metadata")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="formal-release-archive")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--root", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--archive", type=Path, required=True)
    arguments = parser.parse_args(argv)

    receipt = (
        create_portable_archive(arguments.root, arguments.output)
        if arguments.command == "create"
        else verify_portable_archive(arguments.archive)
    )
    print(json.dumps(receipt, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
