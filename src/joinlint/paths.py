from __future__ import annotations

import errno
import os
import stat
from pathlib import Path, PurePosixPath

from joinlint.errors import JoinLintError


_REQUIRED_FLAGS = ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC")
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def resolve_project_root(start: Path) -> Path:
    """Return the nearest ancestor that contains a regular JoinLint config."""
    try:
        resolved_start = start.resolve(strict=True)
    except OSError as exc:
        raise JoinLintError("PROJECT_NOT_FOUND", "project path is unavailable", 2) from exc

    current = resolved_start if resolved_start.is_dir() else resolved_start.parent
    while True:
        joinlint_directory = current / ".joinlint"
        config = joinlint_directory / "config.yaml"
        if _is_regular_non_symlink(joinlint_directory, directory=True) and _is_regular_non_symlink(
            config, directory=False
        ):
            return current
        if current.parent == current:
            break
        current = current.parent

    raise JoinLintError("PROJECT_NOT_FOUND", "no JoinLint project was found", 2)


def _is_regular_non_symlink(path: Path, *, directory: bool) -> bool:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return False

    if stat.S_ISLNK(path_stat.st_mode):
        return False
    return stat.S_ISDIR(path_stat.st_mode) if directory else stat.S_ISREG(path_stat.st_mode)


class SafeProject:
    """An anchored directory descriptor for safe project-relative reads."""

    def __init__(self, root: Path) -> None:
        if os.name != "posix" or any(not hasattr(os, flag) for flag in _REQUIRED_FLAGS):
            raise JoinLintError(
                "PLATFORM_UNSUPPORTED",
                "macOS or Linux descriptor-relative access is required",
                2,
            )
        if root.is_symlink():
            raise JoinLintError("SYMLINK_NOT_ALLOWED", "project root cannot be a symlink", 2)

        try:
            self.root = root.resolve(strict=True)
        except OSError as exc:
            raise JoinLintError("PROJECT_NOT_FOUND", "project root is unavailable", 2) from exc
        if not self.root.is_dir():
            raise JoinLintError("PROJECT_NOT_FOUND", "project root is not a directory", 2)

        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        try:
            self._root_fd = os.open(self.root, flags)
        except OSError as exc:
            raise JoinLintError("SAFE_PATH_OPEN_FAILED", "could not open project root", 2) from exc

        descriptor_stat = os.fstat(self._root_fd)
        path_stat = self.root.lstat()
        if not stat.S_ISDIR(descriptor_stat.st_mode) or (
            descriptor_stat.st_dev,
            descriptor_stat.st_ino,
        ) != (path_stat.st_dev, path_stat.st_ino):
            os.close(self._root_fd)
            raise JoinLintError("SAFE_PATH_OPEN_FAILED", "project root changed during open", 2)

    def __enter__(self) -> SafeProject:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def close(self) -> None:
        if self._root_fd != -1:
            os.close(self._root_fd)
            self._root_fd = -1

    def open_relative(self, relative: PurePosixPath, flags: int) -> int:
        """Open a project-relative regular object without following symlinks."""
        if self._root_fd == -1:
            raise JoinLintError("SAFE_PROJECT_CLOSED", "project boundary is closed", 4)
        if flags & _WRITE_FLAGS:
            raise JoinLintError("SAFE_PATH_WRITE_FORBIDDEN", "source paths are read-only", 2)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise JoinLintError("PATH_OUTSIDE_PROJECT", "path is not project-relative", 2)

        descriptor = os.dup(self._root_fd)
        try:
            for index, component in enumerate(relative.parts):
                is_last = index == len(relative.parts) - 1
                component_stat = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISLNK(component_stat.st_mode):
                    raise JoinLintError("SYMLINK_NOT_ALLOWED", "symlinks are not allowed", 2)
                open_flags = flags if is_last else os.O_RDONLY | os.O_DIRECTORY
                next_descriptor = os.open(
                    component,
                    open_flags | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = next_descriptor

                opened_stat = os.fstat(descriptor)
                if not is_last and not stat.S_ISDIR(opened_stat.st_mode):
                    raise JoinLintError("SAFE_PATH_OPEN_FAILED", "path component is not a directory", 2)
                if is_last and flags & os.O_DIRECTORY and not stat.S_ISDIR(opened_stat.st_mode):
                    raise JoinLintError("UNSUPPORTED_SOURCE", "expected a directory", 2)
                if is_last and not flags & os.O_DIRECTORY and not stat.S_ISREG(opened_stat.st_mode):
                    raise JoinLintError("UNSUPPORTED_SOURCE", "expected a regular file", 2)
            return descriptor
        except JoinLintError:
            os.close(descriptor)
            raise
        except OSError as exc:
            os.close(descriptor)
            if exc.errno == errno.ELOOP:
                raise JoinLintError("SYMLINK_NOT_ALLOWED", "symlinks are not allowed", 2) from exc
            raise JoinLintError("SAFE_PATH_OPEN_FAILED", "could not open project-relative path", 2) from exc
