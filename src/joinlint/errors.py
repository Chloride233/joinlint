from __future__ import annotations


class JoinLintError(Exception):
    """A user-visible failure with a stable code and CLI exit status."""

    def __init__(
        self,
        code: str,
        message: str,
        exit_code: int,
        *,
        affected_refs: tuple[str, ...] = (),
        blocking_relationship_ids: tuple[str, ...] = (),
        freshness_reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.affected_refs = affected_refs
        self.blocking_relationship_ids = blocking_relationship_ids
        self.freshness_reason = freshness_reason
