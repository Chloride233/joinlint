from __future__ import annotations


class JoinLintError(Exception):
    """A user-visible failure with a stable code and CLI exit status."""

    def __init__(self, code: str, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
