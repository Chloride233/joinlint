from __future__ import annotations

from importlib.metadata import version
from typing import Any


SUPPORTED_INSPECT_SANDBOXES_VERSION = "0.4.0"
_PATCH_MARKER = "_joinlint_modal_filesystem_v2"


def install_modal_filesystem_compat() -> bool:
    """Use Modal's reliable filesystem API until inspect-sandboxes adopts it."""
    try:
        from inspect_sandboxes.modal import _modal
    except ImportError:
        return False

    if version("inspect-sandboxes") != SUPPORTED_INSPECT_SANDBOXES_VERSION:
        raise RuntimeError("unsupported_inspect_sandboxes_version")

    environment = _modal.ModalSandboxEnvironment
    if getattr(environment, _PATCH_MARKER, False):
        return True

    environment._write_file_content = _modal._standard_retry(  # type: ignore[method-assign]
        _write_file_content
    )
    environment._read_file_content = _modal._standard_retry(  # type: ignore[method-assign]
        _read_file_content
    )
    environment._create_parent_folder = _modal._standard_retry(  # type: ignore[method-assign]
        _create_parent_folder
    )
    setattr(environment, _PATCH_MARKER, True)
    return True


async def _write_file_content(environment: Any, file: str, contents: str | bytes) -> None:
    filesystem = environment.sandbox.filesystem
    if isinstance(contents, str):
        await filesystem.write_text.aio(contents, file)
    else:
        await filesystem.write_bytes.aio(contents, file)


async def _read_file_content(environment: Any, file: str) -> bytes:
    return await environment.sandbox.filesystem.read_bytes.aio(file)


async def _create_parent_folder(environment: Any, path: str) -> None:
    try:
        await environment.sandbox.filesystem.make_directory.aio(
            path,
            create_parents=True,
        )
    except FileExistsError:
        pass
