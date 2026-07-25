from __future__ import annotations

import errno
import os
import secrets
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
        self._validate_relative(relative)

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

    def exists_relative(self, relative: PurePosixPath) -> bool:
        """Return whether a non-symlink project-relative path exists."""
        parent_descriptor, leaf = self._open_parent_directory(relative)
        try:
            try:
                entry_stat = os.stat(leaf, dir_fd=parent_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return False
            if stat.S_ISLNK(entry_stat.st_mode):
                raise JoinLintError("SYMLINK_NOT_ALLOWED", "symlinks are not allowed", 2)
            return True
        finally:
            os.close(parent_descriptor)

    def ensure_directory_relative(self, relative: PurePosixPath) -> None:
        """Create a project-relative directory tree without following symlinks."""
        self._validate_relative(relative)
        descriptor = os.dup(self._root_fd)
        try:
            for component in relative.parts:
                try:
                    os.mkdir(component, 0o755, dir_fd=descriptor)
                except FileExistsError:
                    pass
                component_stat = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISLNK(component_stat.st_mode):
                    raise JoinLintError("SYMLINK_NOT_ALLOWED", "symlinks are not allowed", 2)
                next_descriptor = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = next_descriptor
                if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    raise JoinLintError("SAFE_PATH_OPEN_FAILED", "path component is not a directory", 2)
        except JoinLintError:
            os.close(descriptor)
            raise
        except OSError as exc:
            os.close(descriptor)
            if exc.errno == errno.ELOOP:
                raise JoinLintError("SYMLINK_NOT_ALLOWED", "symlinks are not allowed", 2) from exc
            raise JoinLintError("SAFE_PATH_WRITE_FAILED", "could not create project-relative directory", 2) from exc
        else:
            os.close(descriptor)

    def create_directory_relative(self, relative: PurePosixPath) -> None:
        """Create one new project-relative directory without following symlinks."""
        parent_descriptor, leaf = self._open_parent_directory(relative)
        try:
            os.mkdir(leaf, 0o700, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
        except FileExistsError as exc:
            raise JoinLintError("SAFE_PATH_WRITE_FAILED", "temporary path already exists", 2) from exc
        except OSError as exc:
            raise JoinLintError("SAFE_PATH_WRITE_FAILED", "could not create project-relative directory", 2) from exc
        finally:
            os.close(parent_descriptor)

    def write_relative_atomically(self, relative: PurePosixPath, body: bytes) -> None:
        """Atomically replace one project-relative regular file without following symlinks."""
        parent_descriptor, leaf = self._open_parent_directory(relative)
        temporary_name = ""
        descriptor = -1
        try:
            for _ in range(10):
                temporary_name = f".{leaf}.{secrets.token_hex(12)}.tmp"
                try:
                    descriptor = os.open(
                        temporary_name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                        0o600,
                        dir_fd=parent_descriptor,
                    )
                    break
                except FileExistsError:
                    continue
            if descriptor == -1:
                raise JoinLintError("SAFE_PATH_WRITE_FAILED", "could not allocate temporary path", 2)
            with os.fdopen(descriptor, "wb") as destination:
                descriptor = -1
                destination.write(body)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(
                temporary_name,
                leaf,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            temporary_name = ""
            os.fsync(parent_descriptor)
        except JoinLintError:
            raise
        except OSError as exc:
            raise JoinLintError("SAFE_PATH_WRITE_FAILED", "could not write project-relative file", 2) from exc
        finally:
            if descriptor != -1:
                os.close(descriptor)
            if temporary_name:
                try:
                    os.unlink(temporary_name, dir_fd=parent_descriptor)
                except FileNotFoundError:
                    pass
            os.close(parent_descriptor)

    def replace_relative(self, source: PurePosixPath, destination: PurePosixPath) -> None:
        """Atomically rename a project-relative path using anchored parent descriptors."""
        source_parent, source_leaf = self._open_parent_directory(source)
        try:
            destination_parent, destination_leaf = self._open_parent_directory(destination)
        except BaseException:
            os.close(source_parent)
            raise
        try:
            os.replace(
                source_leaf,
                destination_leaf,
                src_dir_fd=source_parent,
                dst_dir_fd=destination_parent,
            )
            os.fsync(source_parent)
            if destination_parent != source_parent:
                os.fsync(destination_parent)
        except OSError as exc:
            raise JoinLintError("SAFE_PATH_WRITE_FAILED", "could not replace project-relative path", 2) from exc
        finally:
            os.close(source_parent)
            os.close(destination_parent)

    def unlink_relative(self, relative: PurePosixPath, *, missing_ok: bool = False) -> None:
        """Remove one project-relative non-directory entry without following symlinks."""
        parent_descriptor, leaf = self._open_parent_directory(relative)
        try:
            os.unlink(leaf, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
        except FileNotFoundError:
            if not missing_ok:
                raise JoinLintError("SAFE_PATH_OPEN_FAILED", "project-relative path is missing", 2) from None
        except OSError as exc:
            raise JoinLintError("SAFE_PATH_WRITE_FAILED", "could not remove project-relative path", 2) from exc
        finally:
            os.close(parent_descriptor)

    def remove_tree_relative(self, relative: PurePosixPath, *, missing_ok: bool = False) -> None:
        """Remove a project-relative directory tree without traversing symlinks."""
        parent_descriptor, leaf = self._open_parent_directory(relative)
        try:
            try:
                entry_stat = os.stat(leaf, dir_fd=parent_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                if missing_ok:
                    return
                raise JoinLintError("SAFE_PATH_OPEN_FAILED", "project-relative path is missing", 2) from None
            if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISDIR(entry_stat.st_mode):
                os.unlink(leaf, dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)
                return
            descriptor = os.open(
                leaf,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_descriptor,
            )
            try:
                self._remove_tree_contents(descriptor)
            finally:
                os.close(descriptor)
            os.rmdir(leaf, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
        except JoinLintError:
            raise
        except OSError as exc:
            raise JoinLintError("SAFE_PATH_WRITE_FAILED", "could not remove project-relative directory", 2) from exc
        finally:
            os.close(parent_descriptor)

    def open_parent_relative(self, relative: PurePosixPath) -> tuple[int, str, os.stat_result]:
        """Return an anchored parent FD, validated leaf name, and leaf identity."""
        descriptor, leaf = self._open_parent_directory(relative)
        try:
            leaf_stat = os.stat(leaf, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(leaf_stat.st_mode):
                raise JoinLintError("SYMLINK_NOT_ALLOWED", "symlinks are not allowed", 2)
            if not stat.S_ISREG(leaf_stat.st_mode):
                raise JoinLintError("UNSUPPORTED_SOURCE", "expected a regular file", 2)
            return descriptor, leaf, leaf_stat
        except BaseException:
            os.close(descriptor)
            raise

    def _open_parent_directory(self, relative: PurePosixPath) -> tuple[int, str]:
        if self._root_fd == -1:
            raise JoinLintError("SAFE_PROJECT_CLOSED", "project boundary is closed", 4)
        self._validate_relative(relative)

        descriptor = os.dup(self._root_fd)
        try:
            for component in relative.parts[:-1]:
                component_stat = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISLNK(component_stat.st_mode):
                    raise JoinLintError("SYMLINK_NOT_ALLOWED", "symlinks are not allowed", 2)
                next_descriptor = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = next_descriptor
                if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    raise JoinLintError("SAFE_PATH_OPEN_FAILED", "path component is not a directory", 2)

            leaf = relative.parts[-1]
            return descriptor, leaf
        except JoinLintError:
            os.close(descriptor)
            raise
        except OSError as exc:
            os.close(descriptor)
            if exc.errno == errno.ELOOP:
                raise JoinLintError("SYMLINK_NOT_ALLOWED", "symlinks are not allowed", 2) from exc
            raise JoinLintError("SAFE_PATH_OPEN_FAILED", "could not open project-relative path", 2) from exc

    def _remove_tree_contents(self, descriptor: int) -> None:
        for name in os.listdir(descriptor):
            entry_stat = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(entry_stat.st_mode) and not stat.S_ISLNK(entry_stat.st_mode):
                child_descriptor = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=descriptor,
                )
                try:
                    self._remove_tree_contents(child_descriptor)
                finally:
                    os.close(child_descriptor)
                os.rmdir(name, dir_fd=descriptor)
            else:
                os.unlink(name, dir_fd=descriptor)

    @staticmethod
    def _validate_relative(relative: PurePosixPath) -> None:
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise JoinLintError("PATH_OUTSIDE_PROJECT", "path is not project-relative", 2)
