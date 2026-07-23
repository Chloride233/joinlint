from __future__ import annotations

from typing import Any, Literal

import rfc8785
from pydantic import BaseModel, ConfigDict, Field


Status = Literal["ok", "findings", "inconclusive", "error"]
Severity = Literal["info", "warning", "blocking"]


class ErrorBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    severity: Severity
    message: str


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    command: str
    status: Status
    data: dict[str, Any] | None
    findings: list[Finding] = Field(default_factory=list)
    error: ErrorBody | None = None


def canonical_json(value: Any) -> bytes:
    """Serialize JSON-compatible data with RFC 8785 canonicalization."""
    return rfc8785.dumps(value)


def envelope_for(
    *, command: str, status: Status, error_code: str | None = None
) -> Envelope:
    if status in {"inconclusive", "error"}:
        if error_code is None:
            raise ValueError(f"{status} responses require an error code")
        return Envelope(
            command=command,
            status=status,
            data=None,
            error=ErrorBody(code=error_code, message=error_code),
        )

    if error_code is not None:
        raise ValueError(f"{status} responses cannot contain an error code")

    return Envelope(command=command, status=status, data={})
