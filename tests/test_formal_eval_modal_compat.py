from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("modal")
pytest.importorskip("inspect_sandboxes")

from benchmarks.formal_eval.modal_compat import (
    _create_parent_folder,
    _read_file_content,
    _write_file_content,
    install_modal_filesystem_compat,
)


class _AsyncMethod:
    def __init__(self, implementation: Any) -> None:
        self.aio = implementation


class _Filesystem:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.write_text = _AsyncMethod(self._write_text)
        self.write_bytes = _AsyncMethod(self._write_bytes)
        self.read_bytes = _AsyncMethod(self._read_bytes)
        self.make_directory = _AsyncMethod(self._make_directory)

    async def _write_text(self, data: str, path: str) -> None:
        self.calls.append(("write_text", data, path))

    async def _write_bytes(self, data: bytes, path: str) -> None:
        self.calls.append(("write_bytes", data, path))

    async def _read_bytes(self, path: str) -> bytes:
        self.calls.append(("read_bytes", path))
        return b"payload"

    async def _make_directory(self, path: str, *, create_parents: bool) -> None:
        self.calls.append(("make_directory", path, create_parents))


def test_modal_compat_uses_filesystem_v2_for_text_bytes_and_directories() -> None:
    filesystem = _Filesystem()
    environment = SimpleNamespace(sandbox=SimpleNamespace(filesystem=filesystem))

    async def exercise() -> bytes:
        await _create_parent_folder(environment, "/tmp/tools")
        await _write_file_content(environment, "/tmp/tools/text", "value")
        await _write_file_content(environment, "/tmp/tools/binary", b"value")
        return await _read_file_content(environment, "/tmp/tools/binary")

    assert asyncio.run(exercise()) == b"payload"
    assert filesystem.calls == [
        ("make_directory", "/tmp/tools", True),
        ("write_text", "value", "/tmp/tools/text"),
        ("write_bytes", b"value", "/tmp/tools/binary"),
        ("read_bytes", "/tmp/tools/binary"),
    ]


def test_modal_compat_install_is_idempotent() -> None:
    assert install_modal_filesystem_compat() is True
    assert install_modal_filesystem_compat() is True
