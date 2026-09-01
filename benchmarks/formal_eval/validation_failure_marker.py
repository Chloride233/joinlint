from __future__ import annotations

import os
import stat
from pathlib import Path


VALIDATION_FAILURE_MARKER_CLEAR = b"0\n"
VALIDATION_FAILURE_MARKER_FAILED = b"1\n"


def validation_failure_marker_failed(value: bytes) -> bool:
    if value == VALIDATION_FAILURE_MARKER_CLEAR:
        return False
    if value == VALIDATION_FAILURE_MARKER_FAILED:
        return True
    raise ValueError("validation failure marker is malformed")


class ValidationFailureMarker:
    """Sticky evaluation-only marker for a validation-ledger write failure."""

    def __init__(self, path: Path) -> None:
        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("validation failure marker must be a regular file")
            validation_failure_marker_failed(
                os.pread(descriptor, len(VALIDATION_FAILURE_MARKER_CLEAR) + 1, 0)
            )
        except Exception:
            os.close(descriptor)
            raise
        self._descriptor: int | None = descriptor

    def mark_failed(self) -> None:
        descriptor = self._require_open_descriptor()
        current = os.pread(descriptor, len(VALIDATION_FAILURE_MARKER_CLEAR) + 1, 0)
        if validation_failure_marker_failed(current):
            return
        written = os.pwrite(descriptor, VALIDATION_FAILURE_MARKER_FAILED, 0)
        if written != len(VALIDATION_FAILURE_MARKER_FAILED):
            raise OSError("validation failure marker write was incomplete")
        os.fsync(descriptor)

    def close(self) -> None:
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None

    def _require_open_descriptor(self) -> int:
        if self._descriptor is None:
            raise RuntimeError("validation failure marker is closed")
        return self._descriptor
